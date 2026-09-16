# Restart navalgaming-logger: how a new version, or a new token, takes effect.
#
#   powershell -ExecutionPolicy Bypass -File .\start.ps1
#
# The logger is the "NavalGaming Logger" scheduled task (install.ps1), which
# starts it at logon; it waits for the game by itself. Restarting through the task
# runs it in your desktop session, not a remote session, so a dropped connection
# cannot kill it. Restarting
# mid-battle splits that battle across two files.
$name = 'NavalGaming Logger'
# This script's folder; sent as -EncodedCommand it has none, so the default folder.
$dir = if ($PSScriptRoot) { $PSScriptRoot } else { 'C:\temp\navalgaming-logger' }
Stop-ScheduledTask -TaskName $name -ErrorAction Stop
# Any logger started some other way, such as by hand or by the old start.ps1.
$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='py.exe' OR Name='pythonw.exe' OR Name='pyw.exe'" |
    Where-Object { $_.CommandLine -like '*navalgaming_logger.py*' }
if ($existing) {
    $existing | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    "stopped: " + ($existing.ProcessId -join ', ')
}
Start-Sleep -Seconds 1
Start-ScheduledTask -TaskName $name
Start-Sleep -Seconds 5
"task: " + (Get-ScheduledTask -TaskName $name).State
Get-Content "$dir\navalgaming-logger.log" -Tail 6
