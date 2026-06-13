# -*- coding: utf-8 -*-
"""
事件解析层。

两种模式：
- `parse_important_stream`：把 events / importants tid 上的所有事件解析成
  `ParsedEvent` 列表（量级在几十～几万，可以全量持有）。
- `walk_normal_stream`：在每条业务线程上**流式**解析，每解出一条事件
  立即调用 `on_event(uid, payload_view, aux_views, serial)` 回调。
  绝不把整条流的事件列表化——不然百万级 GameThread 事件会撑爆内存。

参考：
  Engine/Source/Developer/TraceAnalysis/Private/Analysis/Engine.cpp
    FProtocol{5,6,7}Stage::ParseImportantEvents / ParseEvents / ParseEventsWithAux
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from . import protocol as P
from .types import EventType, FieldSpec, TypeRegistry


# ---- 字段值读取（payload 用 memoryview / bytes 都行） ----------------------

def get_value(event_type: EventType, payload, field_name: str, default=None):
    idx = event_type.field_by_name.get(field_name)
    if idx is None:
        return default
    field = event_type.fields[idx]
    if field.is_array:
        return default  # array 在 aux
    return _read_pod(field, payload, field.offset)


def _read_pod(field: FieldSpec, payload, offset: int):
    sz = abs(field.type_size)
    if field.is_float:
        if sz == 4:
            return struct.unpack_from("<f", payload, offset)[0]
        return struct.unpack_from("<d", payload, offset)[0]
    fmt_signed = {1: "<b", 2: "<h", 4: "<i", 8: "<q"}
    fmt_unsigned = {1: "<B", 2: "<H", 4: "<I", 8: "<Q"}
    fmts = fmt_signed if field.is_signed else fmt_unsigned
    fmt = fmts.get(sz, "<B")
    return struct.unpack_from(fmt, payload, offset)[0]


def get_string(event_type: EventType, payload, aux_blocks, field_name: str) -> Optional[str]:
    idx = event_type.field_by_name.get(field_name)
    if idx is None:
        return None
    raw = aux_blocks.get(idx) if aux_blocks else None
    if raw is None:
        return None
    field = event_type.fields[idx]
    if field.type_size == 1:
        s = bytes(raw).decode("utf-8", errors="replace")
    elif field.type_size == 2:
        s = bytes(raw).decode("utf-16-le", errors="replace")
    else:
        s = bytes(raw).decode("utf-8", errors="replace")
    # 去掉末尾的 NUL 终止符（Insights 写字符串字段时有时附带）
    return s.rstrip("\x00")


def get_attachment(event_type: EventType, payload) -> bytes:
    return bytes(payload[event_type.event_size :])


# ---- aux header（Protocol5+） ----------------------------------------------

def parse_aux_header(buf, pos: int) -> Tuple[int, int, int]:
    """FAuxHeader 是 4 字节 uint32 Pack：
        bits  0..7  = Uid (= AuxData)
        bits  8..12 = FieldIndex (5 bits)
        bits 13..31 = SizeInBytes (19 bits)
       返回 (field_index, size, header_byte_count)。"""
    pack = struct.unpack_from("<I", buf, pos)[0]
    field_index = (pack >> 8) & 0x1F
    size = pack >> 13
    return field_index, size, 4


# ---- ParsedEvent for important streams ------------------------------------

@dataclass
class ParsedEvent:
    uid: int
    payload: bytes              # 不带 aux，仅 fixed 段
    aux: Dict[int, bytes]       # field_index -> 完整 aux block（多块自动合并）


def parse_important_stream(stream: bytes, registry: TypeRegistry, version: int) -> List[ParsedEvent]:
    """
    Important 流由若干 FImportantEventHeader { uint16 Uid, uint16 Size, uint8 Data[Size] }
    组成。Data 内可能跟着 0..N 个 AuxData 块，再以 AuxDataTerminal 结尾。
    """
    out: List[ParsedEvent] = []
    pos = 0
    end = len(stream)

    while pos + 4 <= end:
        uid, size = struct.unpack_from("<HH", stream, pos)
        if pos + 4 + size > end:
            break
        body_start = pos + 4
        body_end = body_start + size
        pos = body_end

        if uid == P.UID_NEW_EVENT:
            registry.register(stream[body_start:body_end], version)
            continue

        et = registry.get(uid)
        if et is None:
            continue

        payload = bytes(stream[body_start : body_start + et.event_size])
        aux: Dict[int, bytes] = {}

        if et.maybe_has_aux:
            # 重要流：aux header 的 Uid 字节是「裸」well-known uid（不 << UID_SHIFT），
            # 见 Engine.cpp ParseImportantEvents:3792。AuxData=1，AuxDataTerminal=3。
            cur = body_start + et.event_size
            tail = body_end
            while cur < tail:
                marker = stream[cur]
                if marker == P.UID_AUX_DATA_TERMINAL:
                    cur += 1
                    break
                if marker != P.UID_AUX_DATA:
                    break
                fidx, dsize, hdr = parse_aux_header(stream, cur)
                cur += hdr
                blk = bytes(stream[cur : cur + dsize])
                aux[fidx] = (aux[fidx] + blk) if fidx in aux else blk
                cur += dsize

        out.append(ParsedEvent(uid=uid, payload=payload, aux=aux))

    return out


# ---- normal stream: 流式 dispatch ------------------------------------------

# 回调签名：(uid, payload_view, aux_views_or_None, serial_or_-1) -> None
# - payload_view：memoryview，仅在回调内有效；callee 不应跨调用持有
# - aux_views：Optional[Dict[int, bytes]]；为简单起见 aux 已经合并复制成 bytes
NormalCallback = Callable[[int, memoryview, Optional[Dict[int, bytes]], int], None]


def walk_normal_stream(
    stream: bytes,
    registry: TypeRegistry,
    version: int,
    on_event: NormalCallback,
) -> None:
    """流式解析单条业务线程的字节流，逐事件回调 on_event。

    内置 EnterScope_T/_TA/_TB 等 well-known scope 事件不投递（CPU profiler 的
    timing scope 信息在 EventBatchV2 的 aux Data 里，不在裸 EnterScope 里）。
    """
    mv = memoryview(stream)
    pos = 0
    end = len(stream)

    # 预计算 well-known 事件的固定大小（不含 1 字节 uid 头）
    if version >= 7:
        KNOWN_SIZES = {
            P.UID_AUX_DATA_TERMINAL: 0,
            P.UID_ENTER_SCOPE:       0,
            P.UID_LEAVE_SCOPE:       0,
            P.UID_ENTER_SCOPE_TA:    8,
            P.UID_LEAVE_SCOPE_TA:    8,
            # protocol7 的 EnterScope_TB / LeaveScope_TB 的 uid 与 protocol5
            # 的 EnterScope_T / LeaveScope_T 撞了（8 / 9 vs 8 / 12），所以 uid=8/9
            # 在 protocol7 下表示 _TB（7 字节 = 1 uid + 7 时间戳）
            P.UID_ENTER_SCOPE_TB:    7,
            P.UID_LEAVE_SCOPE_TB:    7,
        }
    else:
        KNOWN_SIZES = {
            P.UID_AUX_DATA_TERMINAL: 0,
            P.UID_ENTER_SCOPE:       0,
            P.UID_LEAVE_SCOPE:       0,
            8:  7,   # EnterScope_T (protocol5/6)
            12: 7,   # LeaveScope_T
        }

    while pos < end:
        b0 = stream[pos]
        if b0 & P.UID_FLAG_TWO_BYTE:
            if end - pos < 2:
                break
            raw = struct.unpack_from("<H", stream, pos)[0]
            uid = raw >> P.UID_SHIFT
            cursor = pos + 2
        else:
            uid = b0 >> P.UID_SHIFT
            cursor = pos + 1

        # 内置事件
        if uid < P.UID_USER:
            if uid == P.UID_AUX_DATA:
                # 出现裸 AuxData：当作 aux 块跳过整条
                cursor -= (cursor - pos)  # 让 aux header 包含 uid 字节
                cursor = pos
                if end - cursor < 4:
                    break
                _fidx, dsize, ahdr = parse_aux_header(stream, cursor)
                pos = cursor + ahdr + dsize
                continue
            event_size = KNOWN_SIZES.get(uid, 0)
            if cursor + event_size > end:
                break
            # 我们不投递裸 scope 事件
            pos = cursor + event_size
            continue

        # 业务事件
        et = registry.get(uid)
        if et is None:
            # 数据损坏/未声明：往后跳一字节继续找。stream 大时可能产生噪音，
            # 但安全第一。
            pos += 1
            continue

        # NoSync = False → 头部还有 24-bit Serial
        serial = -1
        if not et.no_sync:
            if cursor + 3 > end:
                break
            s_lo, s_hi = struct.unpack_from("<HB", stream, cursor)
            serial = (s_lo | (s_hi << 16))
            cursor += 3

        # 固定 payload
        if cursor + et.event_size > end:
            break
        payload_start = cursor
        cursor += et.event_size

        aux: Optional[Dict[int, bytes]] = None
        if et.maybe_has_aux:
            aux = {}
            while cursor < end:
                m = stream[cursor]
                if m == (P.UID_AUX_DATA_TERMINAL << P.UID_SHIFT):
                    cursor += 1
                    break
                if m == (P.UID_AUX_DATA << P.UID_SHIFT):
                    if end - cursor < 4:
                        break
                    fidx, dsize, ahdr = parse_aux_header(stream, cursor)
                    cursor += ahdr
                    if cursor + dsize > end:
                        break
                    blk = bytes(stream[cursor : cursor + dsize])
                    cursor += dsize
                    aux[fidx] = (aux[fidx] + blk) if fidx in aux else blk
                else:
                    # 出现非 aux 字节：意味着 ParseEventsWithAux 里那种交错情况，
                    # 这里直接停止收 aux 让外层下次循环继续解。
                    break

        on_event(uid, mv[payload_start : payload_start + et.event_size], aux, serial)
        pos = cursor
