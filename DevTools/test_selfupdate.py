"""Offline tests of AccessCaster's own update: the rules, the signed check, the download, the
wording, and the job the window follows. NO NETWORK, nothing installed, no desk.

Nearly every check here pins a failure that is SILENT in the field. An off-by-one in the version
comparison shows up as a user never being offered anything, which looks exactly like no release
having happened; a sentence that merges two outcomes hides the one the signing scheme exists to
catch. The scheme's shipped private key is offline by design, so the signed path is exercised with
a key pair generated here.

Run: python DevTools/test_selfupdate.py
"""
from __future__ import annotations

import datetime
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import _bootstrap  # noqa: F401  (puts Project/ on sys.path; import FIRST)
import _testkit
import app_updates
import settings_file
from _testkit import check, check_true, patched
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from selfupdate import check as checks
from selfupdate import checker, downloader, http, indexfile, interval, phrases, restart, signing
from selfupdate.versions import UpdateVersion
from version import HISTORY, __version__

REPO = Path(__file__).resolve().parent.parent
V = UpdateVersion.parse


def index_json(version="2.64.0", url="https://updates.example.com/AccessCasterSetup.exe",
               sha256="a" * 64, notes="Fixed a thing.", released="2026-10-24", **extra):
    """An index as the publishing tool writes it. A field given as None is left out."""
    document = {"version": version, "url": url, "sha256": sha256, "notes": notes,
                "released": released, **extra}
    document = {k: v for k, v in document.items() if v is not None}
    return json.dumps(document, indent=2).encode() + b"\n"


print("THE RUNNING VERSION CAN BE READ")
# A build whose own version the scheme cannot read never checks at all, silently.
check_true(f"version.py's {__version__!r} is a version the scheme reads", V(__version__))
check("HISTORY's newest entry is the running version", HISTORY[0][0], __version__)
check("a two-part version is refused, as the publishing tool refuses it", V("2.62"), None)

print("\nVERSION ORDERING")
check_true("numbers compare as numbers", V("2.10.0") > V("2.9.0"))
check_true("a numeric pre-release compares as a number", V("2.64.0-beta.10") > V("2.64.0-beta.9"))
check_true("a release beats its own pre-releases", V("2.64.0") > V("2.64.0-beta.24"))
check("build metadata is ignored", V("2.64.0+abc"), V("2.64.0"))
check("a pre-release is on the beta track", V("2.64.0-beta.1").track, "beta")
check("a release is on the stable track", V("2.64.0").track, "stable")

print("\nTHE INDEX DOCUMENT")
good = indexfile.parse(index_json())
check_true("a good index is read", good is not None)
check("the hash is lower-cased", indexfile.parse(index_json(sha256="A" * 64)).sha256, "a" * 64)
check("a plain http download is refused", indexfile.parse(index_json(url="http://x/y.exe")), None)
check("a short hash is refused", indexfile.parse(index_json(sha256="a" * 63)), None)
check("an index with no notes is refused", indexfile.parse(index_json(notes="  ")), None)
check("a compact date is refused", indexfile.parse(index_json(released="20261024")), None)
check("a two-part version is refused", indexfile.parse(index_json(version="2.64")), None)
check_true("a field from a later format is ignored",
           indexfile.parse(index_json(channel="x")) is not None)
check("rubbish is refused without raising", indexfile.parse(b"\xff\x00{"), None)

print("\nTHE VERDICT")
running = V("2.63.0")
check("newer is offered", checks.decide(running, good, "").verdict, checks.AVAILABLE)
check("the same version is up to date",
      checks.decide(V("2.64.0"), good, "").verdict, checks.UP_TO_DATE)
check("an older index is not a downgrade",
      checks.decide(V("2.65.0"), good, "").verdict, checks.UP_TO_DATE)
check("the skipped version is silent", checks.decide(running, good, "2.64.0").verdict,
      checks.SKIPPED)
check("the next version after a skip is offered",
      checks.decide(running, good, "2.63.5").verdict, checks.AVAILABLE)
check("an unreadable running version offers nothing",
      checks.decide(None, good, "").verdict, checks.COULD_NOT_CHECK)
check("the stable index lives in AccessCaster's folder", checks.index_url(running),
      "https://updates.slightperception.com/accesscaster/stable/index.json")
check("a beta build reads the beta folder", checks.signature_url(V("2.64.0-beta.1")),
      "https://updates.slightperception.com/accesscaster/beta/index.json.sig")

