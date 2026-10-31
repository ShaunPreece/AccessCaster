"""Drive the real app page in a headless browser, WITHOUT touching the running app.

The checks pin what a screen reader depends on and the eye cannot see: the ARIA roles and states,
one tab stop for a strip, arrow/Home/End travel, only the selected pane exposed, and WHICH device
reads a tab fires when you open it.

The obvious way (point a headless browser at http://127.0.0.1:8770) is a trap, twice over:
  1. The page fires sendBeacon("/api/quit") on pagehide, so the render finishing STOPS the running
     server (docs/PROJECT_GUIDE.md, Tooling).
  2. Anything the checks click could write to the real desk.

So this serves the app's own static files on ANOTHER port and sits in the middle:
  - GET  /api/*  is PROXIED to the running server, so the page is filled with the real desk's real
    values. Reads only, which is what a page load does anyway.
  - POST /api/*  is NEVER forwarded. It is recorded and answered with a stub, so no check can write
    to the desk, and /api/quit cannot reach the server that owns the device.
  - every request is recorded, and the checks can read the log back from /__probe_log, which is how
    "opening the Levels tab reads the levels and the monitor mix, and nothing else does" is asserted
    rather than eyeballed.
  - the served index.html gets ONE extra script tag (the checks file). The repo's files are not
    modified.

Usage:
    python DevTools/ui_probe.py --checks DevTools/ui_checks_routing.js [--route routing]
Exit code 0 = every check passed, 1 = a check failed or the browser never reported.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import win32job
import win32process

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: E402,F401  (puts Project/ on sys.path)
import page        # noqa: E402  - the same index.html assembly the real server uses

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATIC = os.path.join(ROOT, "Project", "static")

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}

# The harness the checks file runs inside. `check(name, cond, detail)` records a result, `probeLog()`
# fetches the recorded request log, and `done()` posts everything back for this script to print.
HARNESS = r"""
(function () {
  const results = [];
  window.check = function (name, cond, detail) {
    results.push({ name: name, ok: !!cond, detail: detail === undefined ? "" : String(detail) });
  };
  window.probeLog = async function () {
    const r = await fetch("/__probe_log");
    return await r.json();
  };
  window.probeClear = async function () {
    await fetch("/__probe_clear", { method: "POST" });
  };
  window.sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  window.probeDone = async function () {
    const errs = window.__probeErrors || [];
    try {
      await fetch("/__probe_results", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ results: results, errors: errs }),
      });
    } catch (e) { /* nothing we can do from here */ }
  };
  window.__probeErrors = [];
  window.addEventListener("error", (e) => {
    window.__probeErrors.push(String((e && e.message) || e));
  });
  window.addEventListener("unhandledrejection", (e) => {
    window.__probeErrors.push("unhandled rejection: " + String((e && e.reason) || e));
  });
})();
"""


class Probe:
    """Shared state between the HTTP handler and the main thread."""

    def __init__(self, upstream_port: int, checks_js: str, stubs: dict | None = None):
        self.upstream_port = upstream_port
        self.checks_js = checks_js
        # GET paths answered LOCALLY instead of being proxied (--stub). This is how a page feature
        # gets tested BEFORE the app is rebuilt: the running server is the installed build, so a
        # newly added endpoint 404s upstream and the page under test sees only the failure path.
        # Stubs are read-only fixtures; nothing here can reach the desk.
        self.stubs = stubs or {}
        self.log: list[dict] = []
        self.lock = threading.Lock()
        self.payload = None
        self.finished = threading.Event()

    def record(self, method: str, path: str, body: bytes = b""):
        """Log one request. The BODY is kept for POSTs so a check can ask WHAT the page tried to
        write, not merely that it tried: "the wireless mute posts to its own endpoint" passes just as
        happily when the page sends the wrong channel name. Decoded as JSON where it is JSON, and
        left as the raw string otherwise, so a check never has to parse it twice."""
        entry = {"method": method, "path": path, "t": round(time.time(), 3)}
        if body:
            text = body.decode("utf-8", "replace")
            try:
                entry["body"] = json.loads(text)
            except ValueError:
                entry["body"] = text
        with self.lock:
            self.log.append(entry)

    def snapshot(self):
        with self.lock:
            return list(self.log)

    def clear(self):
        with self.lock:
            self.log = []


def make_handler(probe: Probe):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):    # keep the console readable
            pass

        # -- helpers ------------------------------------------------------
        def _send(self, code, body: bytes, ctype="text/plain; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _proxy_get(self, path):
            try:
                conn = http.client.HTTPConnection("127.0.0.1", probe.upstream_port, timeout=30)
                conn.request("GET", path)
                resp = conn.getresponse()
                body = resp.read()
                ctype = resp.getheader("Content-Type") or "application/json"
                self._send(resp.status, body, ctype)
                conn.close()
            except Exception as exc:
                self._json({"ok": False, "message": "probe proxy failed: %s" % exc}, 502)

        # -- routes -------------------------------------------------------
        def do_GET(self):
            path = self.path
            probe.record("GET", path)
            if path in probe.stubs:
                self._json(probe.stubs[path])
                return
            if path.startswith("/api/"):
                self._proxy_get(path)
                return
            if path == "/__probe_log":
                self._json(probe.snapshot())
                return
            if path == "/__probe.js":
                self._send(200, (HARNESS + "\n" + probe.checks_js).encode("utf-8"),
                           MIME[".js"])
                return
            rel = path.split("?")[0].lstrip("/") or "index.html"
            # The app's own markup asks for /static/app.js (that is where the real server mounts
            # them), while this probe IS the static root.
            if rel.startswith("static/"):
                rel = rel[len("static/"):]
            if rel in ("", "index.html"):
                # ASSEMBLED THE SAME WAY THE REAL SERVER ASSEMBLES IT (page.render_index splices
                # static/help.html in), so the checks see the Help & About page. Reading the file
                # straight off disk here would hand every probe run a page with that section
                # missing, and the checks would report its absence as a UI fault.
                html = page.render_index().decode("utf-8")
                # ONE injected tag; the repo file is untouched.
                html = html.replace("</body>", '<script src="/__probe.js"></script>\n</body>')
                self._send(200, html.encode("utf-8"), MIME[".html"])
                return
            full = os.path.join(STATIC, rel.replace("/", os.sep))
            if not os.path.abspath(full).startswith(os.path.abspath(STATIC)) \
                    or not os.path.isfile(full):
                self._send(404, b"not found")
                return
            ext = os.path.splitext(full)[1].lower()
            with open(full, "rb") as fh:
                self._send(200, fh.read(), MIME.get(ext, "application/octet-stream"))

        def do_POST(self):
            path = self.path
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            probe.record("POST", path, body)
            if path == "/__probe_results":
                try:
                    probe.payload = json.loads(body.decode("utf-8") or "{}")
                except Exception as exc:
                    probe.payload = {"results": [], "errors": ["bad payload: %s" % exc]}
                self._json({"ok": True})
                probe.finished.set()
                return
            if path == "/__probe_clear":
                probe.clear()
                self._json({"ok": True})
                return
            # Every /api POST is stubbed. NOTHING is forwarded: no write can reach the desk, and a
            # /api/quit beacon cannot reach the server that owns it.
            self._json({"ok": True, "message": "Stubbed by ui_probe (nothing was written)."})

    return Handler


class ProbeServer(ThreadingHTTPServer):
    """Like ThreadingHTTPServer, but it REFUSES to share a port.

    ThreadingHTTPServer sets allow_reuse_address = 1, and on Windows SO_REUSEADDR lets a SECOND
    process bind a port that is already bound, with no error at all. Requests then land on whichever socket Windows picks, so two probe suites
    sharing a fixed port steal each other's traffic: a browser's results POST arrives at the other
    run's server, and the run it belonged to reports "the page never reported back", which reads
    exactly like a real page fault. Each suite takes a free port of its own (see pick_port), and
    refusing to share makes a genuine clash loud instead of silent.
    """

    allow_reuse_address = False

    def handle_error(self, request, client_address):
        """Swallow the browser hanging up; report anything else.

        A headless browser being terminated drops its keep-alive sockets, and socketserver prints a
        full ConnectionReset traceback per socket. On a five-suite run that is dozens of tracebacks
        interleaved across the suites, which reads exactly like a crashing test and buries the
        actual results.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def pick_port(preferred: int, probe: Probe):
    """Bind the first free port from `preferred` upwards, so concurrent suites cannot collide.

    preferred = 0 means "let the OS choose", which is the default and the safest answer.
    """
    if preferred == 0:
        server = ProbeServer(("127.0.0.1", 0), make_handler(probe))
        return server, server.server_address[1]
    last = None
    for port in range(preferred, preferred + 20):
        try:
            return ProbeServer(("127.0.0.1", port), make_handler(probe)), port
        except OSError as exc:
            last = exc
    raise SystemExit("no free port from %d upwards: %s" % (preferred, last))


