[CmdletBinding()]
param(
    [switch]$Repair,
    [switch]$RestoreThreadLinks,
    [switch]$InferExactCwd,
    [string]$CodexHome,
    [string]$OutputDirectory,
    [int]$WaitSeconds = 1800
)
$ErrorActionPreference = 'Stop'
$taskPython = $null
$taskBundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (Test-Path -LiteralPath $taskBundledPython) {
    $taskPython = $taskBundledPython
} else {
    foreach ($taskPythonName in @('python.exe', 'python3.exe')) {
        $taskCommand = Get-Command $taskPythonName -ErrorAction SilentlyContinue
        if ($taskCommand -and $taskCommand.Source -notlike '*\WindowsApps\*') {
            $taskPython = $taskCommand.Source
            break
        }
    }
}
if (-not $taskPython) { throw 'No Python found. Install Python 3.10+ or run this on a computer with the Codex bundled runtime.' }
$taskArguments = @('-X', 'utf8', (Join-Path $PSScriptRoot 'recovery.py'), '--wait-seconds', "$WaitSeconds")
if ($Repair) { $taskArguments += '--repair' }
if ($RestoreThreadLinks) { $taskArguments += '--restore-thread-links' }
if ($InferExactCwd) { $taskArguments += '--infer-exact-cwd' }
if ($CodexHome) { $taskArguments += @('--home', $CodexHome) }
if ($OutputDirectory) { $taskArguments += @('--output', $OutputDirectory) }
& $taskPython @taskArguments
if ($LASTEXITCODE -ne 0) { throw "Recovery script failed with exit code $LASTEXITCODE. See the message above." }
