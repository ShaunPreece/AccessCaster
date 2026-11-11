"""AccessCaster - THE DIAGNOSTIC LOG: what the app did, in order, with timestamps.

WHY IT EXISTS. The shipped app is a WINDOWED exe, so every `print` in the codebase goes nowhere and
the only other way to say what the app is doing is to watch it from OUTSIDE - which measures the
watching as often as the app: a poll timing out against a reader legitimately blocked on the device
lock reads as a fault and is not. This writes the app's own account to a file instead, so a fault
is read rather than inferred.

WHAT IT RECORDS, and the list is shaped by the fault it is built for (a show import that changed
something takes the desk down about a minute AFTER the job has finished):

  app.*        the process coming up and going down, and why
  job.*        every background job: start, each step it reports, its outcome and how long it took
  send         every record written to the desk, DECODED to property = value, not hex
  hid.*        every device handle opened and closed, with what opened it
  transfer.*   entering and leaving Transfer Mode, and who is holding the session
  device.*     the desk going quiet and coming back, seen from the reader that notices first
  print/stderr everything the app writes to its console, which in the shipped build has none
  crash        an unhandled exception on any thread, and a native crash via faulthandler

THREE PROPERTIES IT HAS TO HAVE, and each one is a decision rather than a default:

  * EVERY LINE IS ON DISK WITHIN TWO SECONDS. A fault that ends with the power lead being pulled
    loses whatever is still in a buffer, which is the part worth reading. Each line is flushed as
    it is written, and a flusher thread closes a repeat group once it has gone quiet.
  * REPEATS COLLAPSE. The page polls, the recorder watches and the announcer reopens its handle,
    so an honest line-per-event log is thousands of identical lines with the signal buried in it.
    The FIRST of anything is always written the moment it happens; identical events after it
    become one "repeated x N" line. Nothing is ever hidden, only counted.
  * IT CANNOT BREAK THE APP. Every entry point swallows its own exceptions. A log that can take
    down the thing it is logging is worse than no log.

IT IS OFF UNTIL `start()` IS CALLED, which `service.start` does: the one place every
entry point funnels through, and only the holder of the single-instance lock gets there. So the
offline test suites import this module and write nothing, and only the process that owns the desk
writes to the file. That is also what keeps it a SINGLE-WRITER file.
"""

from __future__ import annotations

import contextlib
import datetime
import faulthandler
import os
import sys
import threading
import time

import desk_secrets
import model_tree  # to decode a sent record into property = value
from paths import data_path

LOG_NAME = "diagnostic.log"
PREVIOUS_NAME = "diagnostic-previous.log"

# One rollover, kept small enough to send in an email and big enough to hold a session of pad
# writes (a pad restore sends a few thousand records).
MAX_BYTES = 4 * 1024 * 1024

# How long identical events keep collapsing into one group before the group is closed and a fresh
# line is written. A poll that runs for an hour should not become a single line claiming to have
# happened at the start of it.
COALESCE_MAX_S = 30.0
_FLUSH_EVERY_S = 2.0

_lock = threading.RLock()
_fh = None
_size = 0
_active = False
_start_time = 0.0
_pending = None          # the open repeat group: {"key", "count", "first", "last"}
# Events from before `start`, written once it runs: the window opens before the service starts the
# log, and what happened to it is otherwise lost. None once written. Capped, for a process that
# never starts the log.
_early: list | None = []
_EARLY_MAX = 50


# ---- where it lives ------------------------------------------------------------------------

def folder() -> str:
    """The log directory, created on first use."""
    path = data_path("logs")
    os.makedirs(path, exist_ok=True)
    return path


def log_path() -> str:
    """The current diagnostic log file."""
    return os.path.join(folder(), LOG_NAME)


def active() -> bool:
    return _active


# ---- writing -------------------------------------------------------------------------------

def _stamp(now: float) -> str:
    t = datetime.datetime.fromtimestamp(now)
    return t.strftime("%Y-%m-%d %H:%M:%S.") + f"{t.microsecond // 1000:03d}"


def _value_text(value, kind: str) -> str:
    """One property value, short enough to read aloud. A pad's file path and an FX preset run to
    hundreds of characters and would bury the line they are on."""
    if kind == "float" and isinstance(value, float):
        return f"{value:.4g}"
    text = str(value)
    if len(text) > 60:
        text = text[:57] + "..."
    return text


def describe_record(rec: bytes) -> str:
    """One report-3 record as `<property> = <value> at <path>`, or a hex head if it will not parse.

    The record is `03 <payload length, u32 LE> <payload>`, and the payload is exactly the addressed
    record `model_tree.parse_record` decodes - the same shape as the desk's own pushes. Decoding it
    here rather than logging hex is the whole difference between a log that answers "what did we
    send it" and one that has to be decoded by hand afterwards.
    """
    try:
        length = int.from_bytes(rec[1:5], "little")
        payload = rec[5:5 + length]
        path, name, value, kind, _end = model_tree.parse_record(payload)
        where = "[" + ", ".join(f"0x{i:02x}" for i in path) + "]"
        shown = "(hidden)" if desk_secrets.is_secret(name) else _value_text(value, kind)
        return f"{name} = {shown} at {where}"
    except Exception:
        return "undecodable record " + bytes(rec[:16]).hex()


