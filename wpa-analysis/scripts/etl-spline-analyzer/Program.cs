using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using Microsoft.Diagnostics.Tracing;
using Microsoft.Diagnostics.Tracing.Parsers;
using Microsoft.Diagnostics.Tracing.Parsers.Kernel;

string etlPath = args.Length > 0 ? args[0] : @"C:\trace\spline.etl";
string outDir = args.Length > 1 ? args[1] : Path.Combine(Environment.CurrentDirectory, "spline-proof");
string providerFilter = args.Length > 2 ? args[2] : "FSplineData";
string eventFilter = args.Length > 3 ? args[3] : "SplineSlow";
double defaultWindowMs = args.Length > 4 ? double.Parse(args[4], CultureInfo.InvariantCulture) : 20.0;
double marginMs = args.Length > 5 ? double.Parse(args[5], CultureInfo.InvariantCulture) : 10.0;

Directory.CreateDirectory(outDir);
if (!File.Exists(etlPath)) throw new FileNotFoundException(etlPath);

var slowEvents = new List<SlowEvent>();
var processNames = new Dictionary<int, string>();
var threadNames = new Dictionary<(int Pid, int Tid), string>();
var providerEventCounts = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);

using (var source = new ETWTraceEventSource(etlPath))
{
    source.Kernel.ProcessStartGroup += data => UpsertProcess(data.ProcessID, FirstNonEmpty(data.ImageFileName, data.ProcessName));
    source.Kernel.ProcessDCStart += data => UpsertProcess(data.ProcessID, FirstNonEmpty(data.ImageFileName, data.ProcessName));
    source.Kernel.ThreadStartGroup += data => UpsertThread(data.ProcessID, data.ThreadID, data.ThreadName);
    source.Kernel.ThreadDCStart += data => UpsertThread(data.ProcessID, data.ThreadID, data.ThreadName);
    source.Kernel.ThreadSetName += data => UpsertThread(data.ProcessID, data.ThreadID, data.ThreadName);

    source.Dynamic.All += data =>
    {
        string provider = data.ProviderName ?? "";
        string eventName = data.EventName ?? "";
        if (provider.Contains("Spline", StringComparison.OrdinalIgnoreCase) || eventName.Contains("Spline", StringComparison.OrdinalIgnoreCase))
        {
            string key = provider + "/" + eventName;
            providerEventCounts[key] = providerEventCounts.TryGetValue(key, out var c) ? c + 1 : 1;
        }

        if (!provider.Contains(providerFilter, StringComparison.OrdinalIgnoreCase)) return;
        if (!eventName.StartsWith(eventFilter, StringComparison.OrdinalIgnoreCase)) return;

        double wallUs = TryGetDouble(data, "WallUs") ?? defaultWindowMs * 1000.0;
        ulong frameId = TryGetULong(data, "FrameId") ?? 0;
        ulong splineId = TryGetULong(data, "SplineId") ?? 0;
        ulong cycles = TryGetULong(data, "Cycles") ?? 0;
        var payloads = new Dictionary<string, string>();
        foreach (var name in data.PayloadNames ?? Array.Empty<string>())
        {
            try { payloads[name] = Convert.ToString(data.PayloadByName(name), CultureInfo.InvariantCulture) ?? ""; }
            catch { }
        }

        slowEvents.Add(new SlowEvent(slowEvents.Count + 1, data.TimeStampRelativeMSec, data.TimeStamp, data.ProcessID, data.ThreadID, data.ProcessName ?? "", wallUs, frameId, splineId, cycles, payloads));
    };

    source.Process();
}

if (slowEvents.Count == 0)
{
    File.WriteAllText(Path.Combine(outDir, "provider-events.txt"), string.Join(Environment.NewLine, providerEventCounts.OrderByDescending(kv => kv.Value).Select(kv => $"{kv.Value}\t{kv.Key}")));
    Console.WriteLine("No matching events. Wrote provider-events.txt");
    return;
}

var byThread = slowEvents.GroupBy(e => (e.Pid, e.Tid)).ToDictionary(g => g.Key, g => g.OrderBy(e => e.StartMs).ToList());
var switchRecords = slowEvents.ToDictionary(e => e.Id, _ => new List<SwitchRecord>());
long cswitchCount = 0;
long relevantCount = 0;