print("\nHOW OFTEN")
check("never is never due", interval.is_due(interval.NEVER, 0, 1e9), False)
check("a first run is due", interval.is_due(1, None, 1e9), True)
check("a day is respected", interval.is_due(1, 1e9 - 3600, 1e9), False)
check("a last check in the future is due rather than stuck", interval.is_due(1, 2e9, 1e9), True)
check("an unknown stored period is the default", interval.normalise(3), interval.DEFAULT)

print("\nTHE SIGNED CHECK")
private = Ed25519PrivateKey.generate()
key_hex = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
document = index_json()
files = {}


def serve(doc, sig):
    files.clear()
    files[checks.index_url(running)] = doc
    files[checks.signature_url(running)] = sig


def get(url):
    return files.get(url)


serve(document, private.sign(document))
check("a signed newer index is offered",
      checker.run(running, "", get=get, public_key_hex=key_hex).verdict, checks.AVAILABLE)
serve(document + b" ", private.sign(document))
check("one changed byte fails it",
      checker.run(running, "", get=get, public_key_hex=key_hex).verdict, checks.COULD_NOT_CHECK)
serve(document, Ed25519PrivateKey.generate().sign(document))
check("another key's signature is refused",
      checker.run(running, "", get=get, public_key_hex=key_hex).verdict, checks.COULD_NOT_CHECK)
serve(document, private.sign(document))
check("the skip holds over the whole round trip",
      checker.run(running, "2.64.0", get=get, public_key_hex=key_hex).verdict, checks.SKIPPED)
asked = []
checker.run(running, "", get=lambda u: asked.append(u), public_key_hex="")
check("no key means no request is even made", asked, [])
check_true("the shipped key is a real one", signing.is_configured())
check("a signature of the wrong length is refused",
      signing.verify(document, b"x" * 63, key_hex), False)
check_true("the request names AccessCaster, not Python, which Cloudflare refuses",
           http.USER_AGENT.startswith("AccessCaster/") and "urllib" not in http.USER_AGENT)

print("\nTHE DOWNLOAD")
payload = b"MZ" + os.urandom(4096)
digest = hashlib.sha256(payload).hexdigest()


class _Response(io.BytesIO):
    def __init__(self, data, die_after=None):
        super().__init__(data)
        self._die_after = die_after

    def read(self, n=-1):
        if self._die_after is not None and self.tell() >= self._die_after:
            raise ConnectionResetError("dropped")
        return super().read(min(n, 1024) if n and n > 0 else n)

    def __exit__(self, *exc):
        return False


def offer_of(sha=digest, version="2.64.0"):
    return indexfile.parse(index_json(version=version, sha256=sha))


with tempfile.TemporaryDirectory() as folder:
    outcome, path = downloader.download(offer_of(), folder, open_url=lambda u: _Response(payload))
    check("a good download is ready", outcome, downloader.READY)
    check("and named after its version, as the build names it",
          os.path.basename(path or ""), "AccessCasterSetup-v2.64.0.exe")
    fetched = []
    outcome, _ = downloader.download(offer_of(), folder,
                                     open_url=lambda u: fetched.append(u) or _Response(payload))
    check("a file already there and right is not fetched again", (outcome, fetched),
          (downloader.READY, []))
with tempfile.TemporaryDirectory() as folder:
    outcome, path = downloader.download(offer_of(sha="b" * 64), folder,
                                        open_url=lambda u: _Response(payload))
    check("a file that hashes wrong is the checksum outcome, not a failed download",
          outcome, downloader.WRONG_CHECKSUM)
    check("and nothing is left on disk", os.listdir(folder), [])
    outcome, _ = downloader.download(offer_of(), folder,
                                     open_url=lambda u: _Response(payload, die_after=2048))
    check("a connection that dies is an ordinary failure", outcome, downloader.COULD_NOT_DOWNLOAD)
    check("and no part file survives it", os.listdir(folder), [])

    class _Cancel(downloader.Watcher):
        def cancelled(self):
            return True
    outcome, _ = downloader.download(offer_of(), folder, open_url=lambda u: _Response(payload),
                                     watcher=_Cancel())
    check("a cancel stops it and leaves nothing behind", (outcome, os.listdir(folder)),
          (downloader.CANCELLED, []))

    class _Stages(downloader.Watcher):
        def __init__(self):
            self.seen = []

        def downloading(self):
            self.seen.append("downloading")

        def checking(self):
            self.seen.append("checking")
    Path(folder, downloader.file_name_for(V("2.64.0"))).write_bytes(b"damaged")
    stages = _Stages()
    outcome, _ = downloader.download(offer_of(), folder, open_url=lambda u: _Response(payload),
                                     watcher=stages)
    check("a damaged installer on disk is fetched again", outcome, downloader.READY)
    check("and the stages say so in order", stages.seen, ["checking", "downloading", "checking"])
    stages = _Stages()
    downloader.download(offer_of(), folder, open_url=lambda u: _Response(payload), watcher=stages)
    check("a good one on disk is only checked", stages.seen, ["checking"])

