#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_function.py —— 给 skill 用的"问题函数 + utrace 文件 → 结构化分析包"。

入口契约（外界系统调用本 skill 时给的两个必填项）：
    --utrace <path>       问题 utrace 文件
    --function <name>     有问题的函数名（部分匹配，区分大小写）

可选：
    --top-n N             返回耗时前 N 帧（默认 10）
    --track NAME          所在 track 名（默认 GameThread）
    --output PATH         JSON 输出路径（默认 utrace_analysis_<func>.json
                          落到 utrace 同目录）

输出：
    一份 JSON 文件，schema 与 utrace_top_frames.py 完全一致——
    见 README "输出 JSON 结构" 段落。

实现：
    本脚本只是 utrace_top_frames.py 的一层薄壳，把外界系统熟悉的字眼
    （function / 问题函数）映射到底层脚本的 timer 概念。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 复用 utrace_top_frames 的 main()，避免实现两套
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import utrace_top_frames  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(
        description="utrace skill 入口：问题函数 + utrace → 调用细节 / 耗时 / 时间窗内日志（JSON）",
    )
    p.add_argument("--utrace", required=True, type=Path,
                   help="问题 utrace 文件路径")
    p.add_argument("--function", required=True,
                   help="有问题的函数名（子串匹配，区分大小写，与 timer 名对应）")
    p.add_argument("--top-n", type=int, default=10,
                   help="返回耗时最久的 N 帧（默认 10）")
    p.add_argument("--track", default="GameThread",
                   help="问题函数所在 track（thread name），默认 GameThread")
    p.add_argument("--output", type=Path, default=None,
                   help="JSON 输出路径；不传时写到 utrace 同目录的 utrace_analysis_<func>.json")
    args = p.parse_args()

    if not args.utrace.is_file():
        raise SystemExit(f"utrace 不存在: {args.utrace}")

    # 输出路径：默认放在 utrace 旁边，避免污染 cwd
    if args.output is None:
        safe_func = "".join(c if c.isalnum() or c in "_-" else "_" for c in args.function)
        args.output = args.utrace.with_name(f"utrace_analysis_{safe_func}.json")

    # 转译参数到底层脚本
    sub_argv = [
        str(args.utrace),
        "--timer", args.function,
        "--top-n", str(args.top_n),
        "--track", args.track,
        "--output", str(args.output),
    ]
    rc = utrace_top_frames.main(sub_argv)
    if rc == 0:
        print(f"\n[ok] JSON 已写入: {args.output}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
