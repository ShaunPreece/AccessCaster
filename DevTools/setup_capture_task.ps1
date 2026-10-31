# AccessCaster - register the elevated capture server as an on-demand Scheduled Task.
#
# Run ONCE (elevated - it self-checks). After this, the capture server can be started
# from a NON-elevated session with NO UAC prompt via:
#     schtasks /run /tn "AccessCasterCapServer"
# The task runs cap_server.ps1 at highest privileges, in the interactive user session
# (so it can reach USBPcap and the device). No auto-run trigger: on-demand only.
#
# WHILE IT IS REGISTERED IT IS A WAY TO ADMIN RIGHTS WITHOUT A PROMPT: anything running as the user
# can edit cap_server.ps1 or the Python it runs, which all live in this user-writable folder, then
# start the task. It is left registered between capture sessions by choice, to save a UAC prompt each
# time. Remove it once USB capture is no longer in use, from an elevated prompt:
#     Unregister-ScheduledTask -TaskName AccessCasterCapServer -Confirm:$false

$ErrorActionPreference = 'Stop'
$marker = "$env:USERPROFILE\cap_task_setup.txt"

try {
    # This script lives beside cap_server.ps1, so derive the path rather than hardcoding a home
    # directory: the repo is public and the task is registered per machine.
    $script = Join-Path $PSScriptRoot 'cap_server.ps1'
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $script + '"')
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName 'AccessCasterCapServer' `
        -Action $action -Principal $principal -Settings $settings -Force | Out-Null
    "OK $(Get-Date -Format s) registered AccessCasterCapServer as $env:USERDOMAIN\$env:USERNAME" |
        Out-File -FilePath $marker -Encoding utf8
} catch {
    "ERR $(Get-Date -Format s): $_" | Out-File -FilePath $marker -Encoding utf8
}
