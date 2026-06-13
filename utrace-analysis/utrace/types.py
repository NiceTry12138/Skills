# -*- coding: utf-8 -*-
"""
事件类型注册表：解析 NewEvent payload，记下每个 Uid 的字段布局。

对应 C++：FTypeRegistry / FDispatchBuilder（Engine.cpp:1194-1450）。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import protocol as P


@dataclass
class FieldSpec:
    name: str
    offset: int
    size: int        # in payload，对 array/string 是元素大小；普通字段是字段大小
    type_size: int   # 解码后的每元素大小（>0 整数, <0 浮点 -size, =0 表示其他）
    is_signed: bool
    is_float: bool
    is_string: bool
    is_array: bool   # 字段值在 AuxData 中（Array / String 都是 Array 类）


@dataclass
class EventType:
    uid: int
    logger: str
    name: str
    flags: int
    event_size: int                      # 固定 payload 大小（不含 aux）
    fields: List[FieldSpec] = field(default_factory=list)
    field_by_name: Dict[str, int] = field(default_factory=dict)

    @property
    def is_important(self) -> bool:
        return bool(self.flags & P.EVENT_FLAG_IMPORTANT)

    @property
    def maybe_has_aux(self) -> bool:
        return bool(self.flags & P.EVENT_FLAG_MAYBE_HAS_AUX)

    @property
    def no_sync(self) -> bool:
        return bool(self.flags & P.EVENT_FLAG_NO_SYNC)


def _decode_type_size(type_info: int) -> int:
    """type_info 高 2 bit 是 category，低 2 bit 是 log2(size)。"""
    return 1 << (type_info & P.FIELD_POW2_SIZE_MASK)


def _decode_field_props(type_info: int, size_in_event: int) -> tuple[int, bool, bool, bool, bool]:
    """返回 (type_size, is_signed, is_float, is_string, is_array)。"""
    cat_bit = type_info & P.FIELD_CATEGORY_MASK
    is_float = (cat_bit & P.FIELD_FLOAT) != 0
    is_array = (cat_bit & P.FIELD_ARRAY) != 0
    spec = type_info & P.FIELD_SPECIAL_MASK
    is_signed = bool(spec & P.FIELD_SIGNED)
    is_string = bool(spec & P.FIELD_STRING)
    type_size = _decode_type_size(type_info)
    if is_float:
        type_size = -type_size
    return type_size, is_signed, is_float, is_string, is_array


class TypeRegistry:
    """
    解析 NewEvent payload。仅支持 protocol 4/5/6/7 的两种新事件结构。
    """

    def __init__(self):
        self.types: Dict[int, EventType] = {}

    def register_v4(self, payload: bytes) -> EventType:
        """Protocol4.FNewEventEvent
            uint16 EventUid;
            uint8 FieldCount;
            uint8 Flags;
            uint8 LoggerNameSize;
            uint8 EventNameSize;
            Fields[FieldCount] { uint16 Offset; uint16 Size; uint8 TypeInfo; uint8 NameSize; }
            uint8 LoggerName[LoggerNameSize];
            uint8 EventName[EventNameSize];
            uint8 FieldNames[NameSize sum];
        """
        uid, field_count, flags, logger_size, event_size_name = struct.unpack_from(
            "<HBBBB", payload, 0
        )
        cursor = 6
        fixed_field_size = 6  # 6 bytes per field
        fields_raw = []
        event_payload_size = 0
        for _ in range(field_count):
            offset, fsize, type_info, name_size = struct.unpack_from(
                "<HHBB", payload, cursor
            )
            cursor += fixed_field_size
            fields_raw.append((offset, fsize, type_info, name_size))
            event_payload_size = max(event_payload_size, offset + fsize)

        logger = payload[cursor : cursor + logger_size].decode("ascii", errors="replace")
        cursor += logger_size
        name = payload[cursor : cursor + event_size_name].decode("ascii", errors="replace")
        cursor += event_size_name

        fields: List[FieldSpec] = []
        field_by_name: Dict[str, int] = {}
        for offset, fsize, type_info, name_size in fields_raw:
            fname = payload[cursor : cursor + name_size].decode("ascii", errors="replace")
            cursor += name_size
            tsize, is_signed, is_float, is_string, is_array = _decode_field_props(type_info, fsize)
            fields.append(FieldSpec(
                name=fname, offset=offset, size=fsize,
                type_size=tsize, is_signed=is_signed, is_float=is_float,
                is_string=is_string, is_array=is_array,
            ))
            field_by_name[fname] = len(fields) - 1

        # 总固定 payload 大小：取所有 (offset+size) 的最大值，向最高字段对齐
        # protocol4 里每个字段都给了 offset，所以 event_payload_size 就够了
        et = EventType(uid=uid, logger=logger, name=name, flags=flags,
                       event_size=event_payload_size, fields=fields,
                       field_by_name=field_by_name)
        self.types[uid] = et
        return et

    def register_v6(self, payload: bytes) -> EventType:
        """Protocol6.FNewEventEvent
            uint16 EventUid;
            uint8 FieldCount;
            uint8 Flags;
            uint8 LoggerNameSize;
            uint8 EventNameSize;       // 6 字节
            Fields[FieldCount] {       // 每条 8 字节（union 内 16-bit 对齐 → 1 + 1 pad + 6）
                uint8 FieldType;
                uint8 _pad;
                union {
                    Regular   { uint16 Offset; uint16 Size;  uint8 TypeInfo; uint8 NameSize; }
                    Reference { uint16 Offset; uint16 RefUid;uint8 TypeInfo; uint8 NameSize; }
                    Definition{ uint16 Offset; uint16 _u1;   uint8 _u2;      uint8 TypeInfo; }
                };
            };
        """
        uid, field_count, flags, logger_size, event_size_name = struct.unpack_from(
            "<HBBBB", payload, 0
        )
        cursor = 6
        per_field = 8

        # 先把 fields 头读完，再读名字
        fields_raw: List[tuple] = []
        event_payload_size = 0
        for _ in range(field_count):
            family = payload[cursor]
            # cursor + 1 是 padding，跳过
            offset = struct.unpack_from("<H", payload, cursor + 2)[0]
            if family == P.FIELD_FAMILY_REGULAR:
                fsize, type_info, name_size = struct.unpack_from(
                    "<HBB", payload, cursor + 4
                )
                fields_raw.append(("R", offset, fsize, type_info, name_size))
                event_payload_size = max(event_payload_size, offset + fsize)
            elif family == P.FIELD_FAMILY_REFERENCE:
                _ref_uid, type_info, name_size = struct.unpack_from(
                    "<HBB", payload, cursor + 4
                )
                tsize = _decode_type_size(type_info)
                fields_raw.append(("X", offset, tsize, type_info, name_size))
                event_payload_size = max(event_payload_size, offset + tsize)
            else:  # DefinitionId（最后字节是 TypeInfo）
                _u1, _u2, type_info = struct.unpack_from(
                    "<HBB", payload, cursor + 4
                )
                tsize = _decode_type_size(type_info)
                fields_raw.append(("D", offset, tsize, type_info, 0))
                event_payload_size = max(event_payload_size, offset + tsize)
            cursor += per_field

        logger = payload[cursor : cursor + logger_size].decode("ascii", errors="replace")
        cursor += logger_size
        name = payload[cursor : cursor + event_size_name].decode("ascii", errors="replace")
        cursor += event_size_name

        fields: List[FieldSpec] = []
        field_by_name: Dict[str, int] = {}
        for entry in fields_raw:
            kind = entry[0]
            if kind == "R":
                _, offset, fsize, type_info, name_size = entry
                fname = payload[cursor : cursor + name_size].decode("ascii", errors="replace")
                cursor += name_size
                tsize, is_signed, is_float, is_string, is_array = _decode_field_props(type_info, fsize)
                fields.append(FieldSpec(
                    name=fname, offset=offset, size=fsize,
                    type_size=tsize, is_signed=is_signed, is_float=is_float,
                    is_string=is_string, is_array=is_array,
                ))
            elif kind == "X":
                _, offset, tsize, type_info, name_size = entry
                fname = payload[cursor : cursor + name_size].decode("ascii", errors="replace")
                cursor += name_size
                fields.append(FieldSpec(
                    name=fname, offset=offset, size=tsize,
                    type_size=tsize, is_signed=False, is_float=False,
                    is_string=False, is_array=False,
                ))
            else:
                _, offset, tsize, type_info, _ = entry
                fields.append(FieldSpec(
                    name="DefinitionId", offset=offset, size=tsize,
                    type_size=tsize, is_signed=False, is_float=False,
                    is_string=False, is_array=False,
                ))
            field_by_name[fields[-1].name] = len(fields) - 1

        et = EventType(uid=uid, logger=logger, name=name, flags=flags,
                       event_size=event_payload_size, fields=fields,
                       field_by_name=field_by_name)
        self.types[uid] = et
        return et

    def register(self, payload: bytes, version: int) -> EventType:
        if version in (0, 4, 5):
            return self.register_v4(payload)
        if version in (6, 7):
            return self.register_v6(payload)
        raise ValueError(f"未支持的 protocol 版本 {version} 用于 NewEvent 解码")

    def get(self, uid: int) -> Optional[EventType]:
        return self.types.get(uid)
