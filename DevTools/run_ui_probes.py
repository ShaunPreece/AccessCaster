"""Run every UI probe suite in turn and say which failed.

Needs the service on port 8770 with the watchdog off (DevTools/run_dev_server.py) and, for a run
that proves anything, a desk attached: the probe fills the page from the real desk's reads. A
suite that needs a stub file is skipped when the file is not there, and said to be.

Each suite says how it is run in its first line, which this reads, so there is no second copy of
it to drift:
    // probe: route=<hash route, or - for none> timeout=<seconds> [stub=<file>] [offer-baseline]

Usage:
    python DevTools/run_ui_probes.py            # every suite
    python DevTools/run_ui_probes.py routing    # only the suites named
Each suite's output goes to %TEMP%\\ui_probe_<suite>.out.
"""
import glob
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PREFIX = "ui_checks_"


def probe_settings(path: str) -> dict:
    """The suite's `// probe:` line as {route, timeout, stub, offer_baseline}."""
    with open(path, encoding="utf-8") as fh:
        first = fh.readline().strip()
    if not first.startswith("// probe:"):
        raise SystemExit(f"{os.path.basename(path)} has no '// probe:' first line")
    words = first[len("// probe:"):].split()
    fields = dict(w.split("=", 1) for w in words if "=" in w)
    return {"route": "" if fields.get("route", "-") == "-" else fields["route"],
            "timeout": int(fields.get("timeout", 90)),
            "stub": fields.get("stub"),
            "offer_baseline": "offer-baseline" in words}


def suites() -> list:
    paths = sorted(glob.glob(os.path.join(HERE, PREFIX + "*.js")))
    return [(os.path.basename(p)[len(PREFIX):-3], p) for p in paths]


def main(names):
    failed, skipped = [], []
    for suite, path in suites():
        if names and suite not in names:
            continue
        cfg = probe_settings(path)
        cmd = [sys.executable, os.path.join(HERE, "ui_probe.py"),
               "--checks", path, "--timeout", str(cfg["timeout"])]
        if cfg["route"]:
            cmd += ["--route", cfg["route"]]
        if cfg["offer_baseline"]:
            cmd.append("--offer-baseline")   # every other suite has the dialog quietened
        if cfg["stub"]:
            stub = os.path.join(HERE, cfg["stub"])
            if not os.path.isfile(stub):
                skipped.append(suite)
                print(f"  skip {suite}: no {cfg['stub']}")
                continue
            cmd += ["--stub", stub]
        out = os.path.join(tempfile.gettempdir(), f"ui_probe_{suite}.out")
        with open(out, "w", encoding="utf-8") as fh:
            try:
                code = subprocess.call(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                       timeout=cfg["timeout"] + 60)
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
