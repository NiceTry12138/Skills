#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
summarize_frames.py —— 把 analyze_function.py 输出的 {meta, frames} JSON
压成一份人读摘要（<10 KB），重点是 meta 里没有的两件事：

  1. Top-N 慢帧的子调用聚合：单帧内每个子 timer 的 count + sum_us
  2. 全局子 timer 热点：跨所有捕获帧打通统计，找反复出现的子 scope

外加 meta 关键数字 + 每帧 Log 频次。

用法：
    python summarize_frames.py <analysis.json> [--top-frames 5] [--top-subs 15]

不接受 .utrace 文件——它读的是 analyze_function.py 已经写好的 JSON。
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Windows 默认 codepage 不是 UTF-8，强制 stdout 用 UTF-8 输出，避免 "µ" / 中文乱码
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


# ---- 时长字符串 → 微秒 -----------------------------------------------------
# JSON 里时长已经是 "73.5ms" / "477 µs" / "1.20s" 这种人读形式，重新解析回数值

_DUR_RE = re.compile(r"([\d.]+)\s*([a-zµ]+)")


def parse_us(s: str) -> float:
    if not s:
        return 0.0
    m = _DUR_RE.match(s.strip().replace("\xa0", " "))
    if not m:
        return 0.0
    v, unit = float(m.group(1)), m.group(2)
    if unit in ("µs", "us"):
        return v
    if unit == "ms":
        return v * 1000.0
    if unit == "s":
        return v * 1_000_000.0
    if unit == "ns":
        return v / 1000.0
    return 0.0


def fmt_us(us: float) -> str:
    if us >= 1_000_000:
        return f"{us / 1_000_000:.2f}s"
    if us >= 1000:
        return f"{us / 1000:.2f}ms"
    return f"{us:.0f}us"


# ---- 子调用聚合 ------------------------------------------------------------

def walk_calls(calls, agg: dict) -> None:
    """递归展平 call tree，按 Name 聚合 count + sum_us（含根 timer 自身）。"""
    for c in calls:
        a = agg[c["Name"]]
        a["count"] += 1
        a["sum_us"] += parse_us(c.get("useTime", "0us"))
        walk_calls(c.get("calls", []), agg)


def new_agg() -> dict:
    return defaultdict(lambda: {"count": 0, "sum_us": 0.0})


# ---- 输出 ------------------------------------------------------------------

def print_meta(meta: dict) -> None:
    fn = meta.get("function", "?")
    track = meta.get("track", "?")
    print(f"=== Function: {fn}    Track: {track} ===")
    print(f"  total_calls          = {meta.get('total_calls', '?')}")
    print(f"  hit_frames           = {meta.get('hit_frames', '?')}"
          f"  / {meta.get('total_frames_on_track', '?')} frames on track")

    def _fmt_stats(d):
        if not d:
            return "(none)"
        return (f"min={fmt_us(d['min'])} p50={fmt_us(d['p50'])} "
                f"p90={fmt_us(d['p90'])} p99={fmt_us(d['p99'])} "
                f"max={fmt_us(d['max'])} mean={fmt_us(d['mean'])}")

    print(f"  per-call             {_fmt_stats(meta.get('duration_us_per_call'))}")
    print(f"  per-frame accum      {_fmt_stats(meta.get('duration_us_per_frame_accum'))}")
    print(f"  hit-frame total      {_fmt_stats(meta.get('frame_total_us_when_hit'))}")
    print(f"  top_n_returned       = {meta.get('top_n_returned', '?')}"
          f"  (covers_all_hits={meta.get('top_n_covers_all_hits', '?')})")
    print()


