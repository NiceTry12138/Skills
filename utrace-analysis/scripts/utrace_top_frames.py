#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
utrace_top_frames.py —— 纯 Python 解析 .utrace，找出指定 timer 在某条 track 上
最耗时的 N 帧，输出 JSON。

依赖：
    pip install lz4

用法：
    python utrace_top_frames.py <utrace> --timer PrepareFillNPCDataByNum \
        [--top-n 10] [--track GameThread] [--output result.json]

实现概览（一次扫盘 + 流式 dispatch + 早期裁剪）：
    1. 读 magic / metadata / TransportVersion / ProtocolVersion；
    2. 切 TidPacket、按 LZ4 块解压，按 ThreadId 分桶；
    3. 解析 events 流（NewEvent / 重要事件类型注册）；
    4. 解析 importants 流（CpuProfiler.EventSpec / Logging.LogCategory 等）；
    5. 业务线程上**流式回调**——每事件解出来立即 dispatch，不存全量列表；
       6500w 个 GameThread 事件中，只有 target timer 的子树构造为
       TimingEvent 对象，其它 scope 仅用 (start_time, name) 浅栈跟踪。
    6. 帧来自 Misc.BeginFrame/EndFrame（或老式 BeginGameFrame）；
    7. 把命中 target timer 的子树按 start_time 落到帧上，
       按"帧内匹配 timer 累计 Duration"排序，输出 top-N。
    8. Log 取「在匹配 timer scope 时间窗内发生」的所有日志。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from bisect import bisect_left
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# 让脚本无论从哪里被调用都能找到位于父目录的 `utrace` 包。
_SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from utrace import protocol as P
from utrace.transport import TidPacketTransport, parse_header
from utrace.analyzer import Analyzer, TimingEvent, LogMessage, ThreadInfo, Frame


# ---- 时间格式 ---------------------------------------------------------------

