"""The shared harness for DevTools tests.

Each check prints one line and records a failure, and `finish` prints the tally and returns the
script's exit status. `as_device` and `patched` replace something for the length of a block and put
it back however the block ends. `decode_record` and `snapshot` read the desk's own record format,
and `push` builds the frame the desk pushes. `hold_desk`, `hold` and `hold_layout` set up the desk
model (`desk_model`) as a read and the desk's pushes would leave it.

    import _testkit
    from _testkit import check, check_true

    check("the label", got, want)
    ...
    sys.exit(_testkit.finish())
"""
from __future__ import annotations

import contextlib
import os
from unittest import mock

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import channels
import desk_model
import devices
import layout_fixture
import model_tree
import rcp_read
import rcp_transfer

FAILURES: list[str] = []

# AN OFFLINE SUITE ADDRESSES ITS WRITES ON A SAVED LAYOUT, the Pro II's on 1.7.7, never on whatever
# desk happens to be attached: `rcp_read.layout` would otherwise dump the real desk to build one.
# A live suite keeps the real desk's.
if (os.environ.get("ACCESSCASTER_NO_DEVICE_WRITE")
        and not os.environ.get("ACCESSCASTER_ALLOW_DEVICE_WRITE")):
    rcp_read.pin_layout(layout_fixture.load(layout_fixture.PRO2_177))


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


def addr(key) -> list:
    """The path the pinned layout gives a `model_tree.Layout` key: where a push for it arrives."""
    return rcp_read.layout().path(key)


@contextlib.contextmanager
def on_layout(name: str):
    """Address writes on a saved layout (`layout_fixture`) for the block."""
    previous = rcp_read.pin_layout(layout_fixture.load(name))
    try:
        yield
    finally:
        rcp_read.pin_layout(previous)


@contextlib.contextmanager
def patched(*targets):
    """Replace module or object attributes for the block, each restored however it ends.

    Each target is `(owner, {name: replacement, ...})`:

        with patched((speech, {"say": said.append}), (rcp_read, {"read_model": fake})):
    """
    with contextlib.ExitStack() as stack:
        for owner, attrs in targets:
            for name, value in attrs.items():
                stack.enter_context(mock.patch.object(owner, name, value))
        yield


def push(path, name: str, value_block: bytes) -> bytes:
    """A whole report-04 frame as the desk pushes it: `04 <len32> <record> <zeros to 256>`, the
    record being `<address> <name> 00 <value block>`. The announcer acts only on a frame that is
    exactly this, so a builder that cuts a corner tests nothing."""
    rec = model_tree.encode_path(path) + name.encode() + b"\x00" + value_block
    frame = bytes([0x04]) + len(rec).to_bytes(4, "little") + rec
    return frame + bytes(256 - len(frame))


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


def where(name: str) -> tuple:
    """The path of the node that carries `name` on the pinned layout."""
    return next(path for path, props in rcp_read.layout()._by_key.values() if name in props)


def _key_of(name: str) -> tuple:
    """The layout key of the node that carries `name` on the pinned layout."""
    return next(key for key, (_path, props) in rcp_read.layout()._by_key.items() if name in props)


def hold(key, name: str, value, kind: str) -> None:
    """The desk model holding `value` for `name` on the node `key` names, as a push leaves it."""
    desk_model.apply_push(tuple(addr(key)), name, value, kind)


def hold_layout(vec) -> None:
    """The desk model's fader layout, and the write cache with it, as a full read leaves them."""
    for strip, code in enumerate(vec):
        hold(rcp_transfer.channel_key(strip), "channelInputSource", code, "int")
    channels.publish_strip_vector(vec)


def hold_desk(vec=None) -> None:
    """The desk model freshly read from the pinned layout's shape (`layout_fixture.skeleton`), and
    trusted: every property 0 but the selected channel, which rests at -1 (none), and the fader
    layout, `vec`, which the write cache holds too."""
    values = {(_key_of("systemChannelSelected"), "systemChannelSelected"): -1}
    for strip, code in enumerate(vec or ()):
        values[(rcp_transfer.channel_key(strip), "channelInputSource")] = code
    desk_model._reset()
    desk_model.set_connected(True)
    desk_model.dump_finished(desk_model.dump_starting(),
                             layout_fixture.skeleton(layout_fixture.PRO2_177, values))
    if vec is not None:
        channels.publish_strip_vector(vec)
