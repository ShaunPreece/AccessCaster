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
#   - Python 3.12, reachable as `py -3.12` (the python.org installer's launcher) or as `python`.
#   - Inno Setup 6   (winget install --id JRSoftware.InnoSetup -e), at
#                    %LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe
# Missing either FAILS LOUDLY below: PowerShell silently ignores a native tool returning non-zero,
# so every native call goes through Invoke-Native.
#
# THE BUILD RUNS IN ITS OWN VIRTUAL ENVIRONMENT, ProjectBuilds\.venv (gitignored), never the
# machine's global Python. PyInstaller bundles whatever the running Python has installed and only
# warns about an import it cannot find, so a global install missing a package produces an exe that
# dies on launch. The venv holds exactly the pins in requirements-build.txt and its includes, and is
# rebuilt from scratch whenever either requirements file changes.

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
# source. ProjectBuilds\Output\ (the installer) is set by the .iss OutputDir.
$DistDir = "ProjectBuilds\dist"
$WorkDir = "ProjectBuilds\build"
$AppDir  = "$DistDir\AccessCaster"

$VenvDir = "ProjectBuilds\.venv"
$Py      = "$VenvDir\Scripts\python.exe"
# Fingerprint of the pins. Written into the venv only after its install succeeds, so an interrupted
# install leaves no stamp and the next build starts the venv again from scratch.
$Stamp   = "$VenvDir\requirements.sha256"
$ReqFiles = @("requirements.txt", "ProjectBuilds\requirements-build.txt")

# The Python 3.12 the venv is created from, as the command and arguments that run it.
function Find-BasePython {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.12 -c "import sys" 2>$null
        if ($LASTEXITCODE -eq 0) { return @("py", "-3.12") }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $ver = & python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver -eq "3.12") { return @("python") }
    }
    throw "Python 3.12 was not found as 'py -3.12' or 'python'. Install Python 3.12. " +
          "See the BUILD PREREQUISITES at the top of this script."
}

$pins = ($ReqFiles | ForEach-Object { (Get-FileHash $_ -Algorithm SHA256).Hash }) -join ""
$current = if (Test-Path $Stamp) { (Get-Content $Stamp -Raw).Trim() } else { "" }
if (-not (Test-Path $Py) -or $current -ne $pins) {
    Write-Host "==> Creating the build environment ($VenvDir) from the pinned requirements..."
    # A venv with a Python still running from it (DevTools\simulate_desk.py, say) cannot be
    # deleted, and a new one created over the remains is broken in ways the check cannot see.
    if (Test-Path $VenvDir) {
        try { Remove-Item $VenvDir -Recurse -Force -ErrorAction Stop }
        catch {
            throw "Could not delete $VenvDir to rebuild it: a program is probably running from " +
                  "it, such as DevTools\simulate_desk.py. Quit it and build again. ($_)"
        }
    }
    # @() because PowerShell unrolls a one-element array returned from a function into a string.
    $base = @(Find-BasePython)
    $baseArgs = @($base | Select-Object -Skip 1)
    Invoke-Native "Creating the build environment" { & $base[0] @baseArgs -m venv $VenvDir }
    Invoke-Native "Installing the pinned requirements" {
        & $Py -m pip install --disable-pip-version-check -r ProjectBuilds\requirements-build.txt
    }
    Set-Content -Path $Stamp -Value $pins -Encoding ascii
}

# Every pin installed at its pinned version, on Python 3.12. A stamp proves the files have not
# changed, not that the venv still matches them.
Invoke-Native "Build requirements check (check_build_env.py)" { & $Py ProjectBuilds\check_build_env.py }

# Read the app version from the SINGLE SOURCE OF TRUTH (Project\version.py). It flows into the
# installer's AppVersion and the output filename (AccessCasterSetup-v<version>.exe). Bump it only
# in version.py; nothing here or in the .iss needs editing per release.
$versionLine = Select-String -Path "Project\version.py" -Pattern '__version__\s*=\s*"([^"]+)"'
if (-not $versionLine) { throw "Could not read __version__ from Project\version.py" }
$AppVer = $versionLine.Matches[0].Groups[1].Value
$SetupExe = "ProjectBuilds\Output\AccessCasterSetup-v$AppVer.exe"
Write-Host "==> Building AccessCaster v$AppVer"

Write-Host "==> 1/4  Generating app icon (Project\assets\icon.ico)..."
Invoke-Native "Icon generation (gen_icon.py; needs the 'pillow' package)" { & $Py ProjectBuilds\gen_icon.py }
if (-not (Test-Path "Project\assets\icon.ico")) {
    throw "gen_icon.py reported success but Project\assets\icon.ico is missing."
}

# This version's release notes, GENERATED from Project/version.py HISTORY: the installer shows them
# on a page of their own, and the update publisher (UpdateKit) reads them from the same file.
Write-Host "==> 2/4  Generating release notes from version.py..."
Invoke-Native "release notes" {
    & $Py ProjectBuilds\make_release_notes.py ProjectBuilds\release-notes.txt
}

Write-Host "==> 3/4  Freezing app with PyInstaller..."
# Delete prior output first so a PyInstaller failure cannot be masked by a stale dist\ exe.
Remove-Item $DistDir, $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
Invoke-Native "PyInstaller freeze (needs the 'pyinstaller' package)" {
    & $Py -m PyInstaller --noconfirm --clean --distpath $DistDir --workpath $WorkDir `
        ProjectBuilds\AccessCaster.spec
}
if (-not (Test-Path "$AppDir\AccessCaster.exe")) {
    throw "PyInstaller did not produce $AppDir\AccessCaster.exe"
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
