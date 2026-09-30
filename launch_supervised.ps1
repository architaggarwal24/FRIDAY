<#
.SYNOPSIS
    F.R.I.D.A.Y. — supervised launcher.

.DESCRIPTION
    Runs start.py, and if it crashes (any non-zero exit code), restarts
    it automatically instead of leaving FRIDAY silently dead until you
    happen to notice. A clean exit — you said "exit" / "shut down
    friday", or Ctrl+C — always exits 0 and never triggers a restart;
    that distinction already exists in start.py's own shutdown path
    (_shutdown_friday() calls os._exit(0) explicitly; an unhandled
    exception falls through to Python's default non-zero exit instead),
    so nothing in start.py itself needed to change for this to work.

    The restart counter resets after a run that stayed up for at least
    -MinUptimeSecondsToReset seconds, so a single crash after days of
    uptime is treated as a fresh incident, not held against FRIDAY
    forever. It gives up after -MaxRestarts crashes that all happened
    without a stable run in between — that pattern means something is
    broken every single launch (bad config, missing dependency, etc.),
    and restarting into the same failure a 6th time won't help; check
    logs\friday.log at that point instead.

.EXAMPLE
    .\launch_supervised.ps1
    .\launch_supervised.ps1 -Wake
    .\launch_supervised.ps1 -Text -Debug
#>

param(
    [switch]$Wake,
    [switch]$Text,
    [switch]$NoUi,
    [switch]$Debug,
    [int]$MaxRestarts = 5,
    [int]$MinUptimeSecondsToReset = 60
)

Set-Location -Path $PSScriptRoot

# Same .venv preference as the .bat launchers — if a project-local venv
# exists, use it; otherwise fall back to whatever `python` resolves to on
# PATH (unchanged old behavior for anyone not using a venv at all).
$venvActivate = Join-Path $PSScriptRoot ".venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) { & $venvActivate }

$pyArgs = @()
if ($Wake)  { $pyArgs += "--wake" }
if ($Text)  { $pyArgs += "--text" }
if ($NoUi)  { $pyArgs += "--no-ui" }
if ($Debug) { $pyArgs += "--debug" }

$restartCount = 0

while ($true) {
    $startTime = Get-Date
    Write-Host "`nStarting F.R.I.D.A.Y. ..." -ForegroundColor Cyan

    python start.py @pyArgs
    $exitCode = $LASTEXITCODE
    $uptime = (Get-Date) - $startTime

    if ($exitCode -eq 0) {
        Write-Host "FRIDAY exited cleanly. Not restarting." -ForegroundColor Green
        break
    }

    if ($uptime.TotalSeconds -ge $MinUptimeSecondsToReset) {
        # It was actually up and running for a while before this crash —
        # treat it as a fresh incident, not part of a rapid crash loop.
        $restartCount = 0
    }
    $restartCount++

    Write-Host ("FRIDAY exited with code {0} after {1:N0}s (crash {2}/{3})." -f `
        $exitCode, $uptime.TotalSeconds, $restartCount, $MaxRestarts) -ForegroundColor Yellow

    if ($restartCount -ge $MaxRestarts) {
        Write-Host "`nFRIDAY crashed $MaxRestarts times without a stable run in between — giving up." -ForegroundColor Red
        Write-Host "This usually means something fails on every launch (bad config, a missing" -ForegroundColor Red
        Write-Host "dependency, etc.) rather than a one-off. Check logs\friday.log for what's" -ForegroundColor Red
        Write-Host "actually happening before relaunching manually." -ForegroundColor Red
        Read-Host "`nPress Enter to close"
        break
    }

    Write-Host "Restarting in 5 seconds... (Ctrl+C to stop retrying)" -ForegroundColor Yellow
    Start-Sleep -Seconds 5
}
