# -*- coding: utf-8 -*-
"""
TidPacket transport 层：把 utrace 文件按 4 字节 packet 头切，按 ThreadId 把
解码后的字节流分桶（每个线程一个 bytes 流）。带 EncodedMarker 的 packet 用
LZ4 block 解压。

对应 C++：Engine/Source/Developer/TraceAnalysis/Private/Analysis/Transport/
        TidPacketTransport.cpp
"""

from __future__ import annotations

import struct
from collections import defaultdict
from typing import Dict, Iterator, Tuple

import lz4.block

from . import protocol as P


def parse_header(data: bytes) -> Tuple[int, int]:
    """读 utrace 头：magic[, metadata], TransportVersion, ProtocolVersion。

    返回 (transport_version, protocol_version, header_byte_count)。
    """
    pos = 0
    if len(data) < 4:
        raise ValueError("utrace 文件太小，没有 magic")
    magic = struct.unpack_from("<I", data, pos)[0]

    if magic == P.MAGIC_TRCE:
        pos += 4
    elif magic == P.MAGIC_TRC2:
        pos += 4
        # FMetadataStage：uint16 size + size 字节
        if len(data) - pos < 2:
            raise ValueError("TRC2 头损坏：metadata size 缺失")
        meta_size = struct.unpack_from("<H", data, pos)[0]
        pos += 2 + meta_size
    elif magic == P.MAGIC_LEGACY:
        # 老协议：直接是 TransportVersion+ProtocolVersion，不前移
        pass
    else:
        raise ValueError(f"无法识别的 magic 0x{magic:08X}")

    if len(data) - pos < 2:
        raise ValueError("缺少 TransportVersion / ProtocolVersion 字节")
    tv = data[pos]
    pv = data[pos + 1]
    pos += 2
    return tv, pv, pos


class TidPacketTransport:
    """
    解析 TidPacket / TidPacketSync transport，把 packet 内容按 ThreadId 拼成
    每条线程独立的字节流。
    """

    def __init__(self, raw: bytes, body_offset: int):
        self._raw = raw
        self._pos = body_offset
        self._end = len(raw)
        # threadId -> bytearray
        self.streams: Dict[int, bytearray] = defaultdict(bytearray)
        self.sync_count = 0
        # protocol5/6 起内置两条线程：events / importants
        self.streams[P.TID_EVENTS] = bytearray()
        self.streams[P.TID_IMPORTANTS] = bytearray()

    def parse_all(self) -> None:
        """把整份 trace 一次性切完。

        输入文件最多约 1 GiB 解码后；为 simplicity 一次性内存承担，
        不做流式（流式需要多线程 + 增量重组比单次扫慢得多）。
        """
        raw = self._raw
        end = self._end
        pos = self._pos
        sync = 0

        while pos + P.PACKET_HEADER_SIZE <= end:
            packet_size, raw_thread_id = struct.unpack_from("<HH", raw, pos)
            if pos + packet_size > end:
                break  # 截断，跳出

            data_start = pos + P.PACKET_HEADER_SIZE
            data_end   = pos + packet_size
            pos = data_end  # 提前推进，下面只用 data_start/data_end

            tid = raw_thread_id & P.PACKET_THREAD_ID_MASK
            if tid == P.TID_SYNC:
                sync += 1
                continue

            data_size = packet_size - P.PACKET_HEADER_SIZE
            if raw_thread_id & P.PACKET_ENCODED_MARKER:
                # 编码包：FTidPacketEncoded { uint16 DecodedSize, uint8 Data[] }
                if data_size < 2:
                    continue
                decoded_size = struct.unpack_from("<H", raw, data_start)[0]
                payload = raw[data_start + 2 : data_end]
                try:
                    decoded = lz4.block.decompress(payload, uncompressed_size=decoded_size)
                except lz4.block.LZ4BlockError:
                    # 损坏数据：忽略此 packet，继续（与 C++ 一样会 ReadError 终止，
                    # 这里宽松一点，跳过它继续解后面的）
                    continue
                if len(decoded) != decoded_size:
                    continue
                self.streams[tid].extend(decoded)
            else:
                self.streams[tid].extend(raw[data_start : data_end])

        self.sync_count = sync

    def thread_ids(self) -> Iterator[int]:
        return iter(self.streams.keys())

    def get_stream(self, tid: int) -> bytes:
        return bytes(self.streams[tid])