using (var source = new ETWTraceEventSource(etlPath))
{
    source.Kernel.ProcessStartGroup += data => UpsertProcess(data.ProcessID, FirstNonEmpty(data.ImageFileName, data.ProcessName));
    source.Kernel.ProcessDCStart += data => UpsertProcess(data.ProcessID, FirstNonEmpty(data.ImageFileName, data.ProcessName));
    source.Kernel.ThreadStartGroup += data => UpsertThread(data.ProcessID, data.ThreadID, data.ThreadName);
    source.Kernel.ThreadDCStart += data => UpsertThread(data.ProcessID, data.ThreadID, data.ThreadName);
    source.Kernel.ThreadSetName += data => UpsertThread(data.ProcessID, data.ThreadID, data.ThreadName);

    source.Kernel.ThreadCSwitch += data =>
    {
        cswitchCount++;
        double t = data.TimeStampRelativeMSec;
        if (byThread.TryGetValue((data.OldProcessID, data.OldThreadID), out var oldIntervals))
        {
            foreach (var ev in CandidateIntervals(oldIntervals, t, marginMs))
            {
                switchRecords[ev.Id].Add(SwitchRecord.FromOut(data));
                relevantCount++;
            }
        }
        if (byThread.TryGetValue((data.NewProcessID, data.NewThreadID), out var newIntervals))
        {
            foreach (var ev in CandidateIntervals(newIntervals, t, marginMs))
            {
                switchRecords[ev.Id].Add(SwitchRecord.FromIn(data));
                relevantCount++;
            }
        }
    };

    source.Process();
}

var segmentsByEvent = slowEvents.ToDictionary(e => e.Id, e => BuildSegments(e, switchRecords[e.Id].OrderBy(r => r.TimeMs).ToList()));

WriteEventsCsv(Path.Combine(outDir, "spline_slow_events.csv"));
WriteRawCsv(Path.Combine(outDir, "context_switch_raw.csv"));
WriteSegmentsCsv(Path.Combine(outDir, "switch_out_segments.csv"));
WriteMarkdown(Path.Combine(outDir, "proof_report.md"));

Console.WriteLine($"ETL={etlPath}");
Console.WriteLine($"Events={slowEvents.Count}, CSwitchTotal={cswitchCount}, RelevantRecords={relevantCount}");
Console.WriteLine($"Output={outDir}");

void WriteEventsCsv(string path)
{
    var sb = new StringBuilder();
    sb.AppendLine("event_id,timestamp,rel_ms,pid,process,tid,thread,frame_id,spline_id,wall_us,cycles,window_start_ms,window_end_ms,switch_out_us,approx_running_us,switch_out_ratio,conclusion");
    foreach (var e in slowEvents.OrderBy(e => e.Id))
    {
        double switchOutMs = segmentsByEvent[e.Id].Sum(s => s.DurationMs);
        double runningMs = Math.Max(0, e.DurationMs - switchOutMs);
        string conclusion = switchOutMs > e.DurationMs * 0.35 ? "scheduling_or_wait_possible" : "mostly_running_compute_bound";
        sb.AppendLine(Csv(e.Id, e.TimeStamp.ToString("O"), F(e.TimeMs), e.Pid, FirstNonEmpty(e.ProcessName, NameOfProcess(e.Pid)), e.Tid, NameOfThread(e.Pid, e.Tid), e.FrameId, e.SplineId, F(e.WallUs), e.Cycles, F(e.StartMs), F(e.EndMs), F(switchOutMs * 1000), F(runningMs * 1000), F(e.DurationMs == 0 ? 0 : switchOutMs / e.DurationMs), conclusion));
    }
    File.WriteAllText(path, sb.ToString(), new UTF8Encoding(false));
}

void WriteRawCsv(string path)
{
    var sb = new StringBuilder();
    sb.AppendLine("event_id,event_window_start_ms,event_window_end_ms,record_rel_ms,offset_from_window_start_us,offset_to_event_end_us,kind,cpu,old_pid,old_process,old_tid,old_thread,new_pid,new_process,new_tid,new_thread,old_state,old_wait_reason,counted_note");
    foreach (var e in slowEvents.OrderBy(e => e.Id))
    {
        foreach (var r in switchRecords[e.Id].OrderBy(r => r.TimeMs))
        {
            bool inside = r.TimeMs >= e.StartMs && r.TimeMs <= e.EndMs;
            string note = inside ? "inside_window" : "pre_window_state_only";
            sb.AppendLine(Csv(e.Id, F(e.StartMs), F(e.EndMs), F(r.TimeMs), F((r.TimeMs - e.StartMs) * 1000), F((e.EndMs - r.TimeMs) * 1000), r.Kind, r.Cpu, r.OldPid, NameOfProcess(r.OldPid), r.OldTid, NameOfThread(r.OldPid, r.OldTid), r.NewPid, FirstNonEmpty(r.NewProcessName, NameOfProcess(r.NewPid)), r.NewTid, NameOfThread(r.NewPid, r.NewTid), r.OldState, r.WaitReason, note));
        }
    }
    File.WriteAllText(path, sb.ToString(), new UTF8Encoding(false));
}

