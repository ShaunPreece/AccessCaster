"""The shared harness for DevTools tests.

Each check prints one line and records a failure, and `finish` prints the tally and returns the
script's exit status. `as_device` and `patched` replace something for the length of a block and put
it back however the block ends. `decode_record` and `snapshot` read the desk's own record format.

    import _testkit
    from _testkit import check, check_true

    check("the label", got, want)
    ...
    sys.exit(_testkit.finish())
"""
from __future__ import annotations

import contextlib
from unittest import mock

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import devices
import model_tree

FAILURES: list[str] = []


def _record(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f": {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{label}: {detail}" if detail else label)
    return ok


def check(label: str, got, want) -> bool:
    """`got` equals `want`."""
    return _record(label, got == want, f"got {got!r}, want {want!r}")


def check_true(label: str, got, detail: str = "") -> bool:
    """`got` is truthy; `detail` is printed with a failure."""
    return _record(label, bool(got), detail)


def check_in(label: str, needle: str, haystack: str | None) -> bool:
    """`needle` is in `haystack`, ignoring case."""
    hay = haystack or ""
    # Cut to 160 characters: a haystack can be a whole source file, and printing all of it buries
    # the failure it reports.
    return _record(label, needle.lower() in hay.lower(), f"{needle!r} not in {hay[:160]!r}")


def check_not_in(label: str, needle: str, haystack: str | None) -> bool:
    """`needle` is not in `haystack`, ignoring case."""
    hay = haystack or ""
    return _record(label, needle.lower() not in hay.lower(), f"{needle!r} is in {hay[:160]!r}")


def failed() -> int:
    """How many checks have failed so far."""
    return len(FAILURES)


def finish() -> int:
    """Print the tally and return the exit status: 1 when any check failed."""
    if FAILURES:
        print(f"\n{len(FAILURES)} CHECK(S) FAILED:")
        for failure in FAILURES:
            print("  " + failure)
        return 1
    print("\nALL CHECKS PASSED")
    return 0


@contextlib.contextmanager
def as_device(dev: devices.DeviceModel):
    """Pin the active desk model for the block, so device-aware code runs with no desk attached."""
    previous = devices.force_device(dev)
    try:
        yield dev
    finally:
        devices.force_device(previous)


@contextlib.contextmanager
def patched(*targets):
    """Replace module or object attributes for the block, each restored however it ends.

    Each target is `(owner, {name: replacement, ...})`:

        with patched((server, {"_ann_say": said.append}), (rcp_read, {"read_model": fake})):
    """
    with contextlib.ExitStack() as stack:
        for owner, attrs in targets:
            for name, value in attrs.items():
                stack.enter_context(mock.patch.object(owner, name, value))
        yield


def decode_record(rec: bytes):
    """`model_tree.parse_record`'s (path, name, value, kind, end) for one built report-3 record."""
    n = int.from_bytes(rec[1:5], "little")
    return model_tree.parse_record(rec[5:5 + n])


def snapshot(blob: bytes) -> dict:
    """Every property in a model dump, keyed (tree path, block, name)."""
    root, _unparsed, _tail = model_tree.parse(blob)
    out = {}
    for node in model_tree.walk(root):
        path = tuple(node.path())
        for name, value, _kind in node.props:
            out[(path, node.name, name)] = value
    return out
