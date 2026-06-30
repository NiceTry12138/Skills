---
name: powershell-safe-invocation
description: Use when writing or executing PowerShell/pwsh commands on Windows that involve native executables, arguments with spaces/quotes/special characters, file path operations, OR when debugging PowerShell errors like "term not recognized", quoting issues, argument splitting, or $LASTEXITCODE confusion. Skip for trivial cmdlets without arguments.
---

# PowerShell Safe Invocation

## Quick Index by Symptom

| Symptom | Read section |
|---|---|
| Argument with spaces was split | 3 |
| `$args` behaves strangely inside a function | 3 |
| JSON arg got mangled / quotes stripped | 3, 7 |
| Python/Node complains about BOM | 13 |
| `Remove-Item` matched unexpected files (paths with `[]`) | 12 |
| Process hangs forever | 9 (deadlock), 17 (-NonInteractive) |
| Exit code always 0 even when command failed | 3 |
| `$variable` was not expanded | 5, 14 |
| Output contains weird `\x1b[...m` characters | 17 (PSStyle) |
| Works in terminal but fails in automation | 17, 1 |

## Decision Order

Choose the simplest safe option:

1. PowerShell cmdlet.
2. `& $exe @cliArgs`.
3. Temporary `.ps1` file with `pwsh.exe -File`.
4. `ProcessStartInfo.ArgumentList`.
5. `Start-Process` when its special behavior is required.
6. `cmd.exe /c` only when cmd semantics are required.
7. `Invoke-Expression` only as a tightly controlled last resort.

For uncommon cases and complete examples, read `reference.md`.

## Runtime Requirement

This skill assumes **PowerShell 7+ via `pwsh.exe`**. Many of its safety guarantees (especially `& $exe @cliArgs` for native programs) rely on PS 7's default `Standard` / `Windows` argument-passing mode.

## Codex On Windows: Safe Default

In this workspace, Codex tool calls may run under **Windows PowerShell 5.1** even when `pwsh.exe` is mentioned in examples. Before relying on PS 7 behavior, probe the runtime with Step 0.

If the probe reports PS 5.1 or `pwsh.exe` is missing:

- Treat `& $exe @cliArgs` as safe only for simple arguments without embedded quotes or tricky backslashes.
- For native commands with spaces, quotes, JSON, regexes, empty args, or nontrivial escaping, use `ProcessStartInfo.ArgumentList`.
- For multiline PowerShell logic, write a temporary `.ps1` and run it with the available shell only if the script does not depend on PS 7-only features.
- For project source writes under `S:/SP`, prefer `apply_patch`; do not use PowerShell text cmdlets for source edits.
- For UTF-8 source reads, use `Get-Content -Raw -Encoding UTF8` only for inspection; do not pair `Get-Content` with `Set-Content` for source rewrites.

If `pwsh.exe` is not available on the target machine:

1. State this limitation explicitly to the user.
2. Either ask the user to install PowerShell 7, or
3. Drop straight to `ProcessStartInfo.ArgumentList` (decision step 4) for all native invocations and treat PS 5.1's `& $exe @cliArgs` as **unsafe** for any argument containing spaces, quotes, or backslashes.

Do not silently downgrade to `powershell.exe` and hope for the best.

Reminder:

- `pwsh.exe` = PowerShell 7
- `powershell.exe` = Windows PowerShell 5.1

Installing PowerShell 7 does **not** make `powershell.exe` use PowerShell 7.

## Step 0: Probe Before First Native Call

When the task involves invoking native programs and the shell version or mode is unknown, run this **once** before generating any other command:

~~~powershell
$PSVersionTable.PSVersion.Major
if ($PSVersionTable.PSVersion.Major -ge 7) {
    $PSNativeCommandArgumentPassing
} else {
    'N/A (PowerShell 5.1)'
}
~~~

Interpret the result:

