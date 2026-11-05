"""
AccessCaster - one channel on its own, for measuring: a temporary custom Chat mix carrying that
source alone, put back afterwards with the restore verified. Auto gain (`autogain_actions`) and the
level check (`level_check`) both listen through it, on the Chat capture endpoint.
"""

from __future__ import annotations

import time

import devices
import diagnostics
import meters
import output_mixes
import rcp_read
import rcp_transfer


NOT_RESTORED = ("WARNING: the Chat mix could not be put back. It may still be carrying one "
                "channel only. Check Routing, Output mixes, Chat.")

# A cross-point quieter than this cannot be measured through: correcting for it would multiply the
# noise floor up rather than the voice.
MIN_CROSSPOINT = 0.05


def endpoint_to_unity(endpoint_id: str) -> str:
    """Unmute the Chat capture endpoint and set its Windows level to 0 dB, and return the sentence
    saying what changed, which is "" when nothing did. Its level scales every reading and its mute
    makes a working microphone read as silence."""
    said = ""
    try:
        now = meters.endpoint_volume(endpoint_id)
        if now.get("muted") and meters.set_endpoint_mute(endpoint_id, False).get("ok"):
            said = "Cleared the Windows mute on the Chat device. "
        db = now.get("db")
        if ((db is None or abs(db) >= 0.2)
                and meters.set_endpoint_volume_db(endpoint_id, 0.0).get("ok")):
            said += "Windows level on the Chat device set to no change. "
    except Exception as exc:
        diagnostics.event("meters.chat_endpoint", "could not set the Chat endpoint", error=str(exc))
    return said


class ChatIsolation:
    """Make the Chat output carry ONE source for the length of a measurement, then put it back.
    Entered and restored under the HID lock.

    NOT the Main Multitrack USB device: switching that makes the unit RE-ENUMERATE ITS WHOLE USB
    AUDIO FUNCTION. Measured: all four desk endpoints go inactive for a second or two, Windows falls
    back to the computer's own sound card, taking the screen reader with it, and on the way back
    Windows picks a different default. Twice per run.

    A custom mix costs none of that, because nothing about the USB audio function changes. It is
    the same operation as the "Chat carries only Mic 1" preset this app already has, so it uses
    exactly the same calls, plus a snapshot and a verified restore.

    CHAT because it returns to the computer as its own capture endpoint, it is not the main mix, and
    a mix that is not the main one is what that output is for.

    IT IS POST-FADER, and that is corrected rather than ignored: the cross-point level is read and
    subtracted from the measurement, so the gain worked out does not move with the fader. Reading
    it is safer than writing the cross-point to unity and having that to put back as well.
    """

    def __init__(self, keep_source: int):
        self.keep = keep_source
        self.chat = rcp_transfer.CROSS_POINT_OUTPUT_INDEX["Chat"]
        # `all_sources`: isolating one channel means every OTHER row out, and in Expanded four of
        # them carry the computer's own audio. Leaving those in measures the screen reader.
        self.sources = [x["index"] for x in output_mixes.active_matrix().all_sources]
        self.level = 1.0
        self.mode_before = None
        self.excluded_before = {}
        self.changed = False
        self.restored = False

    def _snapshot(self):
        blob = rcp_read.read_model_stable(fresh=True)
        m = devices.get_active_device().matrix
        excl = [bool(v) for v in rcp_read.block_values(blob, m.block, "mixDisabled", "bool")]
        if len(excl) != m.cell_count:
            raise RuntimeError("the desk's mix grid is not the shape this expects "
                               "(a phone caller may be connected)")
        got = {src: excl[m.row_stride * src + self.chat] for src in self.sources}
        lv = rcp_read.read_matrix_levels(blob)
        if not lv.get("ok"):
            raise RuntimeError(lv.get("reason") or "could not read the mix levels")
        return lv["modes"].get(self.chat), got, lv["levels"].get(self.chat, {})

    def __enter__(self):
        self.mode_before, self.excluded_before, levels = self._snapshot()
        cell = levels.get(self.keep) or {}
        self.level = float(cell.get("level") or 0.0)
        if self.level < MIN_CROSSPOINT:
            raise RuntimeError("that channel is not reaching the Chat mix - its fader is down, or "
                               "it is excluded there - so nothing could be measured through it")
        # A with block does not call __exit__ when __enter__ raises, so a write that fails part way
        # puts back what the earlier ones changed here, and says so if that fails too.
        try:
            if self.mode_before != 2:
                self.changed = True
                rcp_transfer.set_output_routing(self.chat, 2)      # Custom
                time.sleep(0.3)
            for src in self.sources:
                want = (src != self.keep)
                if self.excluded_before.get(src) != want:
                    self.changed = True
                    rcp_transfer.set_cross_point_excluded(self.chat, src, want)
                    time.sleep(0.05)
        except Exception as exc:
            if not self.restore():
                raise RuntimeError(f"{exc}. {NOT_RESTORED}") from exc
            raise
        if self.changed:
            time.sleep(0.6)            # let the mix settle before anything is measured
        return self

    def __exit__(self, *exc):
        self.restore()
        return False

    def restore(self) -> bool:
        """Put the Chat mix back exactly, and CHECK that it went back.

        Verified rather than assumed, for the same reason auto gain's processing restore is: leaving
        somebody's Chat output carrying one microphone would break the next call they took, and
        they would have no way of knowing why. A mix that already carried the one source had
        nothing written to it, so there is nothing to put back."""
        if self.restored or self.mode_before is None or not self.changed:
            return True
        try:
            for src in self.sources:
                rcp_transfer.set_cross_point_excluded(self.chat, src,
                                                      self.excluded_before.get(src, False))
                time.sleep(0.05)
            if self.mode_before != 2:
                rcp_transfer.set_output_routing(self.chat, self.mode_before)
            time.sleep(0.6)
            mode_now, excl_now, _levels = self._snapshot()
            self.restored = (mode_now == self.mode_before
                             and excl_now == self.excluded_before)
            return self.restored
        except Exception as exc:
            diagnostics.event("chat.restore_failed", str(exc))
            return False
