# AccessCaster - one-command build: icon -> frozen exe -> installer.
#
# Produces:
#   ProjectBuilds\dist\AccessCaster\AccessCaster.exe        (the frozen app)
#   ProjectBuilds\Output\AccessCasterSetup-v<version>.exe   (the double-click installer)
#
# Run it from anywhere; it works relative to its own location:
#   powershell -ExecutionPolicy Bypass -File ProjectBuilds\build_installer.ps1
#
# BUILD PREREQUISITES (install once per machine; NOT tracked in the repo):
#   - Python 3.12 on PATH.
#   - pip packages:  pyinstaller, pillow   (python -m pip install pyinstaller pillow)
#   - Inno Setup 6   (winget install --id JRSoftware.InnoSetup -e), at
#                    %LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe
# Missing any of these FAILS LOUDLY below: PowerShell silently ignores a native tool returning
# non-zero, so every native call goes through Invoke-Native.

$ErrorActionPreference = "Stop"

# Run a native command and STOP the build if it returns a non-zero exit code. PowerShell's
# $ErrorActionPreference = "Stop" only traps cmdlet errors, NOT a native exe's exit code, so
# without this a failing python/ISCC call would be ignored and the build would carry on.
function Invoke-Native {
    param([Parameter(Mandatory)][string]$What, [Parameter(Mandatory)][scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed (exit code $LASTEXITCODE). See the output above. " +
              "Check the BUILD PREREQUISITES at the top of this script."
    }
}

# The build runs from the REPO ROOT (parent of ProjectBuilds), so the spec's Project/ and
# ProjectAssets/ source paths resolve.
Set-Location -Path (Split-Path -Parent $PSScriptRoot)

# EVERYTHING THE BUILD GENERATES LIVES UNDER ProjectBuilds\. PyInstaller defaults to dist\ and
# build\ in the working directory, which is the repo root, so it is told otherwise below: without
# these two switches the root collects two generated folders that have nothing to do with the
# source. ProjectBuilds\Output\ (the installer) already worked this way via the .iss OutputDir.
$DistDir = "ProjectBuilds\dist"
$WorkDir = "ProjectBuilds\build"
$AppDir  = "$DistDir\AccessCaster"

# Preflight: python must be on PATH (the whole build shells out to it).
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "python was not found on PATH. Install Python 3.12 and ensure 'python' runs. " +
          "See the BUILD PREREQUISITES at the top of this script."
}

# Read the app version from the SINGLE SOURCE OF TRUTH (Project\version.py). It flows into the
# installer's AppVersion and the output filename (AccessCasterSetup-v<version>.exe). Bump it only
# in version.py; nothing here or in the .iss needs editing per release.
$versionLine = Select-String -Path "Project\version.py" -Pattern '__version__\s*=\s*"([^"]+)"'
if (-not $versionLine) { throw "Could not read __version__ from Project\version.py" }
$AppVer = $versionLine.Matches[0].Groups[1].Value
$SetupExe = "ProjectBuilds\Output\AccessCasterSetup-v$AppVer.exe"
Write-Host "==> Building AccessCaster v$AppVer"

Write-Host "==> 1/3  Generating app icon (Project\assets\icon.ico)..."
Invoke-Native "Icon generation (gen_icon.py; needs the 'pillow' package)" { python ProjectBuilds\gen_icon.py }
if (-not (Test-Path "Project\assets\icon.ico")) {
    throw "gen_icon.py reported success but Project\assets\icon.ico is missing."
}

# The release notes the installer offers to open at the end. GENERATED from Project/version.py
# HISTORY, never hand-written, so there is no second copy of the history to drift from the first.
# Written into Project/ before the freeze because the spec bundles it from there, which is
# also why it is regenerated on every build rather than committed.
Write-Host "==> 2/4  Generating release notes from version.py..."
Invoke-Native "release notes" { python ProjectBuilds\make_release_notes.py Project\ReleaseNotes.html }

Write-Host "==> 3/4  Freezing app with PyInstaller..."
# Delete prior output first so a PyInstaller failure cannot be masked by a stale dist\ exe.
Remove-Item $DistDir, $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
Invoke-Native "PyInstaller freeze (needs the 'pyinstaller' package)" {
    python -m PyInstaller --noconfirm --clean --distpath $DistDir --workpath $WorkDir `
        ProjectBuilds\AccessCaster.spec
}
if (-not (Test-Path "$AppDir\AccessCaster.exe")) {
    throw "PyInstaller did not produce $AppDir\AccessCaster.exe"
}
# THE RELEASE NOTES GO BESIDE THE EXE, NOT THROUGH THE SPEC. Bundled as a PyInstaller `data` they
# land in _internal\, because that is where PyInstaller 6 puts them - and the installer's
# post-install action opens {app}\ReleaseNotes.html, which would then not exist, so ticking "See
# what is new" silently does nothing.
# They are a document for the reader rather than an asset for the app, so the app folder is also
# where someone would look for them later.
Copy-Item "Project\ReleaseNotes.html" "$AppDir\ReleaseNotes.html" -Force
if (-not (Test-Path "$AppDir\ReleaseNotes.html")) {
    throw "Release notes did not reach $AppDir\ (the installer would offer a missing file)"
}

Write-Host "==> 4/4  Compiling installer with Inno Setup..."
$iscc = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) {
    throw "Inno Setup compiler not found at $iscc. " +
          "Install it: winget install --id JRSoftware.InnoSetup -e"
}
Invoke-Native "Inno Setup compile" { & $iscc "/DAppVer=$AppVer" "ProjectBuilds\installer.iss" }
if (-not (Test-Path $SetupExe)) {
    throw "Inno Setup did not produce $SetupExe"
}

Write-Host ""
Write-Host "Done. Installer: $SetupExe"
