"""Make the Project modules importable from a DevTools script, and locate repo files.

Import this FIRST in any DevTools script that uses rcp_read / rcp_transfer / soundpad / channels:

    import _bootstrap                      # noqa: F401  (adds Project/ to sys.path)
    import rcp_read as rr

Without it, `python DevTools/foo.py` dies with ImportError unless PYTHONPATH is set by hand. It is
also where repo-relative paths come from, so nothing hardcodes a machine-specific path: the same
scripts run on a second machine under a different user name.
"""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT = os.path.join(REPO, "Project")
DEVTOOLS = os.path.join(REPO, "DevTools")

if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

# A TEST NEVER WRITES TO THE USER'S REAL DATA. Anything run as `test_*.py` gets its own throwaway
# data dir, set BEFORE the first Project import so `paths.DATA_DIR` and every constant derived from
# it (profiles, schemes, parked chains, app settings) land there instead of %LOCALAPPDATA%. Without
# it, a sweep of the offline suites rewrites the user's real `off_fader_chains.json` and leaves
# profiles in his real profiles folder.
#
# A TEST NEVER MAKES A NOISE EITHER. A suite that does not stub `_ann_say` leaves the real announcer
# in place, so anything it exercises SPEAKS OR CHIMES through SAPI, onto the desk the user monitors
# on, with nothing to say what is making the sound, possibly mid-recording. `test_shows_sim`
# completes dozens of background jobs and fires the done/failed cue for every one; `test_hotkeys`
# dispatches real shortcut actions and speaks their results.
#
# AND A TEST NEVER WRITES TO THE DESK, which is the same rule a third time and the one that costs
# most when it is missed. An offline suite that reaches a real write fires a pad, engages a voice
# effect on a live mic or drops the audio on a desk somebody is working on - and it PASSES while it
# does it, because the assertion under test was about the decision, not the wire. The guard raises
# at the write (rcp_hid.refuse_write_in_tests, midi_out.cc) rather than swallowing it, so a suite
# that needs a device call is forced to stub it rather than quietly proving nothing.
#
# A `test_*_live` suite that genuinely means to drive hardware sets ACCESSCASTER_ALLOW_DEVICE_WRITE
# for itself, which is an opt-in the live suites can reasonably be asked to make: they exist to
# touch the desk and say so in their names.
#
# All three key off the script NAME rather than asking a test to opt in: an opt-in is what a new
# test forgets, and forgetting must not be able to cost anything.
#
# The dev server, the probes and the capture tools are NOT tests and keep the real data dir, which
# is the point of running them. Set ACCESSCASTER_DATA_DIR yourself to override either way.
if os.path.basename(sys.argv[0] or "").startswith("test_"):
    os.environ.setdefault(
        "ACCESSCASTER_DATA_DIR",
        os.path.join(tempfile.gettempdir(), "AccessCaster-test-data"))
    os.environ.setdefault("ACCESSCASTER_SILENT", "1")
    os.environ.setdefault("ACCESSCASTER_NO_DEVICE_WRITE", "1")
    if os.path.basename(sys.argv[0] or "").endswith("_live.py"):
        os.environ.setdefault("ACCESSCASTER_ALLOW_DEVICE_WRITE", "1")


def repo_path(*parts: str) -> str:
    """An absolute path to something in the repo, e.g. repo_path('ProjectAssets', 'fx_recipes.json')."""
    return os.path.join(REPO, *parts)


def docs_path(*parts: str) -> str:
    """An absolute path inside the PUBLIC reference docs, e.g. docs_path('CodeMap.md').

    `docs/` holds the hardware FINDINGS and ships with the code: the protocol guide, the generated
    per-desk property lists, the parity target, the code map and the soundpad reference. Use this
    for anything a stranger recreating the project would need.

    USE `notes_path` INSTEAD for anything carrying decisions, quotes or open work. That is the
    private repo, and the distinction is the whole reason there are two functions: a file of facts
    about the desk belongs with the code, while the conversation that produced it does not. Getting
    it the wrong way round either publishes a note or hides a finding.
    """
    return os.path.join(REPO, "docs", *parts)


# THE NOTES LIVE OUTSIDE THIS REPO, in a private sibling one, so this repo can be public without
# publishing them: `Claude Projects\_Notes\<project folder name>\`. Same folder name as the project,
# which is how a script finds them without anything being configured.
NOTES = os.path.join(os.path.dirname(REPO), "_Notes", os.path.basename(REPO))


def notes_path(*parts: str) -> str:
    """An absolute path inside this project's notes, e.g. notes_path('ProjectMemory', 'CodeMap.md').

    FALLS BACK TO THE REPO when the notes are not beside it, which is what a fresh public clone
    looks like. The generators (property_list, the coverage reports, codemap_lines) then write
    their output into the clone instead of failing on a folder that is not there.
    """
    base = NOTES if os.path.isdir(NOTES) else REPO
    return os.path.join(base, *parts)


def devtools_path(*parts: str) -> str:
    """An absolute path beside the DevTools scripts (captures, cached maps, probe output)."""
    return os.path.join(DEVTOOLS, *parts)


EXPENDABLE_FLAG = "--expendable-desk"


def require_expendable_desk(what: str) -> None:
    """Refuse to run unless `--expendable-desk` is on the command line. Call it BEFORE anything is
    sent, naming what the script will do to the desk.

    FOR SCRIPTS THAT FACTORY-RESET. It is a flag rather than a warning in the header because a
    sweep that runs every offline test reads no headers: "expendable desks only" in capitals at the
    top of `test_usb_drop_live.py` stops nothing, and the script factory-resets a working desk ten times
    over. A warning protects a reader; a flag protects the desk.
    """
    if EXPENDABLE_FLAG not in sys.argv:
        sys.stderr.write(
            f"\nREFUSING TO RUN: this script {what}.\n"
            f"There is no undo, and it is wrong for a desk anybody works on.\n"
            f"Back the unit up first, then pass {EXPENDABLE_FLAG} if you meant it.\n\n")
        raise SystemExit(2)


def test_wav(name: str = "accesscaster-test-tone.wav", seconds: float = 0.5) -> str:
    """A small WAV to upload in a round-trip test, GENERATED rather than shipped.

    THE REPO CARRIES NO AUDIO - RODE's factory pad sounds are theirs, and the unit restores its own
    on a factory reset - so a test that needs a file makes one. A generated tone is the better
    fixture anyway: it is tiny, its contents are known, and it cannot be confused with a real pad
    sound if it is ever left on a desk.

    Written once into the temp dir and reused. Mono 22.05 kHz 16-bit, which is well inside what the
    pad player accepts (see soundpad.action_add_sound: every rate and depth tried plays natively).
    """
    import math
    import struct
    import tempfile
    import wave

    path = os.path.join(tempfile.gettempdir(), name)
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    rate, freq = 22050, 440.0
    frames = bytearray()
    for n in range(int(rate * seconds)):
        frames += struct.pack("<h", int(0.3 * 32767 * math.sin(2 * math.pi * freq * n / rate)))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path
