# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for AccessCaster (Windows tray app).
#
# Entry point is tray.py: it starts the local server, opens the accessible web UI, and parks the
# app in the system tray. Built as onedir (a folder with AccessCaster.exe + an _internal/ dir),
# which is faster to start than onefile and lets the bundled assets sit on disk for inspection.
# console=False so there is no console window (the pythonw equivalent).
#
# Read-only assets are bundled via `datas` and resolved at runtime by paths.resource_path (which
# reads from sys._MEIPASS when frozen). User data (profiles, presets, settings.json) is NOT
# bundled: it is created in %LOCALAPPDATA%\AccessCaster on first use (see paths.data_path).

# PyInstaller resolves relative paths in a spec against the SPEC FILE's own directory, not the
# working directory. SPECPATH is that directory (ProjectBuilds/); the repo root is its parent.
# We build ABSOLUTE paths from it so the build works regardless of where it is invoked from.
import os
import re
_ROOT = os.path.dirname(SPECPATH)
_PROJ = os.path.join(_ROOT, 'Project')          # the app code + static/ + say.ps1 + assets/
_ASSETS = os.path.join(_ROOT, 'ProjectAssets')  # device data: fx_recipes, mic_presets

# ---------------------------------------------------------------------------
# WINDOWS VERSION INFO, GENERATED FROM Project/version.py AT BUILD TIME.
#
# WHY IT IS GENERATED RATHER THAN WRITTEN OUT. Until 3rd September 2026 the frozen exe carried NO
# version resource at all: file properties, Task Manager and Programs and Features all showed a
# blank version, and the only place the number appeared was the installer's filename. The obvious
# fix is a hand-written version resource file, and it is the wrong one - it would be a SECOND place
# holding the number, and version.py's whole contract is that there is exactly one. So the spec,
# which is ordinary Python executed at build time, reads that file and writes the resource itself.
#
# READ WITH A REGEX, NOT AN IMPORT: importing anything out of Project/ during a spec parse drags
# sys.path games into the build for a single string, and version.py is deliberately import-free.
#
# IT RAISES RATHER THAN DEFAULTING. A malformed version must break the build, not quietly ship an
# exe stamped 0.0.0.0 - that is the same reasoning build_installer.ps1 uses for a missing tool.
_vpath = os.path.join(_PROJ, 'version.py')
with open(_vpath, encoding='utf-8') as fh:
    _m = re.search(r'^__version__\s*=\s*"([^"]+)"', fh.read(), re.M)
if not _m:
    raise SystemExit(f'AccessCaster.spec: could not read __version__ from {_vpath}')
_VERSION = _m.group(1)

# "MAJOR.MINOR" with a TWO-DIGIT MINOR, so 2.50 is version 2.5 and becomes (2, 50, 0, 0). That
# ordering is the point: Windows compares these fields numerically, so 2.50 correctly outranks 2.10.
try:
    _parts = [int(p) for p in _VERSION.split('.')]
except ValueError:
    raise SystemExit(f'AccessCaster.spec: __version__ {_VERSION!r} is not numeric, so no Windows '
                     f'version resource can be built from it')
_vers = tuple((_parts + [0, 0, 0, 0])[:4])

# 040904B0 = US English + Unicode, matching the Translation pair (1033, 1200) below.
_VERSION_RES = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={_vers}, prodvers={_vers}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
        StringStruct('CompanyName', 'Shaun Preece'),
        StringStruct('FileDescription', 'AccessCaster'),
        StringStruct('FileVersion', '{_VERSION}'),
        StringStruct('InternalName', 'AccessCaster'),
        StringStruct('OriginalFilename', 'AccessCaster.exe'),
        StringStruct('ProductName', 'AccessCaster'),
        StringStruct('ProductVersion', '{_VERSION}'),
    ])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ]
)
"""
# Written beside the spec and GITIGNORED. It is left on disk rather than made in a temp file so a
# failed build can be inspected: "what did the exe actually get stamped with" is otherwise
# unanswerable after the fact.
_VERSION_FILE = os.path.join(SPECPATH, 'version_info.generated.txt')
with open(_VERSION_FILE, 'w', encoding='utf-8') as fh:
    fh.write(_VERSION_RES)

# Source files live under Project/ and ProjectAssets/, but are bundled FLAT into the frozen app so
# paths.resource_path finds them under sys._MEIPASS unchanged.
datas = [
    (os.path.join(_PROJ, 'static'), 'static'),                  # the accessible web UI
    # NO FACTORY SMART-PAD WAVs. They are RODE's audio, the app does not restore the factory
    # SOUNDS at all, and the unit keeps its own copies - a factory reset is what puts those back.
    # The two JSON files below are PARAMETERS read off a device rather than recordings, which is a
    # different question, and they are what the effects and mixer restore is built from.
    (os.path.join(_ASSETS, 'mic_presets.json'), '.'),          # factory mic-processing presets
    (os.path.join(_ASSETS, 'fx_recipes.json'), '.'),           # factory voice-FX recipes
    (os.path.join(_PROJ, 'say.ps1'), '.'),                     # SAPI speech helper (one-shot fallback)
    (os.path.join(_PROJ, 'say_worker.ps1'), '.'),              # persistent speech worker (announcer)
    (os.path.join(_PROJ, 'assets', 'icon.ico'), 'assets'),     # tray / window / exe icon
]

# hid links hidapi statically into its .pyd, so no extra DLL is needed; listed here (with the
# pywin32 tray modules) so the analysis never drops them even if import detection misses one.
# comtypes is the level meters' only dependency (Project/meters.py talks to Windows' own audio
# endpoints through it). It builds its COM interface wrappers at RUN time, so PyInstaller's import
# detection sees less of it than it needs - name it, and its client module, explicitly.
hiddenimports = ['hid', 'win32gui', 'win32con', 'win32api',
                 'comtypes', 'comtypes.client', 'comtypes.stream']


a = Analysis(
    [os.path.join(_PROJ, 'tray.py')],
    pathex=[_PROJ],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AccessCaster',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=os.path.join(_PROJ, 'assets', 'icon.ico'),
    version=_VERSION_FILE,       # see the generated-version-info block at the top of this file
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='AccessCaster',
)