def fmt_duration(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    us = seconds * 1_000_000.0
    if us < 1000.0:
        return f"{int(round(us))} µs"
    ms = seconds * 1000.0
    if ms < 1000.0:
        return f"{ms:.1f}ms"
    return f"{seconds:.3f}s"


def fmt_time_abs(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    minutes = int(seconds // 60)
    rem = seconds - minutes * 60
    if minutes > 0:
        return f"{minutes}m{rem:.7f}s"
    return f"{rem:.7f}s"


# ---- 主流程 -----------------------------------------------------------------

def load_and_analyze(utrace_path: Path, target_substr: Optional[str]) -> Analyzer:
    raw = utrace_path.read_bytes()
    print(f"[1/4] 读入 {len(raw) / 1024 / 1024:.1f} MiB", file=sys.stderr, flush=True)

    transport_version, protocol_version, body_offset = parse_header(raw)
    print(f"[1/4] TransportVersion={transport_version} ProtocolVersion={protocol_version}",
          file=sys.stderr, flush=True)
    if transport_version not in (P.TRANSPORT_TID_PACKET, P.TRANSPORT_TID_PACKET_SYNC):
        raise SystemExit(f"暂不支持 TransportVersion={transport_version}（仅 3/4 即 TidPacket / TidPacketSync）")
    if protocol_version not in (5, 6, 7):
        raise SystemExit(f"暂不支持 ProtocolVersion={protocol_version}（仅 5/6/7）")

    # ---- transport 切包 + LZ4 解压
    t0 = time.perf_counter()
    transport = TidPacketTransport(raw, body_offset)
    transport.parse_all()
    # raw 占 568MB，加上 transport.streams 解压后总和大概 1~2 GB；
    # 解压完即丢 raw 释放内存
    del raw
    print(f"[2/4] 切包 + 解压 完成，{len(transport.streams)} 条线程流，"
          f"sync={transport.sync_count}，耗时 {time.perf_counter() - t0:.1f}s",
          file=sys.stderr, flush=True)

    # ---- 协议层解析
    t0 = time.perf_counter()
    analyzer = Analyzer(target_substr=target_substr)
    analyzer.run_important(transport.get_stream(P.TID_EVENTS), protocol_version)
    analyzer.run_important(transport.get_stream(P.TID_IMPORTANTS), protocol_version)
    print(f"[3/4] importants 解析完成，{len(analyzer.timers)} timers, "
          f"{len(analyzer.log_specs)} log specs，耗时 {time.perf_counter() - t0:.1f}s",
          file=sys.stderr, flush=True)

    # 业务线程
    t0 = time.perf_counter()
    business_tids = sorted(
        tid for tid in transport.streams.keys()
        if tid >= P.TID_BIAS and len(transport.streams[tid]) > 0
    )
    for tid in business_tids:
        # 解完一条就把 transport 里的流释放掉，进一步控制内存
        stream = transport.get_stream(tid)
        analyzer.run_normal_thread(tid, stream)
        transport.streams[tid] = bytearray()
    print(f"[4/4] 业务线程解析完成，{len(business_tids)} 条线程，"
          f"耗时 {time.perf_counter() - t0:.1f}s", file=sys.stderr, flush=True)

    # 报告几个关键统计
    total_ev = sum(analyzer.thread_event_count.values())
    captured = sum(len(v) for v in analyzer.captured_per_thread.values())
    print(f"      Frames: GameThread={len(analyzer.frames_by_type[0])} "
          f"RenderThread={len(analyzer.frames_by_type[1])}; "
          f"scope events total={total_ev}; "
          f"captured «{target_substr}» trees={captured}",
          file=sys.stderr, flush=True)
    return analyzer


# ---- 帧筛选 -----------------------------------------------------------------

@dataclass
class FrameMatch:
    frame_index: int          # 1-based
    frame: Frame
    matches: List[TimingEvent]
    matched_total: float
    logs: List[LogMessage] = field(default_factory=list)


def collect_top_frames(analyzer: Analyzer, target_substr: str,
                       track_tid: int, track_frame_type: int,
                       top_n: int) -> List[FrameMatch]:
    frames = analyzer.frames_by_type[track_frame_type]
    if not frames:
        return []
    frame_starts = [f.start for f in frames]

    # 按 start_time 把 captured 落到帧
    bucket: dict[int, List[TimingEvent]] = {i: [] for i in range(len(frames))}
    for tree in analyzer.captured_per_thread.get(track_tid, []):
        # 二分找到 start <= tree.start 的最右帧
        i = bisect_left(frame_starts, tree.start) - 1
        if i < 0 or i >= len(frames):
            continue
        f = frames[i]
        if f.start <= tree.start <= f.end:
            bucket[i].append(tree)

    out: List[FrameMatch] = []
    for i, trees in bucket.items():
        if not trees:
            continue
        total = sum(t.duration for t in trees)
        out.append(FrameMatch(
            frame_index=i + 1,  # 1-based
            frame=frames[i], matches=trees, matched_total=total,
        ))
    out.sort(key=lambda x: x.matched_total, reverse=True)
    return out[:top_n]


def attach_logs(matches: List[FrameMatch], logs: List[LogMessage]) -> None:
    if not logs or not matches:
        return
    # 把 logs 按时间排序后用 bisect 找区间
    logs_sorted = sorted(logs, key=lambda l: l.time)
    times = [l.time for l in logs_sorted]
    for fm in matches:
        bucket: List[LogMessage] = []
        for tree in fm.matches:
            lo = bisect_left(times, tree.start)
            for j in range(lo, len(times)):
                if times[j] > tree.end:
                    break
                bucket.append(logs_sorted[j])
        fm.logs = bucket


# ---- JSON ------------------------------------------------------------------

def event_to_json(ev: TimingEvent) -> dict:
    return {
        "Name": ev.name,
        "useTime": fmt_duration(ev.duration),
        "Time": fmt_time_abs(ev.start),
        "calls": [event_to_json(c) for c in ev.children],
    }


def log_to_json(log: LogMessage) -> dict:
    return {
        "Time": fmt_time_abs(log.time),
        "Category": log.category,
        "Message": log.message,
        "File": log.file,
        "Line": log.line,
    }


def frame_to_json(fm: FrameMatch, needle: str) -> dict:
    return {
        "frame": fm.frame_index,
        "useTime": fmt_duration(fm.frame.end - fm.frame.start),
        "Time": fmt_time_abs(fm.frame.start),
        needle: {
            "calls": [event_to_json(m) for m in fm.matches],
        },
        "Log": [log_to_json(l) for l in fm.logs],
    }


# ---- entry ------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="纯 Python 解析 .utrace，输出指定 timer 在某条 track 上最耗时的 N 帧。",
    )
    p.add_argument("utrace", type=Path, help=".utrace 文件路径")
    p.add_argument("-t", "--timer", default=None,
                   help="timer 名字（部分匹配，例如 PrepareFillNPCDataByNum）；"
                        "传 --list-threads 时可以省略")
    p.add_argument("-n", "--top-n", type=int, default=10, help="返回耗时最久的 N 帧（默认 10）")
    p.add_argument("--track", default="GameThread",
                   help="track 名（即 thread name，默认 GameThread）")
    p.add_argument("-o", "--output", type=Path,
                   help="输出 JSON 路径；不传则打印到 stdout")
    p.add_argument("--list-threads", action="store_true",
                   help="列出当前 trace 里出现过的线程名字 + 该线程上的事件计数，"
                        "不做帧筛选；不需要 --timer")
    args = p.parse_args(argv)

    if not args.utrace.is_file():
        raise SystemExit(f"utrace 不存在: {args.utrace}")

    if not args.list_threads and not args.timer:
        raise SystemExit("非 --list-threads 模式必须传 --timer/-t")

    analyzer = load_and_analyze(args.utrace, args.timer if not args.list_threads else None)

    if args.list_threads:
        rows = []
        for tid, count in analyzer.thread_event_count.items():
            info = analyzer.threads.get(tid)
            rows.append({
                "tid": tid,
                "name": info.name if info else "",
                "events": count,
            })
        rows.sort(key=lambda r: -r["events"])
        text = json.dumps(rows, ensure_ascii=False, indent=2)
        if args.output:
            args.output.write_text(text, encoding="utf-8")
        else:
            print(text)
        return 0

    # 找 track tid + 帧 type
    tid = analyzer.find_thread_id_by_name(args.track)
    if tid is None:
        # 退而求其次：看哪个线程有最多事件
        if not analyzer.thread_event_count:
            raise SystemExit("解析后没有任何 timing event；可能是 utrace 没开 cpu 通道")
        cand = sorted(analyzer.thread_event_count.items(), key=lambda kv: -kv[1])[:5]
        names = ", ".join(
            f"{analyzer.threads.get(t, ThreadInfo(tid=t)).name or t}({c})"
            for t, c in cand
        )
        raise SystemExit(f"未找到 thread name «{args.track}»。事件最多的几条线程: {names}\n"
                         f"用 --list-threads 看完整列表。")

    # GameThread 默认对应 frame_type=0；RenderingThread 对应 1
    if args.track in ("RenderingThread", "RenderThread"):
        frame_type = 1
    else:
        frame_type = 0

    matches = collect_top_frames(analyzer, args.timer, tid, frame_type, args.top_n)
    attach_logs(matches, analyzer.log_messages)

    result = [frame_to_json(m, args.timer) for m in matches]
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"[done] 写入 {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
