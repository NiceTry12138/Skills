# WPR Capture Reference

Use this reference when user needs to create an ETL file that contains both CPU scheduling data and custom TraceLogging events.

## Provider Flow

```text
TRACELOGGING_DEFINE_PROVIDER defines provider name + GUID
TraceLoggingRegister exposes provider to ETW
WPR profile enables provider GUID
TraceLoggingWrite emits events only while provider is enabled
wpr -stop writes ETL
```

## Minimal Capture

Custom provider only is not enough for preemption diagnosis. Combine CPU with the custom provider profile.

```bat
wpr -start CPU -start "S:\SP\ThroughtWork\WPA_WPRP\FSplineData.wprp!FSplineData.Verbose" -filemode
```

Stop:

```bat
wpr -stop C:\trace\spline.etl
```

If the profile name inside the wprp is `MySpline`:

```bat
wpr -start CPU -start "S:\SP\ThroughtWork\WPA_WPRP\FSplineData.wprp!MySpline.Verbose" -filemode
```

## Validation

```bat
wpr -profiledetails "S:\SP\ThroughtWork\WPA_WPRP\FSplineData.wprp!FSplineData.Verbose"
```

Valid output should list provider GUID:

```text
11223344-5566-7788-99aa-bbccddeeff00
```

## Notes

- Use Administrator terminal if WPR fails to start.
- Create `C:\trace` before `wpr -stop`.
- Keep provider GUID stable. Regenerate only for new logical provider.
- Use file mode for long gameplay captures.
- To explain replacement thread work, ensure CPU stacks are captured. Without stacks, report only process/thread identity and timing.
