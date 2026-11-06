"""Run every offline suite, each in its own process, and report one tally.

    python DevTools/run_tests.py                  every offline suite
    python DevTools/run_tests.py announcer pads   only suites whose name contains a word given
    python DevTools/run_tests.py -v               print every suite's output, not only a failure's

An offline suite is a `test_*.py` whose name does not end `_live.py`. A `_live` suite drives the
attached desk and is never run from here, whatever is asked for. A suite that does not import
`_bootstrap` is reported as a failure and not run, because `_bootstrap` is what refuses device
writes inside a test.

Each suite gets a process of its own because `_bootstrap` acts on the script's own name, when
it starts: the throwaway data dir, silence and the write refusal. Suites also pin the desk model
and other module state for their whole run, so sharing a process would let one suite's state
decide another's result.

Exit status 1 when any suite fails, times out or is refused.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

DEVTOOLS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(DEVTOOLS)

TIMEOUT_S = 600
FAILURE_TAIL = 40


def offline_suites(words: list[str] | None = None) -> list[str]:
    """The offline suites' file names, sorted, narrowed to those containing any of `words`."""
    names = []
    for name in sorted(os.listdir(DEVTOOLS)):
        if not (name.startswith("test_") and name.endswith(".py")) or name.endswith("_live.py"):
            continue
        if words and not any(word in name for word in words):
            continue
        names.append(name)
    return names


def imports_bootstrap(name: str) -> bool:
    with open(os.path.join(DEVTOOLS, name), encoding="utf-8") as f:
        return "import _bootstrap" in f.read()


def run_suite(name: str) -> tuple[str, float, str]:
    """Run one suite. Returns its outcome (PASS, FAIL, TIMEOUT, REFUSED), seconds and output."""
    if not imports_bootstrap(name):
        return "REFUSED", 0.0, "does not import _bootstrap, so nothing refuses its device writes"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    started = time.monotonic()
    try:
        done = subprocess.run(
            [sys.executable, os.path.join(DEVTOOLS, name)], cwd=REPO, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=TIMEOUT_S, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        return "TIMEOUT", time.monotonic() - started, output
    elapsed = time.monotonic() - started
    return ("PASS" if done.returncode == 0 else "FAIL"), elapsed, done.stdout + done.stderr


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("words", nargs="*", help="run only suites whose name contains one of these")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every suite's output")
    args = parser.parse_args()

    names = offline_suites(args.words)
    if not names:
        print("No offline suite matches.")
        return 1

    failed = []
    started = time.monotonic()
    for name in names:
        outcome, elapsed, output = run_suite(name)
        print(f"{outcome:8} {name[:-3]}  {elapsed:.1f} s", flush=True)
        if outcome != "PASS":
            failed.append(name[:-3])
        if args.verbose or outcome != "PASS":
            lines = output.rstrip().splitlines()
            shown = lines if args.verbose else lines[-FAILURE_TAIL:]
            for line in shown:
                print("    " + line)

    total = time.monotonic() - started
    print(f"\n{len(names) - len(failed)} of {len(names)} suites passed in {total:.0f} s.")
    if failed:
        print("Failed: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
