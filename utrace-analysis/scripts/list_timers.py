#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
list_timers.py —— 列出 utrace 里所有 timer 名（可选子串过滤）。

跑得快：只解析 events / importants 两条 small 流（10 MB 量级），不扫业务线程，
通常 5~10 秒返回。用来在 captured trees=0 时确认 function 名是否拼对。

用法：
    python list_timers.py <utrace> [--match SUBSTR] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from utrace import protocol as P
from utrace.transport import TidPacketTransport, parse_header
from utrace.analyzer import Analyzer


def main() -> int:
    p = argparse.ArgumentParser(description="列出 utrace 里全部 timer / 函数名（可选子串过滤）")
    p.add_argument("utrace", type=Path, help=".utrace 文件路径")
    p.add_argument("--match", default=None,
                   help="只列名字含此子串的 timer（不区分大小写）")
    p.add_argument("--limit", type=int, default=200,
                   help="最多输出多少条（默认 200，避免动辄上万行）")
    p.add_argument("--json", action="store_true",
                   help="按 JSON 数组输出而不是人读表格")
    args = p.parse_args()

    if not args.utrace.is_file():
        raise SystemExit(f"utrace 不存在: {args.utrace}")

    raw = args.utrace.read_bytes()
    tv, pv, off = parse_header(raw)
    if tv not in (P.TRANSPORT_TID_PACKET, P.TRANSPORT_TID_PACKET_SYNC):
        raise SystemExit(f"暂不支持 TransportVersion={tv}")
    if pv not in (5, 6, 7):
        raise SystemExit(f"暂不支持 ProtocolVersion={pv}")

    transport = TidPacketTransport(raw, off)
    transport.parse_all()
    del raw

    analyzer = Analyzer()
    analyzer.run_important(transport.get_stream(P.TID_EVENTS), pv)
    analyzer.run_important(transport.get_stream(P.TID_IMPORTANTS), pv)

    needle = args.match.lower() if args.match else None
    rows = []
    for ti in analyzer.timers.values():
        if needle and needle not in ti.name.lower():
            continue
        rows.append({
            "spec_id": ti.spec_id,
            "name": ti.name,
            "file": ti.file or "",
            "line": ti.line,
        })
    rows.sort(key=lambda r: r["name"])

    truncated = False
    if len(rows) > args.limit:
        truncated = True
        rows = rows[: args.limit]

    if args.json:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"# {len(rows)} timers" + ("（已截断）" if truncated else ""))
        for r in rows:
            loc = f"  @ {r['file']}:{r['line']}" if r["file"] else ""
            print(f"  {r['name']}{loc}")

    if truncated:
        print(f"\n# ... 输出已截断到 {args.limit} 条；--limit 调大可看更多",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
