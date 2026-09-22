# AccessCaster - elevated USB capture server.
#
# USBPcap capture needs Administrator rights. Rather than elevate for every
# capture (a UAC prompt each time, painful with a screen reader), this runs
# ONCE elevated and then takes commands from a control file, so the rest of the
# session can start and stop captures without further prompts.
#
# Launch (from the normal session):
#   Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','<path>\cap_server.ps1'
#
# Protocol (files under cap_control\):
#   cmd.txt     (written by the main session; one command, consumed + deleted)
#     START <name>   begin capturing the RODECaster to captures\<name>.pcap
#     STOP           stop the current capture
#     RESCAN         redo the hub self-scan
#     QUIT           stop and exit the server
#   status.txt  (written by this server; the main session polls it)
#   server.log  (append-only diagnostic log)

$ErrorActionPreference = 'Continue'

# HIDE this (elevated) console window immediately. It runs elevated via the scheduled task, and if
# an elevated window is in the FOREGROUND, Windows UIPI blocks the non-elevated gui_drive from
# injecting any input (SetCursorPos/clicks are silently ignored), which stops app-driven captures
# dead. A hidden window can't be foreground, so this removes the block entirely. Logging still goes
# to files, so hiding the console loses nothing.
try {
    $hideSig = '[DllImport("kernel32.dll")] public static extern System.IntPtr GetConsoleWindow();' +
               '[DllImport("kernel32.dll")] public static extern bool FreeConsole();' +
               '[DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr h, int n);'
    if (-not ('Cap.ConHide' -as [type])) {
        Add-Type -MemberDefinition $hideSig -Name 'ConHide' -Namespace 'Cap' | Out-Null
    }
    # SW_HIDE the console window, then DETACH from it (FreeConsole) so the conhost window actually
    # closes - SW_HIDE alone is re-shown by the elevated conhost (verified), but a console with no
    # attached process goes away, removing the foreground/UIPI input-block entirely. Logging goes to
    # files, so a console-less server loses nothing.
    $cw = [Cap.ConHide]::GetConsoleWindow()
    if ($cw -ne [System.IntPtr]::Zero) { [Cap.ConHide]::ShowWindow($cw, 0) | Out-Null }
    [Cap.ConHide]::FreeConsole() | Out-Null
} catch {}

# Proof-of-life: write a boot marker immediately so we can tell, from the normal
# session, whether this elevated script actually started and where it thinks it
# is. Uses an absolute path that does not depend on $PSScriptRoot.
$bootFile = "$env:USERPROFILE\cap_server_boot.txt"
"BOOT $(Get-Date -Format s) PSScriptRoot=[$PSScriptRoot] CommandPath=[$PSCommandPath] CWD=[$((Get-Location).Path)] User=[$env:USERNAME]" | Out-File -FilePath $bootFile -Encoding utf8
trap { "TRAP $(Get-Date -Format s): $_" | Out-File -FilePath $bootFile -Append -Encoding utf8 }

# Resolve the project dir robustly even if $PSScriptRoot is empty.
$base = $PSScriptRoot
if ([string]::IsNullOrEmpty($base)) { $base = Split-Path -Parent $PSCommandPath }
if ([string]::IsNullOrEmpty($base)) { $base = (Get-Location).Path }
"BASE=[$base]" | Out-File -FilePath $bootFile -Append -Encoding utf8
$ctl  = Join-Path $base 'cap_control'
$scan = Join-Path $ctl  'scan'
$caps = Join-Path $base 'captures'
$cmdFile    = Join-Path $ctl 'cmd.txt'
$statusFile = Join-Path $ctl 'status.txt'
$logFile    = Join-Path $ctl 'server.log'
$usbpcap = 'C:\Program Files\USBPcap\USBPcapCMD.exe'

foreach ($d in @($ctl, $scan, $caps)) { New-Item -ItemType Directory -Force $d | Out-Null }

