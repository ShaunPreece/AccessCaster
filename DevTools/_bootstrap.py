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
# profiles in the user's real profiles folder.
#
# A TEST NEVER MAKES A NOISE EITHER. A suite that does not stub `speech.say` leaves the real announcer
# in place, so anything it exercises SPEAKS OR CHIMES through SAPI, onto the desk the user monitors
# on, with nothing to say what is making the sound, possibly mid-recording. `test_shows_sim`
# completes dozens of background jobs and fires the done/failed cue for every one.
#
# AND A TEST NEVER WRITES TO THE DESK, which is the same rule a third time and the one that costs
# most when it is missed. An offline suite that reaches a real write fires a pad, engages a voice
# effect on a live mic or drops the audio on a desk somebody is working on - and it PASSES while it
# does it, because the assertion under test was about the decision, not the wire. The guard raises
# at the write (write_guard.refuse_in_tests) rather than swallowing it, so a suite
# that needs a device call is forced to stub it rather than quietly proving nothing.
#
# A `test_*_live` suite that genuinely means to drive hardware sets ACCESSCASTER_ALLOW_DEVICE_WRITE
# for itself, which is an opt-in the live suites can reasonably be asked to make: they exist to
# touch the desk and say so in their names.
#
# All three key off the script NAME rather than asking a test to opt in: an opt-in is what a new
# test forgets, and forgetting must not be able to cost anything.
#
# The probes and the capture tools are NOT tests and keep the real data dir, which is the point of
# running them. Set ACCESSCASTER_DATA_DIR yourself to override either way.
if os.path.basename(sys.argv[0] or "").startswith("test_"):
    os.environ.setdefault(
        "ACCESSCASTER_DATA_DIR",
        os.path.join(tempfile.gettempdir(), "AccessCaster-test-data"))
    os.environ.setdefault("ACCESSCASTER_SILENT", "1")
    os.environ.setdefault("ACCESSCASTER_NO_DEVICE_WRITE", "1")
    if os.path.basename(sys.argv[0] or "").endswith("_live.py"):
        os.environ.setdefault("ACCESSCASTER_ALLOW_DEVICE_WRITE", "1")

# A LIVE SUITE NEVER SHARES THE DESK WITH THE RUNNING APP: two processes writing to it interleave
# their transfers. The app's single-instance lock says which one has it, so a `*_live.py` run
# takes the lock or stops here. Scripts of other names that drive the desk take it through
# `_desk.service`.
if os.path.basename(sys.argv[0] or "").endswith("_live.py"):
    import instance
    if not instance.claim():
        raise SystemExit("AccessCaster is running and holds the desk. Quit it from the tray, "
                         "then run this again.")


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
    looks like. The coverage reports then write their output into the clone instead of failing
    on a folder that is not there.
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
