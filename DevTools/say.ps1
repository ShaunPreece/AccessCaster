# say.ps1 - speak one phrase through SAPI and exit, for DevTools scripts. The app's own speech is
# Project/speech.py; this is not part of it.
#
#   powershell -File say.ps1 -Text "words" [-Wav cue.wav] [-Rate 3] [-Voice Zira] [-Device "Realtek"]
#              [-Volume 100]
#
# Speaks -Text (or plays -Wav, falling back to -Text when the file cannot be played) and exits once it
# has finished, printing "SAY spoken" or "SAY tone". Check for that line rather than assuming, in
# anything that uses sound as evidence.
#
# -Volume is SAPI's own 0-100 volume. It scales ONLY this voice's output stream: it does not touch
# Windows system volume, other applications, or the desk's audio.
#
# -Device picks WHICH Windows output the speech goes to. That matters when the default output is the
# RODECaster itself: anything spoken through it disappears the moment you SOLO another channel or
# mute USB 1, because the cue bus replaces the headphone feed and the speech arrives on USB 1.
param(
  [Parameter(Mandatory=$true)][string]$Text,
  # A WAV to play INSTEAD of speaking $Text. $Text is what it would have said, so a missing or
  # unreadable file degrades to speech rather than to silence.
  [string]$Wav = '',
  [int]$Rate = 3,            # SAPI rate, -10 (slow) .. 10 (fast)
  [string]$Voice = '',       # voice name, exact or partial, e.g. 'Zira'
  [string]$Device = '',      # output device name, exact or partial; '' = the system default
  [int]$Volume = 100         # SAPI stream volume, 0 (silent) .. 100 (full); this voice only
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
  if ($vt) { $spv.Voice = $vt }
}
if ($Device -ne '') {
  $ot = Select-Token -Tokens $spv.GetAudioOutputs() -Want $Device
  if ($ot) { $spv.AudioOutput = $ot }
}

if ($Wav -ne '' -and (Test-Path -LiteralPath $Wav)) {
  try {
    # SYNCHRONOUS: the stream has to stay open until the cue has played.
    $fs = New-Object -ComObject SAPI.SpFileStream
    $fs.Open($Wav, 0)              # SSFMOpenForRead
    $spv.SpeakStream($fs, 0) | Out-Null
    $fs.Close()
    Write-Output "SAY tone"
    exit 0
  } catch { Write-Output "SAY tone-failed, speaking the words instead" }
}
$spv.Speak($Text) | Out-Null      # synchronous: the process must not exit mid-phrase
Write-Output "SAY spoken"
