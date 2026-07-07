# Event Schema Reference

Use this reference when interpreting game TraceLogging events in ETL.

## Provider

```cpp
TRACELOGGING_DEFINE_PROVIDER(
    g_MySplineProvider,
    "FSplineData",
    (0x11223344, 0x5566, 0x7788, 0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff, 0x00));
```

- Provider name: `FSplineData`
- Provider GUID: `11223344-5566-7788-99aa-bbccddeeff00`
- Event name example: `SplineSlow`

## Recommended Slow Event Fields

```text
FrameId: UInt64
SplineId: UInt64
ThreadId: UInt32
WallUs: Float64
Cycles: UInt64
```

ETW also records timestamp, process ID, and thread ID for each event. Explicit `ThreadId` is useful for quick WPA/CSV inspection, but analysis should trust ETW event header PID/TID when available.

## Recommended Interval Events

Use `SplineStart` and `SplineEnd` if possible. They remove ambiguity from a slow event that only reports end time.

```text
SplineStart: FrameId, SplineId
SplineEnd: FrameId, SplineId, WallUs
```

## Time Interval Rules

Prefer exact interval:

```text
start = SplineStart.Time
end = SplineEnd.Time
```

Fallback for `SplineSlow` only:

```text
start = SplineSlow.Time - WallUs
end = SplineSlow.Time
```

## Classification Rules

```text
WallUs high + running time low + switched-out time high => scheduling/preemption likely
WallUs high + running time high => compute-bound or CPU/memory/cache issue likely
Switch-out with wait/sleep/IO/lock reason => voluntary/blocking wait likely
No context switch data => insufficient ETL data
No stack data => cannot say what replacement thread did at function level
```

Always correlate by PID + TID + timestamp because TID can be reused.

## Context Switch Pairing

Do not pair switch-out and switch-in by CPU. A game thread can migrate between cores. Correct logic:

```text
sort all CSwitch records for target PID/TID by time
when OldPID/OldTID == target: target switched out; replacement = NewPID/NewTID
when NewPID/NewTID == target: target switched in; close pending switched-out segment
clamp segment to [slow_start, slow_end]
aggregate replacement threads by PID/TID
```

Use a small pre-window margin only to reconstruct pending state. Do not count time before slow_start.
