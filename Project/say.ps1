# say.ps1 - the app's speech, through SAPI. Two ways to run it:
#
#   ONE PHRASE: powershell -File say.ps1 -Text "words" [-Wav cue.wav] [-Rate 3] [-Voice Zira]
#               [-Device "Realtek"] [-Volume 100]
#     Speaks -Text (or plays -Wav, falling back to -Text when the file cannot be played) and exits
#     once it has finished, printing "SAY spoken" or "SAY tone". The announcer's fallback when the
#     worker cannot start, and the one-shot for DevTools scripts.
#
#   THE WORKER: powershell -File say.ps1 [-Rate 3] [-Voice Zira] [-Device "Realtek"] [-Volume 100]
#     Reads one phrase per line from stdin and speaks it; EOF or "__QUIT__" exits. A one-shot pays
#     PowerShell startup plus loading the speech stack, 1.3 to 1.7 seconds, on every phrase: the
#     announcer speaks on every mute and solo press, so one-shots lag, then fight each other for the
#     audio device. The worker pays it once. Control lines: "!vol N" sets the volume in place,
#     "!wav <path>" plays a cue, "!sync <id> <text>" speaks and then answers "DONE <id>".
#
# -Volume is SAPI's own 0-100 volume. It scales ONLY this voice's output stream: it does not touch
# Windows system volume, other applications, or the desk's audio.
#
# -Device picks WHICH Windows output the speech goes to. That matters when the default output is
# the RODECaster itself: anything spoken through it disappears the moment you SOLO another channel
# or mute USB 1, because the cue bus replaces the headphone feed and our speech arrives on USB 1.
# It also keeps a one-shot off the system default, which on these machines is often the desk, where
# an announcement would leak into the mix and the recording.
param(
  [string]$Text = '',        # one phrase to speak; without it this runs as the stdin worker
  # A WAV to play INSTEAD of speaking $Text, for the announcer's mute cues. Same SpVoice, so it
  # inherits -Device and -Volume. $Text is what this call would have said, so a missing or
  # unreadable file degrades to speech rather than to silence.
  [string]$Wav = '',
  [int]$Rate = 3,            # SAPI rate, -10 (slow) .. 10 (fast)
  [string]$Voice = '',       # voice name, exact or partial, e.g. 'Zira'
  [string]$Device = '',      # output device name, exact or partial; '' = the system default
  [int]$Volume = 100         # SAPI stream volume, 0 (silent) .. 100 (full); this voice only
)

$ErrorActionPreference = 'Continue'
$oneShot = $PSBoundParameters.ContainsKey('Text')

# SAPI COM (not System.Speech) because only SpVoice can choose the audio OUTPUT device, and it names
# voices exactly as the app's own list does.
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
  if ($vt) { $spv.Voice = $vt }
  if (-not $oneShot) {
    if ($vt) { Write-Output "WORKER voice=$($vt.GetDescription())" }
    else { Write-Output "WORKER voice-not-found:$Voice (using the default voice)" }
  }
}
if ($Device -ne '') {
  $ot = Select-Token -Tokens $spv.GetAudioOutputs() -Want $Device
  if ($ot) { $spv.AudioOutput = $ot }
  if (-not $oneShot) {
    if ($ot) { Write-Output "WORKER device=$($ot.GetDescription())" }
    else { Write-Output "WORKER device-not-found:$Device (using the system default)" }
  }
}

# Play a WAV through this SpVoice, so it keeps the chosen output and volume. SYNCHRONOUS: the stream
# has to stay open until the cue has played, and closing it under an async speak truncates the cue
# to a click. $flags 2 (SVSFPurgeBeforeSpeak) lets a cue beat any queued phrase.
function Invoke-Wav {
  param([string]$Path, [int]$Flags = 0)
  $fs = New-Object -ComObject SAPI.SpFileStream
  $fs.Open($Path, 0)              # SSFMOpenForRead
  $spv.SpeakStream($fs, $Flags) | Out-Null
  $fs.Close()
}

if ($oneShot) {
  if ($Wav -ne '' -and (Test-Path -LiteralPath $Wav)) {
    try {
      Invoke-Wav -Path $Wav
      Write-Output "SAY tone"
      exit 0
    } catch { Write-Output "SAY tone-failed, speaking the words instead" }
  }
  $spv.Speak($Text) | Out-Null      # synchronous: the process must not exit mid-phrase
  Write-Output "SAY spoken"
  exit 0
}

Write-Output "WORKER ready"
while ($true) {
  $line = [Console]::In.ReadLine()
  if ($null -eq $line) { break }                     # stdin closed: the server has gone
  $line = $line.Trim()
  if ($line -eq '') { continue }
  if ($line -eq '__QUIT__') { break }
  # Control line, not a phrase: change the volume in place, so the server never has to restart the
  # worker (and pay the first-phrase startup cost again) just to get quieter.
  if ($line -match '^!vol\s+(\d{1,3})$') {
    $spv.Volume = [Math]::Max(0, [Math]::Min(100, [int]$Matches[1]))
    Write-Output "VOL $($spv.Volume)"
    continue
  }
  # A TONE INSTEAD OF A PHRASE ("!wav <path>"), for the mute cues. Played through THIS SpVoice on
  # purpose: it already holds the chosen output device and this voice's volume, so a cue cannot end
  # up on the system default - which on these machines is the desk itself, and would send the cue to
  # the callers instead of to the operator. Blocking the loop for the ~110 ms cue cannot build a
  # backlog the way a spoken sentence can.
  if ($line -match '^!wav\s+(.+)$') {
    $wav = $Matches[1]
    if (Test-Path -LiteralPath $wav) {
      try { Invoke-Wav -Path $wav -Flags 2; Write-Output "TONE $wav" }
      catch { Write-Output "TONE-FAILED $wav" }
    } else { Write-Output "TONE-MISSING $wav" }
    continue
  }
  # A PHRASE SOMEBODY WAITS FOR ("!sync <id> <text>"): spoken SYNCHRONOUSLY, then "DONE <id>", so
  # the server can act only once the words have finished. DONE is written straight to the console
  # stream and flushed, because the server is blocked on it.
  if ($line -match '^!sync\s+(\S+)\s+(.+)$') {
    $sid = $Matches[1]
    $spv.Speak($Matches[2], 2) | Out-Null   # 2 = purge, not async
    [Console]::Out.WriteLine("DONE $sid"); [Console]::Out.Flush()
    continue
  }
  # 1 = SVSFlagsAsync, 2 = SVSFPurgeBeforeSpeak. Async so this loop keeps reading stdin while the
  # phrase plays; purge so a burst of presses lands on the LATEST state instead of playing out a
  # backlog seconds behind the operator's fingers (which is how a screen reader behaves too).
  $spv.Speak($line, 3) | Out-Null
  Write-Output "SAID $line"
}
