# say.ps1 - speak a message out loud via the built-in Windows SAPI5 voice.
# Used so the user (a screen-reader user) can hear Claude's status while a self-driven app
# (e.g. the RODECaster App) is foregrounded and NVDA is reading THAT app, not the terminal.
#
# SELF-GATING: only speaks when the Claude GUI window ("Claudio") is NOT the foreground
# window. When Claudio IS focused, the screen reader already announces the chat, so speaking
# would talk over it. So this is a no-op unless an app has stolen focus (i.e. during self-drive).
# Pass -Force to speak regardless (e.g. for testing).
# Usage: powershell -File say.ps1 -Text "your message"
#          [-Rate 3] [-Voice "Zira"] [-Device "Realtek"] [-Volume 100] [-Force]
#
# -Volume is the synthesiser's own 0-100 volume. It scales ONLY this speech stream, so it cannot
# change Windows system volume, other applications, or the desk's audio.
#
# -Voice and -Device take the same names as say_worker.ps1 and are matched the same way (exact
# first, then case-insensitive substring). They are here because this script is the FALLBACK the
# server drops to when the persistent worker cannot start: without them, a fallback phrase would
# come out in the wrong voice and, worse, through the system default output, which on this machine
# is the RODECaster itself - so announcements would leak into the mix and the recording.
param(
  [Parameter(Mandatory=$true)][string]$Text,
  # A WAV to play INSTEAD of speaking $Text, for the announcer's mute cues. Same SpVoice, so it
  # inherits -Device and -Volume: a cue must not fall back to the system default output, which on
  # these machines is the desk itself. $Text is still required and is what this call would have
  # said, so a missing or unreadable file degrades to speech rather than to silence.
  [string]$Wav = '',
  [int]$Rate = 3,          # -10 (slow) .. 10 (fast); 3 is a bit brisker than default
  [string]$Voice = '',     # voice name, exact or partial, e.g. 'Zira', 'David', 'Hazel'
  [string]$Device = '',    # output device name, exact or partial; '' = the system default
  [int]$Volume = 100,      # 0 (silent) .. 100 (full); this speech stream only
  [switch]$Force           # speak even if Claudio is in focus
)

if (-not $Force) {
  try {
    if (-not ('Say.Fg' -as [type])) {
      Add-Type -Namespace 'Say' -Name 'Fg' -MemberDefinition @"
[DllImport("user32.dll")] public static extern System.IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(System.IntPtr h, out uint pid);
"@
    }
    $h = [Say.Fg]::GetForegroundWindow()
    $fpid = 0
    [Say.Fg]::GetWindowThreadProcessId($h, [ref]$fpid) | Out-Null
    $fp = Get-Process -Id $fpid -ErrorAction SilentlyContinue
    if ($fp -and $fp.ProcessName -match 'claud') {
      # Claude GUI is focused -> the screen reader is announcing; stay silent.
      Write-Output "SAY skipped (Claudio in focus)"
      return
    }
  } catch { }   # if detection fails, fall through and speak
}

# SAPI COM rather than System.Speech: only SpVoice can choose the audio OUTPUT device, and it names
# voices exactly as the app's own list does (System.Speech reports a shorter name, so a name saved
# from the app would not have matched here).
# NOTE the variable name: it must NOT be $voice, which IS the [string]$Voice parameter above, since
# PowerShell variables are case-insensitive; assigning the COM object to it coerces it to a string
# and every later property access fails.
$spv = New-Object -ComObject SAPI.SpVoice
$spv.Rate = $Rate
$spv.Volume = [Math]::Max(0, [Math]::Min(100, $Volume))

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
    $fs = New-Object -ComObject SAPI.SpFileStream
    $fs.Open($Wav, 0)              # SSFMOpenForRead
    $spv.SpeakStream($fs) | Out-Null   # synchronous, like the speech below: the process must not
    $fs.Close()                        # exit, nor the stream close, while the cue is still playing
    Write-Output "SAY tone"
    exit 0
  } catch { }                      # fall through and say the words instead
}
$spv.Speak($Text) | Out-Null      # synchronous: the process must not exit mid-phrase
Write-Output "SAY spoken"