def _body(detail: str, fields: dict) -> str:
    parts = [detail] if detail else []
    parts += [f"{k}={v}" for k, v in fields.items() if v is not None and v != ""]
    return "  ".join(parts)


def _emit(now: float, kind: str, body: str, thread: str | None = None) -> None:
    """Write one line. Caller holds the lock."""
    global _fh, _size
    if _fh is None:
        return
    thread = thread or threading.current_thread().name
    line = (f"{_stamp(now)}  +{now - _start_time:.1f}s  {thread[:20]}  {kind}"
            + (f"  {body}" if body else "") + "\n")
    _fh.write(line)
    _fh.flush()
    _size += len(line)
    if _size >= MAX_BYTES:
        _roll()


def _close_group(now: float | None = None) -> None:
    """Close the open repeat group, writing its count. Caller holds the lock."""
    global _pending
    group, _pending = _pending, None
    if group and group["count"] > 1:
        span = group["last"] - group["first"]
        _emit(group["last"], "repeated",
              f"{group['key'][0]}{('  ' + group['key'][1]) if group['key'][1] else ''}"
              f"  x{group['count']} over {span:.1f}s")


def event(kind: str, detail: str = "", **fields) -> None:
    """Record one thing that happened. Never raises. Before `start()` it is kept in `_early`.

    The FIRST of any event is always written immediately; an identical one straight after it joins
    a repeat group instead of adding a line. `kind` is the dotted category, `detail` the sentence
    and `fields` the key=value tail.
    """
    global _pending
    if not _active:
        try:
            if _early is not None and len(_early) < _EARLY_MAX:
                _early.append((time.time(), threading.current_thread().name, kind,
                               _body(detail, fields)))
        except Exception:
            pass
        return
    try:
        body = _body(detail, fields)
        now = time.time()
        with _lock:
            key = (kind, body)
            if (_pending is not None and _pending["key"] == key
                    and now - _pending["first"] < COALESCE_MAX_S):
                _pending["count"] += 1
                _pending["last"] = now
                return
            _close_group(now)
            _pending = {"key": key, "count": 1, "first": now, "last": now}
            _emit(now, kind, body)
    except Exception:
        pass                    # a log that can break the app is worse than no log


def command(rec: bytes, why: str = "") -> None:
    """One record written to the desk, decoded. See describe_record."""
    if _active:
        event("send", describe_record(rec), by=why)


# A device call that normally takes milliseconds. Past this it is a finding rather than a step.
SLOW_S = 0.5


def slow(kind: str, took: float, detail: str = "", threshold: float = SLOW_S, **fields) -> None:
    """Log an operation ONLY when it took longer than it should have.

    WHY A THRESHOLD RATHER THAN A LINE EVERY TIME: the calls this guards run on every read, so
    logging all of them is thousands of lines saying "normal" and the log is useless. A fault that
    presents as the app freezing is exactly the one where the DURATION is the whole finding, so the
    rule is to say nothing when it is quick and say so loudly when it is not.

    Without it a blocking call leaves a HOLE rather than a line: `hid.enumerate` runs before any
    handle is opened, so a 30-second block there shows up only as the log stopping for half a
    minute.
    """
    if took >= threshold:
        event(kind, detail, took=f"{took:.2f}s", **fields)


class phase:
    """Log the start and the end of something, with how long it took.

        with diagnostics.phase("model.read", "full dump"):
            ...

    The END line is what a hang is read off: it says either how long the desk took or, by its
    absence, that it never answered.
    """

    def __init__(self, kind: str, detail: str = "", **fields):
        self.kind, self.detail, self.fields = kind, detail, fields
        self.at = 0.0
        self.result = ""

    def __enter__(self) -> "phase":
        self.at = time.monotonic()
        event(self.kind + ".start", self.detail, **self.fields)
        return self

    def note(self, **fields) -> None:
        """Add facts learned during the phase to its end line (bytes read, frames seen)."""
        self.fields.update(fields)

    def __exit__(self, exc_type, exc, tb) -> bool:
        took = f"{time.monotonic() - self.at:.2f}s"
        if exc is not None:
            event(self.kind + ".failed", self.detail, took=took, error=f"{exc}")
        else:
            event(self.kind + ".end", self.detail, took=took, **self.fields)
        return False


def flush(force: bool = False) -> None:
    """Close the open repeat group once it has gone quiet, so the file on disk is complete. Called
    by the flusher thread every `_FLUSH_EVERY_S`. A group still repeating is left open: closed on
    every tick, an event that recurs every two seconds or slower never folds at all. A power cut
    loses at most the count of a repeat still under way; its first line is already on disk.
    `force` closes it regardless, for shutdown."""
    if not _active:
        return
    try:
        with _lock:
            if force or (_pending is not None
                         and time.time() - _pending["last"] >= _FLUSH_EVERY_S + 1.0):
                _close_group()
    except Exception:
        pass


# ---- the file ------------------------------------------------------------------------------

