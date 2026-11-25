#!/bin/zsh
# Build AccessCaster.app on a Mac: ProjectBuilds/dist/AccessCaster.app.
#
#     zsh ProjectBuilds/build_mac.sh
#
# The Mac counterpart of build_installer.ps1's first three steps: a venv of its own built from the
# pins, the pin check, the icon, and the freeze. The app is signed ad hoc, which runs on the
# machine that built it; signing with a Developer ID, notarising and a disk image are not built.
#
# THE BASE PYTHON DECIDES THE OLDEST macOS THE APP RUNS ON. A Homebrew Python is built for the
# macOS it was installed on, and the app inherits that floor; python.org's universal2 installer
# targets far older systems and both kinds of Mac. Set PYTHON to choose it, for example
#     PYTHON=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12
set -euo pipefail

cd "${0:A:h}/.."

venv=ProjectBuilds/.venv
py=$venv/bin/python
stamp=$venv/requirements.sha256
base=${PYTHON:-}
if [[ -z $base ]]; then
    for candidate in python3.12 /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12; do
        if command -v $candidate >/dev/null 2>&1; then base=$candidate; break; fi
    done
fi
[[ -n $base ]] || { print -u2 "Python 3.12 was not found. Set PYTHON to its path."; exit 1; }

# The pins' fingerprint, written only after an install succeeds, so an interrupted install leaves
# no stamp and the next build starts the venv again.
pins=$(cat requirements.txt ProjectBuilds/requirements-build.txt | shasum -a 256 | cut -d' ' -f1)
if [[ ! -x $py || ! -f $stamp || $(<$stamp) != $pins ]]; then
    print "==> Creating the build environment ($venv) from the pinned requirements..."
    rm -rf $venv
    $base -m venv $venv
    $py -m pip install --disable-pip-version-check -r ProjectBuilds/requirements-build.txt
    print -r -- $pins > $stamp
fi
$py ProjectBuilds/check_build_env.py

version=$(sed -nE 's/^__version__ *= *"([^"]+)".*/\1/p' Project/version.py)
[[ -n $version ]] || { print -u2 "Could not read __version__ from Project/version.py"; exit 1; }
print "==> Building AccessCaster $version"

print "==> 1/2  Generating the app icon..."
$py ProjectBuilds/gen_icon.py

print "==> 2/2  Freezing the app with PyInstaller..."
rm -rf ProjectBuilds/dist/AccessCaster ProjectBuilds/dist/AccessCaster.app
$py -m PyInstaller --noconfirm --clean --distpath ProjectBuilds/dist \
    --workpath ProjectBuilds/build ProjectBuilds/AccessCaster.spec
app=ProjectBuilds/dist/AccessCaster.app
[[ -x $app/Contents/MacOS/AccessCaster ]] || { print -u2 "PyInstaller did not produce $app"; exit 1; }
codesign --verify --deep --strict $app
print "==> Built $app"