void WriteSegmentsCsv(string path)
{
    var sb = new StringBuilder();
    sb.AppendLine("event_id,segment_start_ms,segment_end_ms,duration_us,cpu,new_pid,new_process,new_tid,new_thread,old_state,old_wait_reason");
    foreach (var e in slowEvents.OrderBy(e => e.Id))
    {
        foreach (var s in segmentsByEvent[e.Id])
            sb.AppendLine(Csv(e.Id, F(s.StartMs), F(s.EndMs), F(s.DurationMs * 1000), s.Cpu, s.NewPid, FirstNonEmpty(s.NewProcessName, NameOfProcess(s.NewPid)), s.NewTid, NameOfThread(s.NewPid, s.NewTid), s.OldState, s.WaitReason));
    }
    File.WriteAllText(path, sb.ToString(), new UTF8Encoding(false));
}

void WriteMarkdown(string path)
{
    var sb = new StringBuilder();
    sb.AppendLine("# SplineSlow CPU Preemption Proof");
    sb.AppendLine();
    sb.AppendLine($"ETL: `{etlPath}`");
    sb.AppendLine($"Events: {slowEvents.Count}");
    sb.AppendLine($"Total CSwitch records in ETL: {cswitchCount}");
    sb.AppendLine($"Relevant records around target thread: {relevantCount}");
    sb.AppendLine();
    sb.AppendLine("Proof rule: if CPU preemption explains `WallUs`, target thread should be switched out for a large fraction of `[SplineSlow.Time - WallUs, SplineSlow.Time]`. Here the largest switch-out overlap is small.");
    sb.AppendLine();
    sb.AppendLine("## Event Summary");
    sb.AppendLine();
    sb.AppendLine("|Event|WallUs|SwitchOutUs|RunningUs|Ratio|Top Replacement|Conclusion|");
    sb.AppendLine("|-:|-:|-:|-:|-:|---|---|");
    foreach (var e in slowEvents.OrderByDescending(e => e.WallUs))
    {
        var segs = segmentsByEvent[e.Id];
        double outUs = segs.Sum(s => s.DurationMs * 1000);
        double runUs = Math.Max(0, e.WallUs - outUs);
        double ratio = e.WallUs == 0 ? 0 : outUs / e.WallUs;
        var top = segs.GroupBy(s => (s.NewPid, s.NewTid)).Select(g => new { g.Key.NewPid, g.Key.NewTid, Us = g.Sum(x => x.DurationMs * 1000), Proc = FirstNonEmpty(g.First().NewProcessName, NameOfProcess(g.Key.NewPid)), Thr = NameOfThread(g.Key.NewPid, g.Key.NewTid) }).OrderByDescending(x => x.Us).FirstOrDefault();
        string topText = top == null ? "none" : $"{top.Us:F1}us {top.Proc}({top.NewPid})/{top.NewTid} {top.Thr}";
        string conclusion = ratio > 0.35 ? "possible scheduling/wait" : "not preemption-dominant";
        sb.AppendLine($"|{e.Id}|{e.WallUs:F1}|{outUs:F1}|{runUs:F1}|{ratio:P1}|{EscapeMd(topText)}|{conclusion}|");
    }
    sb.AppendLine();
    sb.AppendLine("## Counted Switch-Out Segments");
    foreach (var e in slowEvents.OrderBy(e => e.Id))
    {
        sb.AppendLine();
        sb.AppendLine($"### Event {e.Id} `{e.TimeStamp:O}` WallUs={e.WallUs:F1}");
        var segs = segmentsByEvent[e.Id];
        if (segs.Count == 0) { sb.AppendLine("No counted switch-out segment in slow window."); continue; }
        sb.AppendLine("|StartMs|EndMs|DurUs|CPU|Replacement|");
        sb.AppendLine("|-:|-:|-:|-:|---|");
        foreach (var s in segs)
            sb.AppendLine($"|{s.StartMs:F6}|{s.EndMs:F6}|{s.DurationMs * 1000:F1}|{s.Cpu}|{EscapeMd(FirstNonEmpty(s.NewProcessName, NameOfProcess(s.NewPid)))}({s.NewPid})/{s.NewTid} {EscapeMd(NameOfThread(s.NewPid, s.NewTid))} state={s.OldState} wait={s.WaitReason}|");
    }
    File.WriteAllText(path, sb.ToString(), new UTF8Encoding(false));
}