- **PS 7 + `Standard` or `Windows`** → safe to use `& $exe @cliArgs` (decision step 2).
- **PS 5.1**, or **`Legacy` mode**, or **any argument that contains `"`, `\`, or spaces combined with quotes** → skip to `ProcessStartInfo.ArgumentList` (decision step 4).

## Native Programs

Never construct one large command string when arguments can be passed separately.

Use:

~~~powershell
$exe = 'C:\Path With Spaces\tool.exe'
$cliArgs = @(
    '--input'
    'C:\Data Folder\input.json'
    '--flag'
)

& $exe @cliArgs

$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    throw "$exe failed with exit code $exitCode"
}
~~~

Rules:

- Treat every native argument as one array item.
- Invoke executable paths stored in variables with `&`.
- Capture `$LASTEXITCODE` immediately, before any other native call.
- Do not name the array `$args` — that name collides with PowerShell's automatic parameter variable inside functions and script blocks. Use `$cliArgs`, `$argList`, etc.
- Do not use `Invoke-Expression`.
- Do not add a `cmd.exe /c` layer merely to launch an executable.
- Do not use Bash-style `\"` escaping in PowerShell.

## Cmdlets

Use hashtable splatting for PowerShell cmdlets:

~~~powershell
$params = @{
    LiteralPath = 'C:\Data[1]\input.txt'
    Destination = 'C:\Output'
    Force       = $true
    ErrorAction = 'Stop'
}

Copy-Item @params
~~~

Use `-LiteralPath` for real paths unless wildcard expansion is intentional. Characters such as `[`, `]`, and `$` in paths will be misinterpreted by `-Path`.

Do not use `$LASTEXITCODE` to test a PowerShell cmdlet. Use terminating errors:

~~~powershell
$ErrorActionPreference = 'Stop'
~~~

## Complex Commands

Avoid deeply quoted commands such as:

~~~text
cmd.exe /c pwsh.exe -Command "..."
~~~

For multiline code, nested quotes, JSON, XML, regular expressions, pipelines, redirection, or non-ASCII paths:

1. Write a temporary `.ps1` file.
2. Execute it with:

~~~text
pwsh.exe -NoLogo -NoProfile -NonInteractive -File script.ps1
~~~

Prefer `-File` over `-Command` for anything beyond a short, simple expression.

Do not add `-ExecutionPolicy Bypass` unless execution policy is actually blocking a trusted script.

## Strings And Multiline Code

- Use single quotes for literal strings and paths.
- Use double quotes only when PowerShell expansion is needed.
- Avoid backtick line continuation; use arrays, hashtables, splatting, parentheses, or script blocks.
- For JSON, create objects and use `ConvertTo-Json -Depth 10`; do not hand-escape JSON.
- Use single-quoted here-strings for literal multiline text.
- Specify text encoding explicitly when another tool consumes the file. On PS 5.1, `-Encoding utf8` writes a **BOM**, which breaks many Python / Node / Linux tools — prefer PS 7's `utf8NoBOM`, or write bytes via `[System.IO.File]::WriteAllText` with `[System.Text.UTF8Encoding]::new($false)`.

## Start-Process

For normal foreground execution, use:

~~~powershell
& $exe @cliArgs
~~~

Use `Start-Process` only for elevation, new/hidden windows, detached launch, or shell behavior.

`Start-Process -ArgumentList` joins values into a command-line string and is not a reliable structured-argument API.

When a separate process is required and arguments are complex, use:

~~~powershell
$psi = [System.Diagnostics.ProcessStartInfo]::new()
$psi.FileName = $exe
$psi.UseShellExecute = $false

foreach ($arg in $cliArgs) {
    $psi.ArgumentList.Add($arg)
}

$process = [System.Diagnostics.Process]::Start($psi)
$process.WaitForExit()

if ($process.ExitCode -ne 0) {
    throw "Process failed with exit code $($process.ExitCode)"
}
~~~

## File Operations

Before recursive delete, move, or overwrite:

- Resolve the absolute root and target paths.
- Verify the target is inside the intended root (compare with `StartsWith` on the root **plus a trailing separator**, not the bare root — `C:\WorkBackup` is not a child of `C:\Work`).
- Reject empty paths, filesystem roots, and unexpected targets.
- Keep filesystem mutations in PowerShell instead of passing paths to another shell.

See `reference.md` section 12 for a complete validation snippet.

## When A Command Fails

Do **NOT** retry with more quoting or more escaping. Instead:

1. Print each argument and its `.Length` to verify what was actually passed:

   ~~~powershell
   $cliArgs | ForEach-Object { '[{0}] Length={1}' -f $_, $_.Length }
   ~~~

2. Move **down** the Decision Order (e.g., from `& $exe @cliArgs` to a `.ps1` file, or further to `ProcessStartInfo.ArgumentList`).
3. If still failing, read `reference.md` section 16 (Diagnostic Checklist).

Adding more backticks or more `\"` almost always makes it worse.
