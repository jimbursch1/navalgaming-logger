# Install navalgaming-logger: one run does everything, and re-running is safe.
#
#   double-click install.cmd, or: powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# (Remotely, run it as a file: powershell -NoProfile -ExecutionPolicy Bypass -File <folder>\install.ps1
#  -- not as -EncodedCommand: this script is too long for that command line.)
#
# 1. Checks for Python 3.
# 2. Registers the "NavalGaming Logger" scheduled task: at logon, in your desktop
#    session, not elevated, under pythonw (no window), restarted up to 3 times a
#    minute apart if it dies, no time limit. Re-running replaces it.
# 3. Makes an upload token, unless this machine already has one, copies its SHA-256
#    to the clipboard and opens the site, where the Logger card on your dossier takes it.
# 4. Starts the logger now, rather than at the next logon.
#
# To remove it: Unregister-ScheduledTask 'NavalGaming Logger'
$ErrorActionPreference = 'Stop'
$name = 'NavalGaming Logger'
$site = 'https://lom.navalgaming.com'
# This script's folder, or the default folder if it arrived without one.
$dir = if ($PSScriptRoot) { $PSScriptRoot } else { 'C:\temp\navalgaming-logger' }

# 1. Python. Check it runs, not just that a pythonw.exe is on PATH.
$pyw = Get-Command pythonw.exe -ErrorAction SilentlyContinue
$py = if ($pyw) { Join-Path (Split-Path $pyw.Source) 'python.exe' } else { $null }
$major = if ($py -and (Test-Path $py)) { & $py -c "import sys; print(sys.version_info[0])" 2>$null } else { $null }
if ($major -ne '3') {
    Write-Host 'Python 3 was not found.'
    Write-Host 'Install it from https://www.python.org/downloads/windows/ -- tick "Add python.exe to PATH" --'
    Write-Host 'then run this again.'
    exit 1
}

# 2. The scheduled task.
$action = New-ScheduledTaskAction -Execute $pyw.Source -Argument "`"$dir\navalgaming_logger.py`"" `
    -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Force `
    -Description 'navalgaming-logger: waits for Letters of Marque, logs battles and voyages, uploads them (github.com/jimbursch1/navalgaming-logger).' |
    Out-Null
$t = Get-ScheduledTask -TaskName $name
"registered: $($t.TaskName), starts at logon"
"  run:  $($t.Actions[0].Execute) $($t.Actions[0].Arguments)"
"  as:   $($t.Principal.UserId), $($t.Principal.LogonType), $($t.Principal.RunLevel)"

# 3. The token, made before the logger starts so no restart is needed after registering.
$config = Join-Path $env:APPDATA 'navalgaming-logger\upload.json'
if (Test-Path $config) {
    "token: this machine already has one ($config); keeping it"
} else {
    & $py "$dir\upload.py" --new-token "$site/logger_upload.php"
    if ($LASTEXITCODE -ne 0) { exit 1 }
    Write-Host ''
    Write-Host "Opening $site/ -- sign in, and on your dossier's Logger card paste and click Register."
    Start-Process "$site/#logger"
}

# 4. Start it now.
& "$dir\start.ps1"
