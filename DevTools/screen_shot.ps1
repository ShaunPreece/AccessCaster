# Photograph the RODECaster's touchscreen with the Insta360 Link 2C Pro.
#
# WHY THE FIVE-SECOND CLIP: a single -frames:v 1 grab returns the first frame the camera emits,
# which is always out of focus because autofocus has not settled. Recording a short clip and
# taking the LAST frame is the only reliable way to get a readable screen.
#
#   powershell -File DevTools/screen_shot.ps1 -Name storage-screen
#
param(
    [string]$Name = "screen",
    [string]$Device = "Insta360 Link 2C Pro",
    [int]$Seconds = 5
)

$out = Join-Path $PSScriptRoot "captures\touchscreen"
if (-not (Test-Path $out)) { New-Item -ItemType Directory -Path $out -Force | Out-Null }

$clip = Join-Path $out "_warm.mkv"
$png  = Join-Path $out "$Name.png"

ffmpeg -hide_banner -loglevel error -y -f dshow -video_size 1920x1080 -framerate 30 `
    -rtbufsize 200M -i "video=$Device" -t $Seconds $clip
ffmpeg -hide_banner -loglevel error -y -sseof -0.5 -i $clip -frames:v 1 $png
Remove-Item $clip -ErrorAction SilentlyContinue

if (Test-Path $png) { Write-Output $png } else { Write-Output "capture failed" }
