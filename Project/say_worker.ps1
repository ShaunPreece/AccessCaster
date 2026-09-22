# say_worker.ps1 - PERSISTENT speech worker. Reads one phrase per line from stdin and speaks it.
#
# WHY THIS EXISTS. say.ps1 is a one-shot: every call pays PowerShell startup plus loading the
# speech stack, measured at 1.3 to 1.7 SECONDS on this machine. That is fine for the occasional
# status line, but the control announcer speaks on every mute/solo press, so a handful of quick
# presses spawn a handful of processes that each take over a second and fight each other for the
# audio device, so announcements lag badly and then seem to stop altogether. One long-lived process
# removes the startup cost entirely: after the first phrase, speaking is immediate.
#
# Usage (the server does this, see server.py _ann_say):
#   powershell -File say_worker.ps1 [-Rate 3] [-Voice Zira] [-Device "Realtek"] [-Volume 100] [-NoGate]
#   then write "some text`n" to its stdin for each phrase. A blank line is ignored; EOF exits.
#
# -Volume is SAPI's own 0-100 volume, which scales ONLY this voice's output stream: it does not
# touch Windows system volume, other applications, or the desk's audio. It can also be changed
# mid-session by writing the control line "!vol N" instead of a phrase, so the server never has to
# restart the worker (and never has to pay the first-phrase startup cost again) just to get quieter.
#
# -Device picks WHICH Windows output the speech goes to, by substring match on the device name.
# That matters on this desk: the default output is the RODECaster itself, so anything spoken through
# it disappears the moment you SOLO another channel or mute USB 1 - the cue bus replaces the
# headphone feed, and our speech is arriving on USB 1. Pointing announcements at a different device
# (e.g. "Realtek") makes them survive solo. Default is the system default output, unchanged.
#
# SELF-GATING is per phrase, exactly like say.ps1: silent while the Claude GUI has focus, unless
# -NoGate. The check is re-done for every line, since focus changes while the worker lives.
param(
  [int]$Rate = 3,            # SAPI rate, -10 (slow) .. 10 (fast)
  [string]$Voice = '',       # partial voice-name match, e.g. 'Zira'
  [string]$Device = '',      # partial output-device match, e.g. 'Realtek'; '' = system default
  [int]$Volume = 100,        # SAPI stream volume, 0 (silent) .. 100 (full); this voice only
  [switch]$NoGate            # speak even when the Claude GUI is focused
)

$ErrorActionPreference = 'Continue'

# SAPI COM (not System.Speech) because only SpVoice can choose the audio OUTPUT device.
# NOTE the variable name: it must NOT be $voice. PowerShell variables are case-INSENSITIVE, so
# $voice IS the [string]$Voice parameter above, and assigning a COM object to a string-typed
# variable silently coerces it to "System.__ComObject" - after which every property access fails
# with "The property 'Rate' cannot be found on this object".
$spv = New-Object -ComObject SAPI.SpVoice
$spv.Rate = $Rate
$spv.Volume = [Math]::Max(0, [Math]::Min(100, $Volume))
# Pick a token by name: an EXACT description wins, otherwise the first case-insensitive substring.
# Deliberately NOT -like: a real device name can contain [ or ], which -like reads as a wildcard
# character class, so "Speakers (Realtek(R) Audio)" style names could quietly fail to match.
function Select-Token {
  param($Tokens, [string]$Want)
  $all = @($Tokens)
  foreach ($t in $all) { if ($t.GetDescription() -eq $Want) { return $t } }
  $low = $Want.ToLower()
  foreach ($t in $all) { if ($t.GetDescription().ToLower().Contains($low)) { return $t } }
  return $null
}

if ($Voice -ne '') {
  $vt = Select-Token -Tokens $spv.GetVoices() -Want $Voice
  if ($vt) { $spv.Voice = $vt; Write-Output "WORKER voice=$($vt.GetDescription())" }
  else { Write-Output "WORKER voice-not-found:$Voice (using the default voice)" }
}
if ($Device -ne '') {
  $ot = Select-Token -Tokens $spv.GetAudioOutputs() -Want $Device
  if ($ot) { $spv.AudioOutput = $ot; Write-Output "WORKER device=$($ot.GetDescription())" }
  else { Write-Output "WORKER device-not-found:$Device (using the system default)" }
}

if (-not $NoGate) {
  if (-not ('Say.Fg' -as [type])) {
    Add-Type -Namespace 'Say' -Name 'Fg' -MemberDefinition @"
[DllImport("user32.dll")] public static extern System.IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(System.IntPtr h, out uint pid);
"@
  }
}

function Test-Gated {
  if ($NoGate) { return $false }
  try {
    $h = [Say.Fg]::GetForegroundWindow()
    $fpid = 0
    [Say.Fg]::GetWindowThreadProcessId($h, [ref]$fpid) | Out-Null
    $fp = Get-Process -Id $fpid -ErrorAction SilentlyContinue
    return ($fp -and $fp.ProcessName -match 'claud')
  } catch { return $false }   # detection failed: better to speak than to go silent
}

Write-Output "WORKER ready"
while ($true) {
  $line = [Console]::In.ReadLine()
  if ($null -eq $line) { break }                     # stdin closed: the server has gone
  $line = $line.Trim()
  if ($line -eq '') { continue }
  if ($line -eq '__QUIT__') { break }
  # Control line, not a phrase: change the volume in place. Checked BEFORE the focus gate, because a
  # gated (silent) moment must still keep the worker's volume in step with the app's setting -
  # otherwise the next audible phrase would come out at the old level.
  if ($line -match '^!vol\s+(\d{1,3})$') {
    $spv.Volume = [Math]::Max(0, [Math]::Min(100, [int]$Matches[1]))
    Write-Output "VOL $($spv.Volume)"
    continue
  }
  # A TONE INSTEAD OF A PHRASE ("!wav <path>"), for the mute cues. Played through THIS SpVoice on
  # purpose: it already holds the chosen output device and this voice's volume, so a cue cannot end
  # up on the system default - which on these machines is the desk itself, and would send the cue to
  # the callers instead of to the operator. SpeakStream is how SAPI plays a wave rather than text.
  #
  # SYNCHRONOUS, unlike a phrase. The tone is ~110 ms, and the stream has to stay open until it has
  # played: closing it under an async speak truncates the cue to a click. Blocking the loop for a
  # tenth of a second cannot build a backlog the way a spoken sentence can.
  if ($line -match '^!wav\s+(.+)$') {
    $wav = $Matches[1]
    if (Test-Path -LiteralPath $wav) {
      try {
        $fs = New-Object -ComObject SAPI.SpFileStream
        $fs.Open($wav, 0)          # SSFMOpenForRead
        $spv.SpeakStream($fs, 2)   # 2 = SVSFPurgeBeforeSpeak: the cue beats any queued phrase
        $fs.Close()
        Write-Output "TONE $wav"
      } catch { Write-Output "TONE-FAILED $wav" }
    } else { Write-Output "TONE-MISSING $wav" }
    continue
  }
  if (Test-Gated) { Write-Output "SKIP $line"; continue }
  # 1 = SVSFlagsAsync, 2 = SVSFPurgeBeforeSpeak. Async so this loop keeps reading stdin while the
  # phrase plays; purge so a burst of presses lands on the LATEST state instead of playing out a
  # backlog seconds behind the operator's fingers (which is how a screen reader behaves too).
  $spv.Speak($line, 3) | Out-Null
  Write-Output "SAID $line"
}
