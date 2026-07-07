param(
    [Parameter(Mandatory = $true)]
    [string]$EtlPath,

    [string]$OutputDir = "",
    [string]$ProviderName = "FSplineData",
    [string]$EventName = "SplineSlow",
    [double]$DefaultWindowMs = 20.0,
    [double]$MarginMs = 10.0
)

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$sourceRoot = Join-Path $scriptRoot "etl-spline-analyzer"

if (-not (Test-Path -LiteralPath $EtlPath -PathType Leaf)) {
    throw "ETL not found: $EtlPath"
}

if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $etlDir = Split-Path -Parent ([System.IO.Path]::GetFullPath($EtlPath))
    $OutputDir = Join-Path $etlDir "spline-proof"
}

if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    throw "dotnet SDK not found. Install .NET SDK before running the ETL analyzer."
}

$workRoot = Join-Path $env:TEMP "wpa-analysis"
$runRoot = Join-Path $workRoot "etl-spline-analyzer"
New-Item -ItemType Directory -Force -Path $workRoot | Out-Null

if (Test-Path -LiteralPath $runRoot) {
    Remove-Item -LiteralPath $runRoot -Recurse -Force
}
Copy-Item -LiteralPath $sourceRoot -Destination $runRoot -Recurse -Force

$project = Join-Path $runRoot "EtlSplineAnalyzer.csproj"
$nugetConfig = Join-Path $runRoot "NuGet.Config"

$env:DOTNET_CLI_HOME = Join-Path $workRoot "dotnet-home"
$env:NUGET_PACKAGES = Join-Path $workRoot "nuget-packages"
$env:APPDATA = Join-Path $workRoot "appdata"
New-Item -ItemType Directory -Force -Path (Join-Path $env:APPDATA "NuGet") | Out-Null

dotnet restore $project --configfile $nugetConfig
dotnet run --project $project -c Release --no-restore -- $EtlPath $OutputDir $ProviderName $EventName $DefaultWindowMs $MarginMs
