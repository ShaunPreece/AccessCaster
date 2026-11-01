# say_gated.ps1 - speak a status line through say.ps1, unless the Claude GUI has the focus.
#
# For a development session driven through Claudio: while Claudio is the foreground window, the
# screen reader is already reading the chat, so speaking would talk over it. When another app has
# the focus (during a self-driven run), the line is spoken. This gate is a personal convention for
# working with Claude, not part of the app, which is why it lives here and not in say.ps1.
#
# Usage: powershell -ExecutionPolicy Bypass -File DevTools/say_gated.ps1 -Text "your message"
#        [-Force] [-Wav cue.wav] [-Rate 3] [-Voice Zira] [-Device "Realtek"] [-Volume 100]
param(
  [Parameter(Mandatory=$true)][string]$Text,
  [string]$Wav = '',
  [int]$Rate = 3,
  [string]$Voice = '',
  [string]$Device = '',
  [int]$Volume = 100,
  [switch]$Force             # speak even if Claudio is in focus
)

if (-not $Force) {
  try {
    if (-not ('SayGated.Fg' -as [type])) {
      Add-Type -Namespace 'SayGated' -Name 'Fg' -MemberDefinition @"
[DllImport("user32.dll")] public static extern System.IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(System.IntPtr h, out uint pid);
"@
    }
    $fpid = 0
    [SayGated.Fg]::GetWindowThreadProcessId([SayGated.Fg]::GetForegroundWindow(), [ref]$fpid) | Out-Null
    $fp = Get-Process -Id $fpid -ErrorAction SilentlyContinue
    if ($fp -and $fp.ProcessName -match 'claud') {
      Write-Output "SAY skipped (Claudio in focus)"
      return
    }
  } catch {
    Write-Output "SAY focus check failed ($($_.Exception.Message)), speaking anyway"
  }
}

$pass = @{} + $PSBoundParameters
$pass.Remove('Force')
& (Join-Path $PSScriptRoot 'say.ps1') @pass