def print_per_frame(frames: list, fn: str, top_frames: int, top_subs: int) -> None:
    if not frames:
        print("(no frames captured)")
        return
    print(f"=== TOP-{min(top_frames, len(frames))} slow frames (sub-timer breakdown) ===\n")
    for f in frames[:top_frames]:
        calls = f.get(fn, {}).get("calls", [])
        tram_us = sum(parse_us(c.get("useTime", "0us")) for c in calls)
        n_calls = len(calls)
        print(f"--- frame {f.get('frame', '?')} | "
              f"frame {f.get('useTime', '?')} | "
              f"{fn} total {fmt_us(tram_us)} | "
              f"{n_calls} call(s) | start {f.get('Time', '?')} ---")

        agg = new_agg()
        walk_calls(calls, agg)
        items = sorted(agg.items(), key=lambda kv: kv[1]["sum_us"], reverse=True)
        print(f"  {'sub_timer':<52} {'sum':>10} {'count':>6}")
        for name, v in items[:top_subs]:
            print(f"  {name[:52]:<52} {fmt_us(v['sum_us']):>10} {v['count']:>6}")

        # Top-level (root timer 直接子节点)
        if calls:
            print("  Top-level call breakdown:")
            top_calls = sorted(
                calls,
                key=lambda c: parse_us(c.get("useTime", "0us")),
                reverse=True,
            )
            for c in top_calls[:6]:
                print(f"    - {c.get('Name', '?')}: {c.get('useTime', '?')}")

        # Logs
        logs = f.get("Log", [])
        if logs:
            print(f"  Log entries in window: {len(logs)} (showing first 5)")
            for lg in logs[:5]:
                msg = (lg.get("Message") or "")[:160]
                print(f"    [{lg.get('Category', '?')}] {msg}")
        print()


def print_global_hotspots(frames: list, fn: str, top_subs: int) -> None:
    print(f"=== GLOBAL hot sub-timers across {len(frames)} captured frames (sum) ===")
    global_agg = new_agg()
    for f in frames:
        walk_calls(f.get(fn, {}).get("calls", []), global_agg)
    items = sorted(global_agg.items(), key=lambda kv: kv[1]["sum_us"], reverse=True)
    print(f"  {'sub_timer':<52} {'sum_total':>12} {'count':>10}")
    for name, v in items[:top_subs]:
        print(f"  {name[:52]:<52} {fmt_us(v['sum_us']):>12} {v['count']:>10}")
    print()


def print_log_summary(frames: list) -> None:
    cat_counter = Counter()
    msg_counter = Counter()
    total = 0
    for f in frames:
        for lg in f.get("Log", []):
            total += 1
            cat_counter[lg.get("Category", "?")] += 1
            # 只取消息前 80 字做 key——同模板的日志变量部分通常在后面
            msg_counter[(lg.get("Message") or "")[:80]] += 1
    if total == 0:
        return
    print(f"=== Log frequency across captured frames ({total} entries) ===")
    print("  By Category:")
    for cat, n in cat_counter.most_common(10):
        print(f"    {cat:<32} {n:>8}")
    print("  Top message templates (first 80 chars):")
    for tpl, n in msg_counter.most_common(8):
        if n < 2:
            break
        print(f"    [{n:>4}x] {tpl}")
    print()


# ---- entry -----------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Summarize {meta, frames} JSON from analyze_function.py.",
    )
    p.add_argument("json_path", type=Path,
                   help="analyze_function.py 输出的 JSON 文件")
    p.add_argument("--top-frames", type=int, default=5,
                   help="逐帧明细数量上限（默认 5）")
    p.add_argument("--top-subs", type=int, default=15,
                   help="子 timer 聚合行数上限（默认 15）")
    args = p.parse_args(argv)

    if not args.json_path.is_file():
        raise SystemExit(f"JSON 不存在: {args.json_path}")

    data = json.loads(args.json_path.read_text(encoding="utf-8"))

    # 兼容两种结构：新版 {meta, frames}；旧版直接是 list
    if isinstance(data, dict) and "meta" in data:
        meta = data["meta"]
        frames = data.get("frames", [])
        fn = meta.get("function") or _guess_fn(frames)
    elif isinstance(data, list):
        meta = None
        frames = data
        fn = _guess_fn(frames)
    else:
        raise SystemExit("JSON 结构不识别：既不是 {meta, frames}，也不是 list")

    if not fn:
        raise SystemExit("无法从 JSON 推断 function 名（meta.function 缺失且 frames 为空）")

    if meta:
        print_meta(meta)
    else:
        print(f"=== Function: {fn} (no meta block — old-format JSON) ===\n")

    print_per_frame(frames, fn, args.top_frames, args.top_subs)
    print_global_hotspots(frames, fn, args.top_subs)
    print_log_summary(frames)
    return 0


def _guess_fn(frames: list) -> str:
    """旧版 JSON 没 meta；从 frames[0] 里找一个非 frame/useTime/Time/Log 的 key。"""
    if not frames:
        return ""
    reserved = {"frame", "useTime", "Time", "Log"}
    for k in frames[0].keys():
        if k not in reserved:
            return k
    return ""


if __name__ == "__main__":
    sys.exit(main())
