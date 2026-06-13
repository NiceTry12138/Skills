# -*- coding: utf-8 -*-
"""
把"扁平 ParsedEvent 流"转成 Insights 等价的领域数据：
- TimerInfo（CpuProfiler EventSpec）
- TimingEvent（每条线程一棵作用域树）
- LogCategory / LogMessageSpec / LogMessage
- ThreadInfo
- Frame（GameThread / RenderThread / 其它）

只覆盖 Insights 视图我们要用到的 4 个 logger：
  CpuProfiler / Cpu / Misc / Logging / $Trace。
其它 logger 一律忽略，省内存。

对应 C++：CpuProfilerTraceAnalysis.cpp / LogTraceAnalysis.cpp / MiscTraceAnalysis.cpp /
        Engine.cpp 内部 FTraceAnalyzer。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import protocol as P
from .events import (
    ParsedEvent, parse_important_stream, walk_normal_stream,
    get_value, get_string, get_attachment,
)
from .types import EventType, TypeRegistry


# ---- 数据模型 ---------------------------------------------------------------

@dataclass
class TimerInfo:
    timer_id: int
    spec_id: int
    name: str
    file: Optional[str] = None
    line: int = 0
    is_gpu: bool = False


@dataclass
class TimingEvent:
    timer_id: int
    name: str
    start: float            # 秒
    end: float              # 秒
    children: List["TimingEvent"] = field(default_factory=list)
    # 仅 capture root（命中 target_substr 的最外层 scope）会填：
    # 该 scope 在线程调用栈上的祖先 timer 名，从外到内（不含自己）。
    # 子节点天然为空——它的祖先已经在 JSON 的嵌套 calls 里能看到。
    call_from: List[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class LogCategory:
    pointer: int
    name: str = ""
    default_verbosity: int = 0


@dataclass
class LogMessageSpec:
    log_point: int
    category: Optional[LogCategory] = None
    file: str = ""
    line: int = 0
    format_string: str = ""
    verbosity: int = 0


@dataclass
class LogMessage:
    time: float
    category: str
    message: str
    file: str
    line: int


@dataclass
class ThreadInfo:
    tid: int                # transport tid（ETransportTid::Bias 之上）
    system_id: int = 0      # 来自 $Trace.ThreadInfo
    sort_hint: int = 0
    name: str = ""
    group: str = ""


@dataclass
class Frame:
    frame_type: int   # 0=Game, 1=Render
    index: int
    start: float
    end: float


# ---- format-args formatter (与 C++ FFormatArgsHelper 保持等价) -------------

# 类型 code = (category << 6) | size
_FA_INTEGER = 1 << 6
_FA_FLOAT   = 2 << 6
_FA_STRING  = 3 << 6
_FA_CATEGORY_MASK = 0xC0
_FA_SIZE_MASK     = 0x3F


def _format_log_message(format_string: str, args_blob: bytes) -> str:
    """将 Logging.LogMessage 的 FormatArgs 重放成最终字符串。

    格式约定（FFormatArgsTrace::EncodeArguments）：
      uint8 ArgumentCount
      uint8 TypeCodes[ArgumentCount]    每条 = (category<<6) | sizeOrCharSize
      uint8 PayloadBytes[]              每条按其 TypeCode 解码
        - integer：直接 little-endian uint
        - float  ：4 -> float, 8 -> double
        - string ：sizeOrCharSize=1 → 0 结尾的 ANSI；2 → UTF-16; 4 → UTF-32
    """
    if not args_blob:
        return format_string

    pos = 0
    arg_count = args_blob[pos]
    pos += 1
    if arg_count == 0:
        return format_string

    type_codes = args_blob[pos : pos + arg_count]
    pos += arg_count
    payload_pos = pos
    end = len(args_blob)

    def _next_arg(idx: int):
        """读出 arg idx 的 Python 值，并推进 payload_pos。"""
        nonlocal payload_pos
        code = type_codes[idx]
        cat = code & _FA_CATEGORY_MASK
        size = code & _FA_SIZE_MASK

        if cat == _FA_INTEGER:
            val = int.from_bytes(args_blob[payload_pos : payload_pos + size],
                                 "little", signed=False)
            payload_pos += size
            return val
        if cat == _FA_FLOAT:
            if size == 4:
                v = struct.unpack_from("<f", args_blob, payload_pos)[0]
            else:
                v = struct.unpack_from("<d", args_blob, payload_pos)[0]
            payload_pos += size
            return v
        if cat == _FA_STRING:
            if size == 1:
                # ANSI 0 结尾
                start = payload_pos
                while payload_pos < end and args_blob[payload_pos] != 0:
                    payload_pos += 1
                s = args_blob[start:payload_pos].decode("utf-8", errors="replace")
                payload_pos += 1
                return s
            if size == 2:
                start = payload_pos
                while payload_pos + 1 < end and args_blob[payload_pos] | args_blob[payload_pos + 1]:
                    payload_pos += 2
                s = args_blob[start:payload_pos].decode("utf-16-le", errors="replace")
                payload_pos += 2
                return s
            # size 4: UTF-32
            start = payload_pos
            while payload_pos + 3 < end and any(args_blob[payload_pos:payload_pos + 4]):
                payload_pos += 4
            s = args_blob[start:payload_pos].decode("utf-32-le", errors="replace")
            payload_pos += 4
            return s
        # 未知类型，往后跳一字节避免死循环
        payload_pos += 1
        return ""

    # 简化的 printf：只识别 % + 标志 + 宽度 + .精度 + 长度 + 类型字符；
    # 不做精确的 width/precision 处理（正确性 > 完美还原），失败时退化为 str(val)。
    out: List[str] = []
    i = 0
    arg_idx = 0
    flen = len(format_string)
    while i < flen:
        ch = format_string[i]
        if ch != '%':
            out.append(ch)
            i += 1
            continue
        # % 开始
        spec_start = i
        i += 1
        # 跳标志
        while i < flen and format_string[i] in "-+ #0":
            i += 1
        # 宽度
        while i < flen and format_string[i].isdigit():
            i += 1
        # 精度
        if i < flen and format_string[i] == '.':
            i += 1
            while i < flen and format_string[i].isdigit():
                i += 1
        # 长度
        while i < flen and format_string[i] in "hljztL":
            i += 1
        if i >= flen:
            out.append(format_string[spec_start:])
            break
        conv = format_string[i]
        i += 1
        if conv == '%':
            out.append('%')
            continue

        if arg_idx >= arg_count:
            out.append(format_string[spec_start:i])
            continue

        val = _next_arg(arg_idx)
        arg_idx += 1
        try:
            if conv in "diu":
                # 强转 int
                if isinstance(val, str):
                    val = 0
                v = int(val) & 0xFFFFFFFFFFFFFFFF
                if conv != 'u' and v >= (1 << 63):
                    v -= (1 << 64)
                out.append(str(v))
            elif conv in "xX":
                v = int(val) if not isinstance(val, str) else 0
                s = format(v & 0xFFFFFFFFFFFFFFFF, 'x' if conv == 'x' else 'X')
                out.append(s)
            elif conv == 'o':
                v = int(val) if not isinstance(val, str) else 0
                out.append(format(v & 0xFFFFFFFFFFFFFFFF, 'o'))
            elif conv == 'p':
                out.append(f"0x{int(val):X}")
            elif conv == 'c':
                out.append(chr(int(val) & 0xFFFF))
            elif conv in "fF":
                out.append(f"{float(val):f}")
            elif conv in "eE":
                out.append(f"{float(val):{conv}}")
            elif conv in "gG":
                out.append(f"{float(val):{conv}}")
            elif conv in "sS":
                out.append(str(val))
            else:
                out.append(format_string[spec_start:i])
        except Exception:
            out.append(str(val))

    return "".join(out)


# ---- 核心分析器 -------------------------------------------------------------

class Analyzer:
    """
    一次性扫描 + 状态聚合。流程：
        1. parse_important_stream(events_tid 流) → 注册全部 NewEvent / 解码 important
        2. parse_important_stream(importants_tid 流) → 解码 important（同上但内容不同）
        3. 对每条业务线程：parse_normal_stream → 转化为 timing / scope 事件
    """

    def __init__(self, target_substr: Optional[str] = None):
        """target_substr 用于在解析阶段就丢弃不相关的 scope 子树，节省 95% 内存。
        如果传 None（list-threads 模式），则只统计 scope 数量、不构 timing tree。"""
        self.registry = TypeRegistry()
        self.target_substr = target_substr

        # timing
        self.spec_to_timer: Dict[int, int] = {}
        self.timers: Dict[int, TimerInfo] = {}
        # threadId -> 已闭合的、命中 target_substr 的子树（多个；按出现顺序）
        # list-threads 模式下，这里改放一个轻量整数 = 该线程的事件计数
        self.captured_per_thread: Dict[int, List[TimingEvent]] = {}
        self.thread_event_count: Dict[int, int] = {}
        self.thread_state: Dict[int, "_ThreadTimingState"] = {}

        # log
        self.log_categories: Dict[int, LogCategory] = {}
        self.log_specs: Dict[int, LogMessageSpec] = {}
        self.log_messages: List[LogMessage] = []

        # frames
        self.frames_by_type: Dict[int, List[Frame]] = {0: [], 1: []}
        self._last_frame_cycle = [0, 0]
        self._frame_open_start: Dict[int, float] = {}

        # threads
        self.threads: Dict[int, ThreadInfo] = {}

        # timing base
        self.base_cycle: int = 0
        self.cycles_per_second: int = 1
        self.inv_cycles: float = 1.0

        # protocol version（在 run() 中由调用方注入）
        self.protocol_version: int = 5

    # -- timing helpers ------------------------------------------------------

    def cycles_to_seconds(self, cycles: int) -> float:
        return float(cycles - self.base_cycle) * self.inv_cycles

    def cycles_to_seconds_abs(self, cycles_abs: int) -> float:
        # 与 EventTime::AsSeconds(c) 相同：相对 BaseTimestamp
        return self.cycles_to_seconds(cycles_abs)

    # ----------------------------------------------------------------------

    def find_thread_id_by_name(self, name: str) -> Optional[int]:
        for tid, info in self.threads.items():
            if info.name == name:
                return tid
        return None

    # -- 主入口 ---------------------------------------------------------------

    def run_important(self, stream: bytes, version: int) -> None:
        self.protocol_version = version
        events = parse_important_stream(stream, self.registry, version)
        for ev in events:
            self._dispatch_important(ev)

    # 业务线程只关心这几个 (logger, name) 组合。命中 → 走 dispatch；不命中 → 跳过。
    _BUSINESS_KEYS = {
        ("CpuProfiler", "EventBatchV2"),
        ("CpuProfiler", "EventBatch"),
        ("CpuProfiler", "EndCapture"),
        ("CpuProfiler", "EndThread"),
        ("Logging", "LogMessage"),
        ("Misc", "BeginFrame"),
        ("Misc", "EndFrame"),
        ("Misc", "BeginGameFrame"),
        ("Misc", "EndGameFrame"),
        ("Misc", "BeginRenderFrame"),
        ("Misc", "EndRenderFrame"),
        # 业务线程上有时也会发 ThreadInfo（线程刚创建时把自己的名字 trace 出来）
        ("$Trace", "ThreadInfo"),
        ("$Trace", "ThreadTiming"),
    }

    def run_normal_thread(self, tid: int, stream: bytes) -> None:
        """tid 是 transport thread id（>= TID_BIAS）。流式回调，不在内存中
        构造完整事件列表（GameThread 在大 trace 里 6000w+ 事件，全列表会爆内存）。"""
        # 提前把 uid -> EventType 缓存，避免回调里每次 dict 查找
        type_cache: Dict[int, Optional[EventType]] = {}

        def on_event(uid: int, payload_view: memoryview, aux, serial: int) -> None:
            et = type_cache.get(uid)
            if et is None:
                et = self.registry.get(uid)
                # 同一个 uid 多次出现，决定一次：是不是我们关心的事件
                key = (et.logger, et.name) if et else None
                if key in self._BUSINESS_KEYS:
                    type_cache[uid] = et
                else:
                    type_cache[uid] = False  # 用 False 标记"无视"
                    return
            elif et is False:
                return
            self._dispatch_business(et, bytes(payload_view), aux, tid)

        walk_normal_stream(stream, self.registry, self.protocol_version, on_event)

    # -- important 调度 -------------------------------------------------------

    def _dispatch_important(self, ev: ParsedEvent) -> None:
        et = self.registry.get(ev.uid)
        if et is None:
            return
        logger = et.logger
        name = et.name

        # $Trace.NewTrace / Timing：记录 cycle 基准
        if logger == "$Trace":
            if name in ("NewTrace", "Timing"):
                start_cycle = get_value(et, ev.payload, "StartCycle")
                cycle_freq  = get_value(et, ev.payload, "CycleFrequency")
                if start_cycle is not None and cycle_freq:
                    self.base_cycle = int(start_cycle)
                    self.cycles_per_second = int(cycle_freq)
                    self.inv_cycles = 1.0 / float(cycle_freq)
            elif name == "ThreadInfo":
                self._on_thread_info(et, ev)
            return

        if logger == "CpuProfiler":
            if name == "EventSpec":
                self._on_cpu_event_spec(et, ev)
            return

        if logger == "Logging":
            if name == "LogCategory":
                self._on_log_category(et, ev)
            elif name == "LogMessageSpec":
                self._on_log_message_spec(et, ev)
            return

        # 其它 important（GpuProfiler / Memory ...）忽略

    # -- normal 调度（已经过 _BUSINESS_KEYS 过滤的事件） --------------------

    def _dispatch_business(self, et: EventType, payload: bytes,
                           aux: Optional[Dict[int, bytes]], tid: int) -> None:
        logger = et.logger
        name = et.name
        if logger == "$Trace":
            if name == "ThreadInfo":
                # ThreadInfo 在业务线程上发：tid 字段里没 ThreadId，就用上下文 tid
                self._on_thread_info_business(et, payload, aux, tid)
            return
        if logger == "CpuProfiler":
            if name == "EventBatchV2":
                self._on_cpu_event_batch(et, payload, aux, tid, version_v2=True)
            elif name == "EventBatch":
                self._on_cpu_event_batch(et, payload, aux, tid, version_v2=False)
            elif name == "EndCapture":
                self._on_cpu_event_batch(et, payload, aux, tid, version_v2=False, end=True)
            elif name == "EndThread":
                self._on_cpu_end_thread(et, payload, tid)
            return
        if logger == "Logging" and name == "LogMessage":
            self._on_log_message(et, payload, aux)
            return
        if logger == "Misc":
            if name in ("BeginGameFrame", "EndGameFrame", "BeginRenderFrame", "EndRenderFrame"):
                self._on_frame_event_legacy(et, payload, name)
            elif name in ("BeginFrame", "EndFrame"):
                self._on_frame_event_v2(et, payload, name)

    # -- $Trace --------------------------------------------------------------

    def _on_thread_info(self, et: EventType, ev: ParsedEvent):
        tid_field = get_value(et, ev.payload, "ThreadId", default=None)
        if tid_field is None:
            return
        info = self.threads.setdefault(tid_field, ThreadInfo(tid=tid_field))
        info.system_id = get_value(et, ev.payload, "SystemId", default=0) or 0
        info.sort_hint = get_value(et, ev.payload, "SortHint", default=0) or 0
        name = get_string(et, ev.payload, ev.aux, "Name")
        if name is not None:
            info.name = name

    def _on_thread_info_business(self, et: EventType, payload: bytes,
                                 aux: Optional[Dict[int, bytes]], tid: int):
        """业务线程上的 ThreadInfo（事件没有 ThreadId 字段时用上下文 tid）。"""
        tid_field = get_value(et, payload, "ThreadId", default=None)
        target_tid = tid_field if tid_field is not None else tid
        info = self.threads.setdefault(target_tid, ThreadInfo(tid=target_tid))
        info.system_id = get_value(et, payload, "SystemId", default=0) or 0
        info.sort_hint = get_value(et, payload, "SortHint", default=0) or 0
        name = get_string(et, payload, aux, "Name")
        if name is not None:
            info.name = name

    # -- CpuProfiler ---------------------------------------------------------

    def _on_cpu_event_spec(self, et: EventType, ev: ParsedEvent):
        spec_id = get_value(et, ev.payload, "Id")
        if spec_id is None:
            return
        # name 既可能在 aux Name 字段，也可能在 attachment；优先 aux
        name = get_string(et, ev.payload, ev.aux, "Name")
        if name is None:
            char_size = get_value(et, ev.payload, "CharSize", default=0) or 0
            attachment = get_attachment(et, ev.payload)
            if char_size == 1:
                name = attachment.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
            elif char_size in (0, 2):
                name = attachment.decode("utf-16-le", errors="replace").rstrip("\x00")
            else:
                name = f"<invalid {spec_id}>"
        if not name:
            name = f"<noname {spec_id}>"

        file = get_string(et, ev.payload, ev.aux, "File")
        line = get_value(et, ev.payload, "Line", default=0) or 0

        if spec_id in self.spec_to_timer:
            timer_id = self.spec_to_timer[spec_id]
            info = self.timers[timer_id]
            info.name = name
            info.file = file
            info.line = line
            return

        timer_id = len(self.timers)
        self.timers[timer_id] = TimerInfo(
            timer_id=timer_id, spec_id=spec_id, name=name, file=file, line=line,
        )
        self.spec_to_timer[spec_id] = timer_id

    def _get_or_make_thread_state(self, tid: int) -> "_ThreadTimingState":
        st = self.thread_state.get(tid)
        if st is None:
            st = _ThreadTimingState(tid=tid)
            self.thread_state[tid] = st
            self.captured_per_thread[tid] = []
            self.thread_event_count[tid] = 0
        return st

    def _on_cpu_event_batch(self, et: EventType, payload: bytes,
                            aux: Optional[Dict[int, bytes]], tid: int, *,
                            version_v2: bool, end: bool = False):
        if not aux:
            return
        data_idx = et.field_by_name.get("Data", -1)
        data = aux.get(data_idx)
        if data is None:
            return
        st = self._get_or_make_thread_state(tid)
        if version_v2:
            self._process_buffer_v2(st, data)
        else:
            self._process_buffer_v1(st, data)
        if end:
            self._close_all_scopes(st, float("inf"))

    def _on_cpu_end_thread(self, et: EventType, payload: bytes, tid: int):
        st = self._get_or_make_thread_state(tid)
        cycle = get_value(et, payload, "Cycle", default=st.last_cycle) or st.last_cycle
        if cycle and cycle > 0:
            self._close_all_scopes(st, self.cycles_to_seconds(cycle))
        st.last_cycle = -1

    def _close_all_scopes(self, st: "_ThreadTimingState", end_time: float):
        while st.depth_stack:
            self._end_scope(st, end_time)

    # ---- CPU profiler decode（与 ProcessBufferV2 等价） ------------------

    def _process_buffer_v2(self, st: "_ThreadTimingState", buf: bytes):
        """对应 ProcessBufferV2（CpuProfilerTraceAnalysis.cpp:415）"""
        pos = 0
        end = len(buf)
        last_cycle = st.last_cycle if st.last_cycle != -1 else 0

        while pos < end:
            decoded, pos = _decode_7bit(buf, pos)
            actual_cycle = decoded >> 2
            if actual_cycle < last_cycle:
                actual_cycle += last_cycle

            if decoded & 2:
                # CoroTask 相关（开始/结束 coroutine + 嵌套 timer 深度）。
                # 我们的需求里 PrepareFillNPCData 不太可能是 coroutine，
                # 但为了保持 scope 栈一致，仍然把这类事件转成 begin/end 占位。
                if decoded & 1:
                    _coro_id, pos = _decode_7bit(buf, pos)
                    nested_depth, pos = _decode_7bit(buf, pos)
                    actual_time = self.cycles_to_seconds(actual_cycle)
                    self._begin_scope(st, actual_time, "<Coroutine>")
                    for _ in range(nested_depth):
                        self._begin_scope(st, actual_time, "<unknown>")
                else:
                    nested_depth, pos = _decode_7bit(buf, pos)
                    actual_time = self.cycles_to_seconds(actual_cycle)
                    for _ in range(nested_depth):
                        self._end_scope(st, actual_time)
                    self._end_scope(st, actual_time)
            else:
                actual_time = self.cycles_to_seconds(actual_cycle)
                if decoded & 1:
                    spec_id, pos = _decode_7bit(buf, pos)
                    timer_id = self.spec_to_timer.get(spec_id)
                    name = self.timers[timer_id].name if timer_id is not None else f"<spec {spec_id}>"
                    self._begin_scope(st, actual_time, name, timer_id=timer_id)
                else:
                    self._end_scope(st, actual_time)

            last_cycle = actual_cycle

        st.last_cycle = last_cycle

    def _process_buffer_v1(self, st: "_ThreadTimingState", buf: bytes):
        pos = 0
        end = len(buf)
        last_cycle = st.last_cycle if st.last_cycle != -1 else 0

        while pos < end:
            decoded, pos = _decode_7bit(buf, pos)
            actual_cycle = decoded >> 1
            if actual_cycle < last_cycle:
                actual_cycle += last_cycle
            actual_time = self.cycles_to_seconds(actual_cycle)
            if decoded & 1:
                spec_id, pos = _decode_7bit(buf, pos)
                timer_id = self.spec_to_timer.get(spec_id)
                name = self.timers[timer_id].name if timer_id is not None else f"<spec {spec_id}>"
                self._begin_scope(st, actual_time, name, timer_id=timer_id)
            else:
                self._end_scope(st, actual_time)
            last_cycle = actual_cycle

        st.last_cycle = last_cycle

    def _begin_scope(self, st: "_ThreadTimingState", t: float, name: str, *, timer_id: Optional[int] = None):
        st.depth_stack.append((t, name))
        self.thread_event_count[st.tid] = self.thread_event_count.get(st.tid, 0) + 1

        target = self.target_substr
        if target is None:
            return  # list-threads 模式：仅计数，不构造对象

        is_root_match = (st.capture_root_depth < 0 and target in name)
        if is_root_match:
            st.capture_root_depth = len(st.depth_stack) - 1

        if st.capture_root_depth >= 0:
            ev = TimingEvent(
                timer_id=timer_id if timer_id is not None else -1,
                name=name, start=t, end=t,
            )
            if is_root_match:
                # depth_stack 已经把当前 scope 在 _begin_scope 开头 push 进去了，
                # 所以"祖先"是 depth_stack[:-1]——即捕获 root 之前的所有外层 scope。
                ev.call_from = [n for (_s, n) in st.depth_stack[:-1]]
            if st.capture_stack:
                st.capture_stack[-1].children.append(ev)
            st.capture_stack.append(ev)

    def _end_scope(self, st: "_ThreadTimingState", t: float):
        if not st.depth_stack:
            return
        st.depth_stack.pop()

        target = self.target_substr
        if target is None:
            return

        if not st.capture_stack:
            return
        ev = st.capture_stack.pop()
        ev.end = t

        # 如果这是 capture root（最外那一层），落到 captured_subtrees
        if len(st.depth_stack) <= st.capture_root_depth:
            st.capture_root_depth = -1
            self.captured_per_thread[st.tid].append(ev)

    # -- Logging -------------------------------------------------------------

    def _on_log_category(self, et: EventType, ev: ParsedEvent):
        ptr = get_value(et, ev.payload, "CategoryPointer")
        if ptr is None:
            return
        cat = self.log_categories.setdefault(ptr, LogCategory(pointer=ptr))
        # 老 trace 把 Name 放在 attachment（TCHAR），新 trace 在 aux Name 字段
        name = get_string(et, ev.payload, ev.aux, "Name")
        if name is None:
            attachment = get_attachment(et, ev.payload)
            name = attachment.decode("utf-16-le", errors="replace").rstrip("\x00")
        cat.name = name
        cat.default_verbosity = get_value(et, ev.payload, "DefaultVerbosity", default=0) or 0

    def _on_log_message_spec(self, et: EventType, ev: ParsedEvent):
        log_point = get_value(et, ev.payload, "LogPoint")
        if log_point is None:
            return
        spec = self.log_specs.setdefault(log_point, LogMessageSpec(log_point=log_point))
        cat_ptr = get_value(et, ev.payload, "CategoryPointer", default=0) or 0
        spec.category = self.log_categories.setdefault(cat_ptr, LogCategory(pointer=cat_ptr))
        spec.line = get_value(et, ev.payload, "Line", default=0) or 0
        spec.verbosity = get_value(et, ev.payload, "Verbosity", default=0) or 0

        file = get_string(et, ev.payload, ev.aux, "FileName")
        fmt  = get_string(et, ev.payload, ev.aux, "FormatString")
        if file is not None:
            spec.file = file
            spec.format_string = fmt or ""
        else:
            # legacy: attachment 是 ANSI File\0 + WIDE FormatString
            attachment = get_attachment(et, ev.payload)
            zero = attachment.find(b"\x00")
            if zero >= 0:
                spec.file = attachment[:zero].decode("utf-8", errors="replace")
                wfmt = attachment[zero + 1 :]
                spec.format_string = wfmt.decode("utf-16-le", errors="replace").rstrip("\x00")

    def _on_log_message(self, et: EventType, payload: bytes,
                        aux: Optional[Dict[int, bytes]]):
        log_point = get_value(et, payload, "LogPoint")
        cycle     = get_value(et, payload, "Cycle")
        if log_point is None or cycle is None:
            return
        spec = self.log_specs.get(log_point)
        if spec is None:
            return
        # FormatArgs 在 aux 字段或 attachment
        args_blob = None
        if aux:
            args_blob = aux.get(et.field_by_name.get("FormatArgs", -1))
        if args_blob is None:
            args_blob = get_attachment(et, payload)
        message = _format_log_message(spec.format_string, bytes(args_blob))
        cat_name = spec.category.name if spec.category else ""
        self.log_messages.append(LogMessage(
            time=self.cycles_to_seconds(int(cycle)),
            category=cat_name,
            message=message,
            file=spec.file,
            line=spec.line,
        ))

    # -- Frames --------------------------------------------------------------

    def _on_frame_event_legacy(self, et: EventType, payload: bytes, name: str):
        """legacy: BeginGameFrame/EndGameFrame/Begin/End RenderFrame 在 attachment
        里塞了 7bit-encoded CycleDiff（相对前一次同类事件）。"""
        ftype = 0 if "Game" in name else 1
        attachment = get_attachment(et, payload)
        if not attachment:
            return
        cycle_diff, _ = _decode_7bit(attachment, 0)
        cycle = self._last_frame_cycle[ftype] + cycle_diff
        self._last_frame_cycle[ftype] = cycle
        t = self.cycles_to_seconds(cycle)
        if name.startswith("Begin"):
            self._frame_open_start[ftype] = t
        else:
            start = self._frame_open_start.pop(ftype, t)
            idx = len(self.frames_by_type[ftype])
            self.frames_by_type[ftype].append(Frame(ftype, idx, start, t))

    def _on_frame_event_v2(self, et: EventType, payload: bytes, name: str):
        """新 BeginFrame/EndFrame：直接传 Cycle + FrameType。"""
        cycle = get_value(et, payload, "Cycle", default=0) or 0
        ftype = get_value(et, payload, "FrameType", default=0) or 0
        if ftype not in self.frames_by_type:
            return
        t = self.cycles_to_seconds(int(cycle))
        if name == "BeginFrame":
            self._frame_open_start[ftype] = t
        else:
            start = self._frame_open_start.pop(ftype, t)
            idx = len(self.frames_by_type[ftype])
            self.frames_by_type[ftype].append(Frame(ftype, idx, start, t))


@dataclass
class _ThreadTimingState:
    """
    GameThread 上一帧可能有几万个 scope，全部建对象会爆内存。
    我们的需求只关心 target timer 子串匹配的子树 + 它所在的 frame 时间窗，
    所以维护两层栈：
      - depth_stack：每开一个 scope 就 push 一个 (start_time, name) 浅元组，
        关闭时 pop。整树永远不构造 TimingEvent。
      - capture_stack：仅当某个 scope 命中 target_substr 时，从此点开始
        构造完整 TimingEvent；其子 scope 也都加入此树。capture_stack 起到
        "嵌套捕获区"的作用，pop 出来时落到 captured_subtrees。
    """
    tid: int
    last_cycle: int = -1
    # depth_stack 元素：(start_time, name)；name 仅用于回溯调试，不向外发
    depth_stack: List[Tuple[float, str]] = field(default_factory=list)
    # capture_stack 元素：TimingEvent 节点，与 depth_stack 后段对应
    capture_stack: List[TimingEvent] = field(default_factory=list)
    # capture_stack 第一层（最外的命中）开始时的 depth_stack 索引
    capture_root_depth: int = -1
    # 已闭合的命中子树（每条 = 一棵 TimingEvent 树）
    captured_subtrees: List[TimingEvent] = field(default_factory=list)


# ---- 7-bit varint (与 FTraceAnalyzerUtils::Decode7bit 等价) ---------------

def _decode_7bit(buf: bytes, pos: int) -> Tuple[int, int]:
    """返回 (value, new_pos)。"""
    value = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return value, pos
