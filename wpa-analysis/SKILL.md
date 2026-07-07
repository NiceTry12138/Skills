---
name: wpa-analysis
description: Analyze Windows ETL traces from WPR/WPA for game CPU scheduling, TraceLogging events, custom providers, context switches, thread preemption, and spline slow-event diagnostics. Use when Codex needs to help create WPR profiles, collect ETL files with wpr, read ETL files with TraceProcessing/TraceEvent, or correlate custom TraceLogging events such as FSplineData/SplineSlow with process IDs, thread IDs, CPU context switches, and stacks.
---

# WPA Analysis

Use this skill to help collect and analyze Windows ETL traces for game performance issues, especially when a custom TraceLogging event marks a slow code path and the question is whether CPU scheduling/preemption caused the slowdown.

## Workflow

1. Confirm ETL contains both custom TraceLogging events and CPU scheduling data.
2. If no ETL exists, guide user to add provider instrumentation and collect with WPR.
3. For `FSplineData/SplineSlow` preemption checks, run `scripts/analyze-spline-etl.ps1` first.
4. Prefer TraceProcessing (`Microsoft.Windows.EventTracing.Processing.All`) for broad offline analysis.
5. Use TraceEvent (`Microsoft.Diagnostics.Tracing.TraceEvent`) when lower-level event streaming or raw CSwitch access is needed.
6. Avoid `tracerpt.exe` and `wpaexporter.exe` as primary analysis paths for preemption diagnosis; they are useful for quick CSV/XML export only.

## Required Capture

To answer "was this thread preempted and by whom", ETL must include:

- Custom provider events: `FSplineData` / `SplineSlow` or matching module-specific provider.
- CPU scheduling/context switch data from WPR CPU profile.
- Stacks if user asks "what did the replacement thread do".

If stacks are missing, report only process/thread switch relationships, not function-level work.

## Quick Analyzer

Use the bundled analyzer for the common question: "When `SplineSlow` fires, was `GameThread` switched out, and which thread ran instead?"

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\analyze-spline-etl.ps1 -EtlPath C:\trace\spline.etl
```

Override provider/event names when needed:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\analyze-spline-etl.ps1 -EtlPath C:\trace\spline.etl -OutputDir C:\trace\spline-proof -ProviderName FSplineData -EventName SplineSlow
```

The analyzer uses TraceEvent and prints matching slow events, event PID/TID/thread name, `[Time - WallUs, Time]` window, switch-out overlap, replacement PID/TID/process/thread, wait reason/state, and compute-bound vs scheduling/preemption verdict. It also writes `proof_report.md`, `spline_slow_events.csv`, `switch_out_segments.csv`, and `context_switch_raw.csv` to `-OutputDir` or to `spline-proof` beside the ETL file.

## Instrumentation Example

Provider name is stable module identity. GUID must be generated once and kept stable for the provider.

```cpp
TRACELOGGING_DEFINE_PROVIDER(
    g_MySplineProvider,
    "FSplineData",
    (0x11223344, 0x5566, 0x7788, 0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff, 0x00));
```

Register provider during game startup and unregister during shutdown:

```cpp
FWinADK FWinADK::Instance;

FWinADK::FWinADK()
{
    TraceLoggingRegister(g_MySplineProvider);
}

FWinADK::~FWinADK()
{
    TraceLoggingUnregister(g_MySplineProvider);
}
```

Emit event at slow path. Include PID/TID-correlatable fields where possible; ETW already records process/thread, but explicit fields make CSV/WPA inspection easier.

```cpp
TraceLoggingWrite(
    g_MySplineProvider,
    "SplineSlow",
    TraceLoggingUInt64(FrameId, "FrameId"),
    TraceLoggingUInt64(SplineId, "SplineId"),
    TraceLoggingUInt32(GetCurrentThreadId(), "ThreadId"),
    TraceLoggingFloat64(TotalUs, "WallUs"),
    TraceLoggingUInt64(DeltaCycles, "Cycles"));
```

Prefer Start/End when code can be changed:

```cpp
TraceLoggingWrite(g_MySplineProvider, "SplineStart",
    TraceLoggingUInt64(FrameId, "FrameId"),
    TraceLoggingUInt64(SplineId, "SplineId"));

// calculate spline

TraceLoggingWrite(g_MySplineProvider, "SplineEnd",
    TraceLoggingUInt64(FrameId, "FrameId"),
    TraceLoggingUInt64(SplineId, "SplineId"),
    TraceLoggingFloat64(TotalUs, "WallUs"));
```

## WPR Profile

Use `assets/FSplineData.wprp` as template. Its `EventProvider Name` must equal the provider GUID from `TRACELOGGING_DEFINE_PROVIDER`.

Important: `wpr -start` must reference `wprp!profile`, not just the `.wprp` path.

Collect ETL:

```bat
wpr -start CPU -start "S:\SP\ThroughtWork\WPA_WPRP\FSplineData.wprp!FSplineData.Verbose" -filemode
```

Stop capture:

```bat
wpr -stop C:\trace\spline.etl
```

If using an older profile named `MySpline`, command is:

```bat
wpr -start CPU -start "S:\SP\ThroughtWork\WPA_WPRP\FSplineData.wprp!MySpline.Verbose" -filemode
```

Remind user:

- Run terminal as Administrator when WPR requires it.
- Create `C:\trace` before stopping.
- Keep provider GUID stable across builds.
- Use `wpr -profiledetails "path\FSplineData.wprp!FSplineData.Verbose"` to validate profile parsing.

## Analysis Logic

Given a `SplineSlow` event with `ProcessId`, `ThreadId`, `Time`, and `WallUs`:

1. Build slow interval:
   - If Start/End exists, use exact interval.
   - Else use `[event.Time - WallUs, event.Time]`.
2. Find context switches near that interval where old or new thread equals the spline PID/TID.
3. For each switch-out, record the replacement new PID/TID.
4. Find the next switch-in of the original PID/TID by chronological thread state, not by same CPU.
5. Use wait reason, ready time, priority, and state to classify:
   - preempted by another runnable thread
   - voluntary wait/sleep/lock/IO
   - quantum end
   - unknown
6. If stacks exist, inspect sample/context-switch stacks for the replacement PID/TID during the switched-out window.
7. Output top offenders by total time overlapping slow interval.

Always match by PID + TID + time. Thread IDs are reused.

Important lesson from `spline.etl`: do not pair switch-out and switch-in by CPU. `GameThread` can migrate across cores, so same-CPU pairing can double count replacement threads and falsely report preemption. Track one pending switch-out per target thread, clamp duration to the slow-event interval, and end the segment at the next switch-in of that same PID/TID.

## Output Shape

For each slow event, report:

```text
SplineSlow: frame/spline/time/wall_us/process/thread
Interval source: StartEnd or WallUsBackfill
Original thread running time
Original thread switched-out time
Top replacement threads:
  process name / pid / tid / cpu / duration / wait reason / stack summary
Conclusion:
  likely CPU preemption | likely voluntary wait | likely compute-bound | insufficient data
```

## References

Read these only when needed:

- `references/wpr_capture.md`: detailed capture commands and `.wprp` notes.
- `references/event_schema.md`: expected TraceLogging event fields and interpretation.
- `scripts/etl-spline-analyzer/`: TraceEvent analyzer source for raw CSwitch correlation.

## Implementation Preference

Use C#/.NET TraceProcessing first for ETL analysis:

```text
NuGet: Microsoft.Windows.EventTracing.Processing.All
```

Use TraceEvent only when TraceProcessing cannot expose needed raw events.

