# Polls the active audio endpoints and prints a line whenever the set of RODE endpoints changes.
# Used to measure how long RODE's Virtual Device Driver takes to attach after the desk
# re-enumerates, without touching the desk. Run in the background; stop it with Stop-Process.
#
# Usage: powershell -File DevTools/watch_vad_endpoints.ps1 [-Seconds 300] [-Every 2]
param([int]$Seconds = 300, [double]$Every = 2)

$start = Get-Date
$last = $null
while (((Get-Date) - $start).TotalSeconds -lt $Seconds) {
    $names = Get-PnpDevice -PresentOnly -Class AudioEndpoint -ErrorAction SilentlyContinue |
        Where-Object { $_.FriendlyName -match 'Caster' } |     # "RODECaster" and "RØDECaster" alike; the Ø does not survive PS 5.1 reading a BOM-less file
        ForEach-Object { $_.FriendlyName } | Sort-Object
    $faders = @($names | Where-Object { $_ -match '^Fader' }).Count
    $system = @($names | Where-Object { $_ -match '^System' }).Count
    $usb = @($names | Where-Object { $_ -notmatch '^(Fader|System)' }).Count
    $line = "faders=$faders system=$system usb=$usb"
    if ($line -ne $last) {
        $t = ((Get-Date) - $start).TotalSeconds
        Write-Output ("{0,7:F1}s  {1}   [{2}]" -f $t, $line, ($names -join '; '))
        $last = $line
    }
    Start-Sleep -Seconds $Every
}
Write-Output "done"
