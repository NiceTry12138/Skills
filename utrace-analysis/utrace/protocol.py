# -*- coding: utf-8 -*-
"""
UE Trace 协议常量。

参考引擎源码：
- Engine/Source/Runtime/TraceLog/Public/Trace/Detail/Transport.h
- Engine/Source/Runtime/TraceLog/Public/Trace/Detail/Protocols/Protocol{0..7}.h
- Engine/Source/Developer/TraceAnalysis/Private/Analysis/Engine.cpp
"""

# ---- magic ------------------------------------------------------------------
# Engine.cpp:5267 FMagicStage::OnData。
# C++ multi-char literal 'TRCE' 把 T 放最高字节：T<<24|R<<16|C<<8|E = 0x54524345。
# Insights 把这个 uint32 以小端写到文件，于是文件里实际看到的字节序列是
# 反过来的：E C R T。读出 uint32 (little-endian) 仍然 = 0x54524345，与
# C++ 端比较的常量数值上一致。
# 'TRC2' 同理 = 0x54524332，文件里看到 2 C R T。
MAGIC_TRCE   = 0x54524345
MAGIC_TRC2   = 0x54524332
MAGIC_LEGACY = 0x00000001  # protocol 0 / transport 1，无 magic 时也接受

# ---- transport --------------------------------------------------------------
# Transport.h ETransport
TRANSPORT_RAW            = 1
TRANSPORT_PACKET         = 2
TRANSPORT_TID_PACKET     = 3
TRANSPORT_TID_PACKET_SYNC = 4

# Transport.h ETransportTid
TID_EVENTS     = 0       # 事件类型描述
TID_IMPORTANTS = 1       # important / cached events（也叫 Internal）
TID_BIAS       = 2       # 真正业务线程从这里开始
TID_END        = 0x3ffe
TID_SYNC       = 0x3fff

# FTidPacketBase
PACKET_ENCODED_MARKER = 0x8000
PACKET_PARTIAL_MARKER = 0x4000
PACKET_THREAD_ID_MASK = 0x3fff
PACKET_HEADER_SIZE = 4   # uint16 PacketSize + uint16 ThreadId

# ---- protocol 5/6/7 well-known UIDs -----------------------------------------
# Protocol5.h EKnownEventUids
UID_FLAG_TWO_BYTE = 0x0001
UID_SHIFT         = 1

UID_NEW_EVENT          = 0   # 注册一个新的事件类型
UID_AUX_DATA           = 1   # 跟随事件后的可变长附加数据（数组/字符串）
UID_AUX_DATA_TERMINAL  = 3   # AuxData 流终止
UID_ENTER_SCOPE        = 4   # 不带时间戳的 EnterScope（cpu profiler 内部时间）
UID_LEAVE_SCOPE        = 5
UID_ENTER_SCOPE_T      = 8   # 带 56-bit 相对时间戳（protocol 5/6）
UID_LEAVE_SCOPE_T      = 12

# Protocol7 新增（保留 5 的全部，再加这 4 个）
UID_ENTER_SCOPE_TA = 6   # absolute timestamp
UID_LEAVE_SCOPE_TA = 7
UID_ENTER_SCOPE_TB = 8   # base-relative timestamp（取代 protocol5 的 EnterScope_T）
UID_LEAVE_SCOPE_TB = 9

UID_USER = 16  # _WellKnownNum，从这里起是业务事件

# ---- event flags（FNewEventEvent.Flags） -----------------------------------
EVENT_FLAG_IMPORTANT      = 1 << 0
EVENT_FLAG_MAYBE_HAS_AUX  = 1 << 1
EVENT_FLAG_NO_SYNC        = 1 << 2
EVENT_FLAG_DEFINITION     = 1 << 3   # protocol 6+

# ---- field type info (Protocol0::Field_*) ----------------------------------
FIELD_CATEGORY_MASK   = 0o300
FIELD_INTEGER         = 0o000
FIELD_FLOAT           = 0o100
FIELD_ARRAY           = 0o200

FIELD_POW2_SIZE_MASK  = 0o003   # log2(size in bytes)

FIELD_SPECIAL_MASK    = 0o030
FIELD_POD             = 0o000
FIELD_STRING          = 0o010
FIELD_SIGNED          = 0o020

# ---- protocol 6 new event field families ------------------------------------
FIELD_FAMILY_REGULAR     = 0
FIELD_FAMILY_REFERENCE   = 1
FIELD_FAMILY_DEFINITION  = 2
