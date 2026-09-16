# Uninstall navalgaming-logger: stops it, removes its scheduled task and deletes this
# machine's upload token. Safe to run again.
#
#   double-click uninstall.cmd, or: powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
#
# 1. Stops the logger and removes the "NavalGaming Logger" scheduled task.
# 2. Deletes %APPDATA%\navalgaming-logger, where the token lives. Without it this
#    machine can no longer upload.
# 3. Opens the site: the token is still registered there, and only a signed-in member
#    can revoke it, on the Logger card of their dossier.
#
# The logger's folder is left alone: it holds your battle and voyage records, and this
# script. Delete it yourself once you no longer want them.
$name = 'NavalGaming Logger'
$site = 'https://lom.navalgaming.com'

# 1. The logger, and its task.
if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $name
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    "removed: the '$name' scheduled task"
} else {
    "no '$name' scheduled task"
}
# Any logger started some other way, such as by hand.
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='py.exe' OR Name='pythonw.exe' OR Name='pyw.exe'" |
    Where-Object { $_.CommandLine -like '*navalgaming_logger.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "stopped: $($_.ProcessId)" }

# 2. The token.
$config = Join-Path $env:APPDATA 'navalgaming-logger'
if (Test-Path $config) {
    Remove-Item $config -Recurse -Force
    "deleted: $config (this machine's upload token)"
} else {
    "no token on this machine"
}

# 3. The site.
Write-Host ''
Write-Host "Opening $site/ -- sign in, and on your dossier's Logger card click Revoke for this machine."
Write-Host "Your records are still in $PSScriptRoot; delete that folder if you don't want them."
Start-Process "$site/#logger"