function Log($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format 'HH:mm:ss'), $msg
    Add-Content -Path $logFile -Value $line -Encoding utf8
}
function Set-Status($msg) {
    # Atomic-ish: write temp then move over the real file.
    $tmp = "$statusFile.tmp"
    Set-Content -Path $tmp -Value $msg -Encoding utf8
    Move-Item -Path $tmp -Destination $statusFile -Force
    Log "STATUS: $msg"
}

function Start-Capture($dev, $outFile, $addr) {
    # Pass args as a SINGLE STRING with the output path quoted. Passing an array
    # lets Start-Process split the path at its spaces (the project lives under
    # "claude projects\access caster"), so USBPcapCMD gets a broken -o and exits.
    $extra = if ($addr -gt 0) { "--devices $addr" } else { '-A' }
    $argline = '-d ' + $dev + ' -o "' + $outFile + '" -b 8388608 ' + $extra
    return Start-Process -FilePath $usbpcap -ArgumentList $argline -PassThru -WindowStyle Hidden
}

function Stop-Capture($proc) {
    if ($proc -and -not $proc.HasExited) {
        try { Stop-Process -Id $proc.Id -Force } catch {}
    }
}

function Invoke-Scan() {
    Log "scan: starting captures on USBPcap1..8"
    Get-ChildItem $scan -Filter 'hub*.pcap' -ErrorAction SilentlyContinue | Remove-Item -Force
    $procs = @{}
    foreach ($n in 1..8) {
        $dev = "\\.\USBPcap$n"
        $out = Join-Path $scan "hub$n.pcap"
        $argline = '-d ' + $dev + ' -o "' + $out + '" -b 8388608 -A'
        try {
            $procs[$n] = Start-Process -FilePath $usbpcap -ArgumentList $argline `
                -PassThru -WindowStyle Hidden -ErrorAction Stop
        } catch { Log "scan: hub $n could not start" }
    }
    Start-Sleep -Milliseconds 1200
    # THESE HELPERS LIVE BESIDE THIS SCRIPT in DevTools/, and $base is DevTools because the
    # scheduled task points at DevTools\cap_server.ps1. They import Project/ via
    # DevTools/_bootstrap.py, so no PYTHONPATH is needed. A wrong relative path here makes every
    # scan fail SILENTLY, so keep them bare names under $base.
    Push-Location $base
    & python (Join-Path $base "gen_traffic.py") 40 2>&1 | ForEach-Object { Log "  gen: $_" }
    Pop-Location
    Start-Sleep -Milliseconds 500
    foreach ($n in $procs.Keys) { Stop-Capture $procs[$n] }
    Start-Sleep -Milliseconds 600
    foreach ($n in ($procs.Keys | Sort-Object)) {
        $out = Join-Path $scan "hub$n.pcap"
        $sz = if (Test-Path $out) { (Get-Item $out).Length } else { -1 }
        Log "scan: hub $n pcapSize=$sz"
    }
    Push-Location $base
    # Pass $scan WITHOUT a trailing separator: `Join-Path $scan ''` ends in a backslash, and a
    # trailing backslash before the closing quote escapes the quote, so pick_hub saw a mangled path,
    # fell back to its own default dir (empty) and printed NONE even though the scan was good.
    $pick = (& python (Join-Path $base "pick_hub.py") $scan) 2>&1
    Pop-Location
    Log "scan: pick_hub -> $pick"
    $line = ($pick | Select-String -Pattern '^(BEST|NONE)').Line
    if ($line -and $line.StartsWith('BEST')) {
        $parts = $line.Split(' ')
        return @{ hub = [int]$parts[1]; addr = [int]$parts[2]; count = [int]$parts[3] }
    }
    return $null
}

Log "=== cap_server starting ==="
if (-not (Test-Path $usbpcap)) { Set-Status "ERROR USBPcapCMD-not-found"; exit 1 }

# Hub cache: the 8-interface scan spams "Couldn't open device - 2" / "Thread started with
# invalid read handle!" for every USBPcap interface that does not exist (screen-reader noise).
# Once the RODECaster hub is found we cache it and SKIP the scan on later starts. Force a fresh
# scan with the RESCAN command (e.g. if the device is moved to a different USB port).
$cacheFile = Join-Path $ctl 'hub_cache.txt'
$hubInfo = $null
if (Test-Path $cacheFile) {
    $c = (Get-Content $cacheFile -Raw).Trim().Split(' ')
    if ($c.Count -ge 2) {
        $hubInfo = @{ hub = [int]$c[0]; addr = [int]$c[1]; count = $(if ($c.Count -ge 3) { [int]$c[2] } else { 0 }) }
        Log "using cached hub $($hubInfo.hub) addr $($hubInfo.addr) (no scan)"
    }
}
if ($null -eq $hubInfo) {
    Set-Status 'SCANNING'
    $hubInfo = Invoke-Scan
    if ($hubInfo) { "$($hubInfo.hub) $($hubInfo.addr) $($hubInfo.count)" | Out-File -FilePath $cacheFile -Encoding utf8 }
}
if ($null -eq $hubInfo) {
    Set-Status 'ERROR no-rodecaster-hub-found'
    $hub = 0; $addr = 0
} else {
    $hub = $hubInfo.hub; $addr = $hubInfo.addr
    Set-Status ("READY hub={0} addr={1} count={2}" -f $hub, $addr, $hubInfo.count)
}

$current = $null
$currentName = ''
if (Test-Path $cmdFile) { Remove-Item $cmdFile -Force }

while ($true) {
    if (Test-Path $cmdFile) {
        $raw = ''
        try { $raw = (Get-Content $cmdFile -Raw -ErrorAction Stop).Trim() } catch { Start-Sleep -Milliseconds 100; continue }
        Remove-Item $cmdFile -Force -ErrorAction SilentlyContinue
        if ($raw -eq '') { Start-Sleep -Milliseconds 200; continue }
        $tok = $raw.Split(' ')
        $verb = $tok[0].ToUpper()
        Log "cmd: $raw"
        switch ($verb) {
            'START' {
                $name = if ($tok.Count -gt 1) { $tok[1] } else { 'capture' }
                if ($current) { Stop-Capture $current; Start-Sleep -Milliseconds 300 }
                if ($hub -le 0) { Set-Status 'ERROR no-hub-for-start'; break }
                $out = Join-Path $caps "$name.pcap"
                if (Test-Path $out) { Remove-Item $out -Force }
                $current = Start-Capture "\\.\USBPcap$hub" $out $addr
                $currentName = $name
                Set-Status "CAPTURING $name"
            }
            'STOP' {
                if ($current) {
                    Stop-Capture $current
                    Start-Sleep -Milliseconds 400
                    $out = Join-Path $caps "$currentName.pcap"
                    $size = if (Test-Path $out) { (Get-Item $out).Length } else { 0 }
                    Set-Status ("STOPPED {0} bytes={1}" -f $currentName, $size)
                    $current = $null; $currentName = ''
                } else {
                    Set-Status 'IDLE nothing-to-stop'
                }
            }
            'RESCAN' {
                if ($current) { Stop-Capture $current; $current = $null }
                Set-Status 'SCANNING'
                $hubInfo = Invoke-Scan
                if ($null -eq $hubInfo) { Set-Status 'ERROR no-rodecaster-hub-found'; $hub = 0; $addr = 0 }
                else {
                    $hub = $hubInfo.hub; $addr = $hubInfo.addr
                    "$($hubInfo.hub) $($hubInfo.addr) $($hubInfo.count)" | Out-File -FilePath $cacheFile -Encoding utf8
                    Set-Status ("READY hub={0} addr={1} count={2}" -f $hub, $addr, $hubInfo.count)
                }
            }
            'QUIT' {
                if ($current) { Stop-Capture $current }
                Set-Status 'QUIT'
                Log "=== cap_server exiting ==="
                exit 0
            }
            default { Set-Status "ERROR unknown-cmd:$verb" }
        }
    }
    Start-Sleep -Milliseconds 250
}