def find_browser():
    for path in BROWSERS:
        if os.path.isfile(path):
            return path
    return None


PROFILE_PREFIX = "ui_probe_"
PROFILE_MAX_AGE_S = 24 * 3600   # far longer than any run, so a concurrent suite's profile is safe


def sweep_old_profiles():
    """Delete profiles left by runs that were killed before their own cleanup ran."""
    root = tempfile.gettempdir()
    cutoff = time.time() - PROFILE_MAX_AGE_S
    for name in os.listdir(root):
        path = os.path.join(root, name)
        try:
            if (name.startswith(PROFILE_PREFIX) and os.path.isdir(path)
                    and os.path.getmtime(path) < cutoff):
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


class BrowserJob:
    """The headless browser and every process it spawns, held in one Windows job object.

    Chromium is a tree of processes, so terminating the one this script starts leaves the rest
    running. A survivor runs until shutdown, and Windows' "restart apps after sign-in" then
    relaunches it at the next sign-in. The job is created with KILL_ON_JOB_CLOSE, so Windows ends
    the whole tree when the job's last handle closes, which happens when this script exits however
    it exits, including being killed by its caller's timeout before any cleanup code runs.

    The browser starts suspended and is resumed only once it is in the job, so no child process
    can be spawned outside it.
    """

    def __init__(self, cmd: list[str]):
        self.job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(
            self.job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(
            self.job, win32job.JobObjectExtendedLimitInformation, info)
        process, thread, _pid, _tid = win32process.CreateProcess(
            None, subprocess.list2cmdline(cmd), None, None, False,
            win32process.CREATE_SUSPENDED, None, None, win32process.STARTUPINFO())
        try:
            win32job.AssignProcessToJobObject(self.job, process)
        except Exception:
            win32process.TerminateProcess(process, 1)
            raise
        win32process.ResumeThread(thread)
        thread.Close()
        process.Close()

    def close(self, wait_s: float = 10.0):
        """End every process in the job and wait for them to go, so the profile can be deleted."""
        win32job.TerminateJobObject(self.job, 0)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            accounting = win32job.QueryInformationJobObject(
                self.job, win32job.JobObjectBasicAccountingInformation)
            if not accounting["ActiveProcesses"]:
                break
            time.sleep(0.1)
        self.job.Close()


# The default answer given for /api/baseline/status: "already asked, nothing to offer". Same shape
# as the real endpoint, so the Profiles page's as-found panel still renders rather than erroring.
BASELINE_QUIET = {"ok": True, "offer": False, "connected": True, "baseline": None,
                  "suggested_name": "", "device_name": "RODECaster Pro 2", "seconds_per_pad": 15}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--checks", required=True, help="JS file of checks to run in the page")
    ap.add_argument("--route", default="", help="hash route to open (e.g. routing)")
    ap.add_argument("--port", type=int, default=0,
                    help="port to serve the page on. 0 (the default) picks a free one, which is "
                         "what lets two suites run at once; a number is tried first and then the "
                         "next 19 above it.")
    ap.add_argument("--upstream", type=int, default=8770)
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--stub", default="", help="JSON file of {api path: response} answered locally "
                                               "instead of proxied, for endpoints the installed "
                                               "build does not have yet")
    ap.add_argument("--offer-baseline", action="store_true",
                    help="let /api/baseline/status through to the real server. Only the baseline "
                         "suite wants this; see the note where it is applied below.")
    args = ap.parse_args()

    checks_path = args.checks if os.path.isabs(args.checks) else os.path.join(ROOT, args.checks)
    with open(checks_path, encoding="utf-8") as fh:
        checks_js = fh.read()

    stubs = {}
    # THE FIRST-RUN BASELINE DIALOG IS OFF BY DEFAULT, and this is not a convenience.
    # It is a MODAL alertdialog that opens on page entry and takes focus, exactly as designed. Every
    # page suite that asks "where did focus go?" then answers "baseline-save-btn", and the run reports
    # a wall of failures that have nothing to do with the page under test (6 on Channels, 9 on
    # Routing). A suite that does not test focus passes cleanly through the same dialog, so the trap
    # is silent unless that suite happens to check focus.
    # So: quieten it here, once, for everyone. --offer-baseline puts it back for the one suite whose
    # subject IS the dialog.
    if not args.offer_baseline:
        stubs["/api/baseline/status"] = BASELINE_QUIET
    if args.stub:
        stub_path = args.stub if os.path.isabs(args.stub) else os.path.join(ROOT, args.stub)
        with open(stub_path, encoding="utf-8") as fh:
            stubs.update(json.load(fh))    # an explicit --stub wins over the default above
    if stubs:
        print("stubbed GETs: %s" % ", ".join(sorted(stubs)))

    browser = find_browser()
    if not browser:
        print("No Edge or Chrome found; cannot run the UI probe.")
        return 1

    probe = Probe(args.upstream, checks_js, stubs)
    server, port = pick_port(args.port, probe)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % port + (("#/" + args.route) if args.route else "")
    print("probe serving on %s (api reads proxied to %d, writes stubbed)" % (url, args.upstream))

    sweep_old_profiles()
    profile = tempfile.mkdtemp(prefix=PROFILE_PREFIX)
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run",
           "--no-default-browser-check", "--user-data-dir=" + profile,
           "--window-size=1280,900", url]
    browser_job = BrowserJob(cmd)
    try:
        ok = probe.finished.wait(args.timeout)
    finally:
        browser_job.close()
        server.shutdown()
        shutil.rmtree(profile, ignore_errors=True)

    if not ok:
        print("FAIL: the page never reported back within %ds." % args.timeout)
        print("requests seen: %s" % json.dumps(probe.snapshot()[-20:], indent=1))
        return 1

    payload = probe.payload or {}
    results = payload.get("results") or []
    errors = payload.get("errors") or []
    failed = [r for r in results if not r.get("ok")]
    for r in results:
        print("%s %s%s" % ("PASS" if r.get("ok") else "FAIL", r.get("name"),
                           ("  [%s]" % r["detail"]) if r.get("detail") else ""))
    if errors:
        print("\nPAGE ERRORS (%d):" % len(errors))
        for e in errors:
            print("  " + e)
    print("\n%d checks, %d failed, %d page errors" % (len(results), len(failed), len(errors)))
    return 0 if (not failed and not errors) else 1


if __name__ == "__main__":
    sys.exit(main())