def _roll() -> None:
    """Start a fresh log, keeping one previous. Caller holds the lock."""
    global _fh, _size
    try:
        _fh.close()
    except Exception:
        pass
    path = log_path()
    previous = os.path.join(os.path.dirname(path), PREVIOUS_NAME)
    try:
        os.replace(path, previous)
    except Exception:
        pass
    _fh = open(path, "a", encoding="utf-8", errors="replace")
    _size = 0
    _bind_faulthandler()


@contextlib.contextmanager
def handled_native_exceptions():
    """faulthandler off around a native call that raises and catches a Windows exception of its
    own. faulthandler hears every exception with the error bit set, handled or not, and logs each
    as a fatal one with every thread's stack."""
    was = faulthandler.is_enabled()
    if was:
        faulthandler.disable()
    try:
        yield
    finally:
        if was:
            _bind_faulthandler()


def _bind_faulthandler() -> None:
    """Point faulthandler at the current file. It holds the file DESCRIPTOR, not the object, so
    after a rollover closes the old file it must be bound again, or a native crash is written to a
    closed descriptor that may by then belong to some other file."""
    try:
        faulthandler.enable(file=_fh)
    except Exception:
        pass


# ---- the console, which the shipped build does not have ------------------------------------

class _Tee:
    """Everything written to stdout/stderr, into the log as well.

    THE POINT OF IT: the codebase already prints the things worth knowing - the shortcut count,
    the watchdog's stop reason, a startup read that failed - and in the frozen
    windowed build `sys.stdout` is None, so all of it is discarded. Teeing means every existing
    diagnostic starts being recorded without a single call site changing.
    """

    def __init__(self, under, kind: str):
        self._under = under
        self._kind = kind
        self._buf = ""

    def write(self, text) -> int:
        if self._under is not None:
            try:
                self._under.write(text)
            except Exception:
                pass
        try:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    event(self._kind, line.rstrip())
        except Exception:
            pass
        return len(text)

    def flush(self) -> None:
        if self._under is not None:
            try:
                self._under.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        try:
            return bool(self._under is not None and self._under.isatty())
        except Exception:
            return False


def _install_hooks() -> None:
    """Tee the console, and catch what kills a thread or the process.

    An unhandled exception on a worker thread is invisible in the shipped build: the thread dies,
    whatever it was doing stops, and nothing says so. `faulthandler` covers the other end - a
    native crash in hidapi (closing a handle while a read is in flight on another thread segfaults
    the interpreter) leaves no Python traceback at all.
    """
    sys.stdout = _Tee(sys.stdout, "print")
    sys.stderr = _Tee(sys.stderr, "stderr")

    previous_hook = sys.excepthook

    def _excepthook(kind, value, tb):
        event("crash", f"{kind.__name__}: {value}")
        try:
            previous_hook(kind, value, tb)
        except Exception:
            pass

    sys.excepthook = _excepthook

    previous_thread_hook = threading.excepthook

    def _thread_hook(args):
        event("crash", f"{args.exc_type.__name__}: {args.exc_value}",
              thread=getattr(args.thread, "name", "?"))
        try:
            previous_thread_hook(args)      # the traceback, on stderr, which the tee logs
        except Exception as exc:            # the crash event above is still the record
            event("crash.hook_failed", str(exc))

    threading.excepthook = _thread_hook
    _bind_faulthandler()


def _flusher() -> None:
    while _active:
        time.sleep(_FLUSH_EVERY_S)
        flush()


def start(context: str, **fields) -> str | None:
    """Begin logging. Returns the log path, or None if logging is off or could not be opened.

    Called once, from `service.start`: the single place every entry point goes through,
    and the only process that should be writing this file. Set ACCESSCASTER_NO_DIAG_LOG to refuse.
    """
    global _fh, _size, _active, _start_time, _early
    if _active:
        return log_path()
    if os.environ.get("ACCESSCASTER_NO_DIAG_LOG"):
        return None
    try:
        path = log_path()
        _size = os.path.getsize(path) if os.path.exists(path) else 0
        _fh = open(path, "a", encoding="utf-8", errors="replace")
        _start_time = time.time()
        _active = True
        banner = dict(pid=os.getpid(),
                      frozen=bool(getattr(sys, "frozen", False)),
                      python=sys.version.split()[0])
        banner.update(fields)
        with _lock:
            _emit(_start_time, "app.start", _body(context, banner))
            for at, thread, kind, body in _early or ():
                _emit(at, kind, body, thread)
            _early = None
        _install_hooks()
        threading.Thread(target=_flusher, name="diag-flush", daemon=True).start()
        return path
    except Exception:
        _active = False
        return None


def stop(reason: str = "") -> None:
    """Write the last line and close the file. The app ending cleanly is itself a fact worth
    having: the alternative reading of a log that simply stops is that the process was killed."""
    global _active
    if not _active:
        return
    try:
        with _lock:
            _close_group()
            _emit(time.time(), "app.stop", reason)
            _active = False
            try:
                faulthandler.disable()
                _fh.close()
            except Exception:
                pass
    except Exception:
        _active = False
