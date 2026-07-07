param(
    [string]$Name = "FSplineData"
)

$guid = [guid]::NewGuid()
$bytes = $guid.ToByteArray()
$d1 = [BitConverter]::ToUInt32($bytes, 0)
$d2 = [BitConverter]::ToUInt16($bytes, 4)
$d3 = [BitConverter]::ToUInt16($bytes, 6)
$d4 = $bytes[8..15]

$tuple = "(0x{0:x8}, 0x{1:x4}, 0x{2:x4}, {3})" -f $d1, $d2, $d3, (($d4 | ForEach-Object { "0x{0:x2}" -f $_ }) -join ", ")
$line = 'TRACELOGGING_DEFINE_PROVIDER(g_' + $Name + 'Provider, "' + $Name + '", ' + $tuple + ');'

Write-Output "ProviderName: $Name"
Write-Output "ProviderGuid: $($guid.Guid)"
Write-Output "TraceLoggingTuple: $tuple"
Write-Output ""
Write-Output "TRACELOGGING_DEFINE_PROVIDER line template:"
Write-Output $line
