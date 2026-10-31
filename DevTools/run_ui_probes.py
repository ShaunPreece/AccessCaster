"""Run every UI probe suite in turn and say which failed.

Needs the service on port 8770 with the watchdog off (DevTools/run_dev_server.py) and, for a run
that proves anything, a desk attached: the probe fills the page from the real desk's reads. A
suite that needs a stub file is skipped when the file is not there, and said to be.

Usage:
    python DevTools/run_ui_probes.py            # every suite
    python DevTools/run_ui_probes.py routing    # only the suites named
Each suite's output goes to %TEMP%\\ui_probe_<suite>.out.
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# (suite, route, timeout in seconds, stub file or None), from each checks file's "Run with" line.
SUITES = [
    ("about", "about", 90, None),
    ("appsettings", "profiles", 90, None),
    ("backups", "profiles", 90, "_probe_stub_backups.json"),
    ("baseline", "", 90, "_probe_stub_baseline.json"),
    ("boundary", "about", 90, None),
    ("callme", "callme", 240, None),
    ("callme_ready", "callme", 240, "_probe_stub_callme_unready.json"),
    ("chantabs", "channels", 90, None),
    ("device", "status", 600, None),
    ("levelcheck", "channels", 240, None),
    ("lifecycle", "device", 90, None),
    ("network", "status", 240, None),
    ("pads", "pads", 600, None),
    ("padsbus", "channels", 90, None),
    ("readonly", "routing", 90, "_probe_stub_readonly.json"),
    ("recording", "recording", 240, None),
    ("routing", "routing", 90, None),
    ("shell", "status", 90, None),
    ("solo", "channels", 240, None),
    ("staged", "routing", 90, None),
    ("stereopan", "channels", 240, None),
    ("storage", "recording", 240, None),
    ("wireless", "channels", 90, None),
]


def main(names):
    failed, skipped = [], []
    for suite, route, timeout, stub in SUITES:
        if names and suite not in names:
            continue
        cmd = [sys.executable, os.path.join(HERE, "ui_probe.py"),
               "--checks", os.path.join(HERE, f"ui_checks_{suite}.js"), "--timeout", str(timeout)]
        if route:
            cmd += ["--route", route]
        if suite == "baseline":
            cmd.append("--offer-baseline")   # every other suite has the dialog quietened
        if stub:
            path = os.path.join(HERE, stub)
            if not os.path.isfile(path):
                skipped.append(suite)
                print(f"  skip {suite}: no {stub}")
                continue
            cmd += ["--stub", path]
        out = os.path.join(tempfile.gettempdir(), f"ui_probe_{suite}.out")
        with open(out, "w", encoding="utf-8") as fh:
            try:
                code = subprocess.call(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                       timeout=timeout + 60)
            except subprocess.TimeoutExpired:
                code = 1
        print(f"  {'ok  ' if code == 0 else 'FAIL'} {suite}")
        if code:
            failed.append(suite)
    print(f"\n{len(failed)} failed, {len(skipped)} skipped"
          + (f": failed {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