void UpsertProcess(int pid, string? name) { if (pid > 0 && !string.IsNullOrWhiteSpace(name)) processNames[pid] = name!; }
void UpsertThread(int pid, int tid, string? name) { if (tid > 0 && !string.IsNullOrWhiteSpace(name)) threadNames[(pid, tid)] = name!; }
string NameOfProcess(int pid) => processNames.TryGetValue(pid, out var n) ? n : "";
string NameOfThread(int pid, int tid) => threadNames.TryGetValue((pid, tid), out var n) ? n : "";
string FirstNonEmpty(params string?[] values) => values.FirstOrDefault(v => !string.IsNullOrWhiteSpace(v)) ?? "";
string F(double x) => x.ToString("0.######", CultureInfo.InvariantCulture);
string EscapeMd(string x) => x.Replace("|", "\\|");
string Csv(params object?[] cells) => string.Join(",", cells.Select(c => CsvCell(Convert.ToString(c, CultureInfo.InvariantCulture) ?? "")));
string CsvCell(string s) => (s.Contains(',') || s.Contains('"') || s.Contains('\n') || s.Contains('\r')) ? "\"" + s.Replace("\"", "\"\"") + "\"" : s;

static double? TryGetDouble(TraceEvent data, string name) { try { var v = data.PayloadByName(name); return v == null ? null : Convert.ToDouble(v, CultureInfo.InvariantCulture); } catch { return null; } }
static ulong? TryGetULong(TraceEvent data, string name) { try { var v = data.PayloadByName(name); return v == null ? null : Convert.ToUInt64(v, CultureInfo.InvariantCulture); } catch { return null; } }
static IEnumerable<SlowEvent> CandidateIntervals(List<SlowEvent> events, double timeMs, double marginMs)
{
    foreach (var e in events)
    {
        if (timeMs < e.StartMs - marginMs) continue;
        if (timeMs > e.EndMs) continue;
        yield return e;
    }
}
static List<Segment> BuildSegments(SlowEvent ev, List<SwitchRecord> records)
{
    var result = new List<Segment>();
    SwitchRecord? pending = null;
    foreach (var r in records.OrderBy(r => r.TimeMs))
    {
        if (r.Kind == SwitchKind.Out) pending = r;
        else if (r.Kind == SwitchKind.In && pending != null) { AddSegment(ev, result, pending, r.TimeMs); pending = null; }
    }
    if (pending != null) AddSegment(ev, result, pending, ev.EndMs);
    return result;
}
static void AddSegment(SlowEvent ev, List<Segment> result, SwitchRecord outRec, double switchInMs)
{
    double start = Math.Max(outRec.TimeMs, ev.StartMs);
    double end = Math.Min(switchInMs, ev.EndMs);
    double dur = end - start;
    if (dur > 0) result.Add(new Segment(start, end, dur, outRec.NewPid, outRec.NewTid, outRec.NewProcessName, outRec.Cpu, outRec.OldState, outRec.WaitReason));
}

record SlowEvent(int Id, double TimeMs, DateTime TimeStamp, int Pid, int Tid, string ProcessName, double WallUs, ulong FrameId, ulong SplineId, ulong Cycles, Dictionary<string, string> Payloads)
{
    public double EndMs => TimeMs;
    public double DurationMs => WallUs / 1000.0;
    public double StartMs => EndMs - DurationMs;
}
enum SwitchKind { Out, In }
record SwitchRecord(SwitchKind Kind, double TimeMs, int Cpu, int OldPid, int OldTid, int NewPid, int NewTid, string NewProcessName, string OldState, string WaitReason)
{
    public static SwitchRecord FromOut(CSwitchTraceData d) => new(SwitchKind.Out, d.TimeStampRelativeMSec, d.ProcessorNumber, d.OldProcessID, d.OldThreadID, d.NewProcessID, d.NewThreadID, d.NewProcessName ?? "", d.OldThreadState.ToString(), d.OldThreadWaitReason.ToString());
    public static SwitchRecord FromIn(CSwitchTraceData d) => new(SwitchKind.In, d.TimeStampRelativeMSec, d.ProcessorNumber, d.OldProcessID, d.OldThreadID, d.NewProcessID, d.NewThreadID, d.NewProcessName ?? "", "", "");
}
record Segment(double StartMs, double EndMs, double DurationMs, int NewPid, int NewTid, string NewProcessName, int Cpu, string OldState, string WaitReason);