print("\nTHE WORDING")
sentences = [phrases.available("2.64.0"), phrases.confirm_title("2.64.0"),
             phrases.CONFIRM_AFTER, phrases.CHECKING, phrases.DOWNLOADING,
             phrases.CHECKING_STAGE, phrases.READY_STAGE, phrases.CANCELLED_DOWNLOAD,
             phrases.COULD_NOT_DOWNLOAD, phrases.WRONG_CHECKSUM, phrases.COULD_NOT_CHECK,
             phrases.CANNOT_CHECK, phrases.ALREADY_CHECKING, phrases.OFFER_CHANGED,
             phrases.not_back("2.64.0", 3), phrases.up_to_date("2.63.0"),
             phrases.skipped("2.64.0"), phrases.just_updated("2.64.0"),
             phrases.update_did_not_take("2.63.0", r"C:\x\AccessCasterSetup-v2.64.0.exe"),
             phrases.settings_status("once a day", None, "2.64.0", datetime.date(2026, 11, 1)),
             phrases.tray_install("2.64.0"), phrases.TRAY_CHECK]
check("the not-back sentence says the app is still running, in words",
      phrases.not_back("2.64.0", 3).startswith("AccessCaster is still running three minutes"), True)
check("and does not send the user to start an app that is running",
      "Start menu" in phrases.not_back("2.64.0", 3), False)
check("the dialog's wait is whole minutes, as the sentence says",
      app_updates.NOT_BACK_WITHIN_S % 60, 0)
check("no sentence uses a dash a screen reader skips",
      [s for s in sentences if "\u2013" in s or "\u2014" in s], [])
# A file name's "Setup-v2.64.0" is a name, not a version in prose.
check("no version is written as v-number, read as volt",
      [s for s in sentences if re.search(r"(?<![\w-])v\d", s)], [])
check_true("the notice names the key that acts on it", phrases.KEY in phrases.available("2.64.0"))
check_true("a wrong checksum does not read as a failed download",
           phrases.WRONG_CHECKSUM != phrases.COULD_NOT_DOWNLOAD
           and "match" in phrases.WRONG_CHECKSUM)
check_true("the notification fits the shell's 255 characters",
           len(phrases.available("12.345.678-beta.910")) <= 255)
check("a release three weeks old reads as weeks",
      phrases.age(datetime.date(2026, 10, 11), datetime.date(2026, 11, 1)), "three weeks ago")

print("\nTHE JOB THE PAGE FOLLOWS")


def reset():
    app_updates._offer = app_updates._job = app_updates._report = None
    app_updates._busy = app_updates._open_request = False
    settings_file.update(**{app_updates._LAST_CHECKED_KEY: "", app_updates._SKIPPED_KEY: "",
                            app_updates._INSTALLING_KEY: ""})


def wait_for(stage, within=3.0):
    end = time.monotonic() + within
    while time.monotonic() < end:
        job = app_updates.job()["job"]
        if job and job["stage"] == stage:
            return job
        time.sleep(0.02)
    return app_updates.job()["job"]


offered = offer_of()
found = checks.CheckResult(checks.AVAILABLE, offered)
launched = []
registered = []


def fake_download(offer, folder, open_url=None, watcher=None):
    watcher.checking()
    return downloader.READY, os.path.join(folder, downloader.file_name_for(offer.version))


with patched((checker, {"run": lambda running, skipped: found}),
             (signing, {"is_configured": lambda key=None: True}),
             (downloader, {"download": fake_download}),
             (app_updates.subprocess, {"Popen": lambda *a, **k: launched.append((a, k))}),
             (restart, {"register": lambda: registered.append(True) or True})):
    reset()
    reply = app_updates.check_now()
    offer = reply.get("offer") or {}
    check("a check that finds a version returns the decision", offer.get("version"), "2.64.0")
    check_true("with its notes and the first progress line",
               offer.get("notes") == "Fixed a thing." and offer.get("downloading"))
    check("installing a version other than the one offered is refused",
          app_updates.install("2.65.0")["ok"], False)
    check("the right version starts", app_updates.install("2.64.0")["ok"], True)
    check("the job reaches ready", (wait_for("ready") or {}).get("stage"), "ready")
    check("nothing is launched until the window says proceed", launched, [])
    check("proceed starts the installer", app_updates.proceed()["ok"], True)
    args = launched[0][0][0] if launched else []
    check("silently, restarting the app, never rebooting", args[1:],
          ["/VERYSILENT", "/RESTARTAPPLICATIONS", "/NORESTART"])
    check_true("the restart is registered before the hand-off", registered)
    check_true("and the next launch is told what to look for",
               settings_file.load().get(app_updates._INSTALLING_KEY, "").startswith("2.64.0|"))

    reset()
    launched.clear()
    app_updates.check_now()
    app_updates.install("2.64.0")
    wait_for("ready")
    check("Cancel at the ready point cancels", app_updates.cancel()["ok"], True)
    check("and proceed then installs nothing", (app_updates.proceed()["ok"], launched),
          (False, []))

with patched((checker, {"run": lambda running, skipped: found}),
             (signing, {"is_configured": lambda key=None: True}),
             (downloader, {"download": lambda o, f, open_url=None, watcher=None:
                           (downloader.WRONG_CHECKSUM, None)})):
    reset()
    app_updates.check_now()
    settings_file.update(**{app_updates._LAST_CHECKED_KEY: 1.0})
    app_updates.install("2.64.0")
    job = wait_for("failed")
    check("a wrong checksum fails with its own sentence", (job or {}).get("message"),
          phrases.WRONG_CHECKSUM)
    check("the held offer is thrown away", app_updates.held_version(), None)
    check("and so is the last-checked stamp, so the next check is a fresh read",
          settings_file.load().get(app_updates._LAST_CHECKED_KEY), "")

with patched((signing, {"is_configured": lambda key=None: False})):
    check("a build with no key says so", app_updates.check_now().get("message"),
          phrases.CANNOT_CHECK)

print("\nTHE REPORT FROM THE FAR SIDE OF THE RESTART")
reset()
with patched((app_updates, {"_later": lambda *a: None})):
    settings_file.update(**{app_updates._INSTALLING_KEY: "99.0.0|C:/x/AccessCasterSetup-v99.0.0.exe"})
    app_updates._read_outcome()
    report = app_updates.take_report() or {}
    check("still on the old version is a failure", report.get("kind"), "failed")
    check_true("which names the installer to run by hand", "AccessCasterSetup-v99.0.0" in
               report.get("text", ""))
    check("the record is cleared as it is read", settings_file.load().get(app_updates._INSTALLING_KEY),
          "")
    settings_file.update(**{app_updates._INSTALLING_KEY: f"{__version__}|x",
                            app_updates._SKIPPED_KEY: "1.0.0"})
    app_updates._read_outcome()
    check("on the version it wanted, it updated", (app_updates.take_report() or {}).get("kind"),
          "updated")
    check("a stored skip is forgotten once an update lands",
          settings_file.load().get(app_updates._SKIPPED_KEY), "")
    check("the report is taken once", app_updates.take_report(), None)

reset()
app_updates.request_decision()
check("the tray's request is taken once", (app_updates.take_request(), app_updates.take_request()),
      (True, False))

print("\nTHE RESTART AND THE INSTALLER")
check("a source run never registers a restart", restart.register(), False)
iss = (REPO / "ProjectBuilds" / "installer.iss").read_text(encoding="utf-8")
check_true("a silent update never touches the firewall rule, which would raise UAC",
           "not WizardSilent and FirewallRuleAdded" in iss)
check_true("and no install adds one", "add rule" not in iss)

print("\nTHE NOTES THE PUBLISHER READS")
sys.path.insert(0, str(REPO / "ProjectBuilds"))
import make_release_notes  # noqa: E402

text = make_release_notes.text_notes()
check("the first line names the version, which the publisher checks",
      text.splitlines()[0], f"What is new in AccessCaster {__version__}")
check("no line starts with a bullet a screen reader would read out",
      [line for line in text.splitlines() if line.lstrip().startswith(("-", "*"))], [])

raise SystemExit(_testkit.finish())
