"""
AccessCaster - the stereo pan link on the Channels page. A stereo channel pans with two legs.
Linked, one Pan control shifts both from where they were when linked. The desk has no such setting,
so the links live in settings.json, filed under the desk (`device_page.desk_record`):
{name: {"base": [l, r], "offset": n}}, in percent. `base` is kept so that returning the control to
centre restores the original width after an end stop has squeezed the legs together.
"""

from __future__ import annotations

import channels
import desk_access
import device_page

_LINKS_KEY = "pan_links"
PAN_LEGS = ("channelPanL", "channelPanR")


def pan_links() -> dict:
    """{channel name: link} for THIS desk; empty if the record belongs to another one."""
    return device_page.desk_record(_LINKS_KEY)


def _save_pan_links(links: dict) -> None:
    device_page.save_desk_record(_LINKS_KEY, links)


def shift_pan_legs(base, offset: int) -> list:
    return [max(0, min(100, int(b) + int(offset))) for b in base]


def linked_pan_offset(link: dict, left, right) -> int:
    """Where the linked control sits for legs now at `left` and `right` (percent): the stored
    offset, or 0 when the legs have moved since (the desk, a preset, an unlinked write), which
    links afresh from where they are. `action_set_linked_pan` makes the same call."""
    expect = shift_pan_legs(link["base"], link["offset"])
    if abs(expect[0] - left) > 1 or abs(expect[1] - right) > 1:
        return 0
    return int(link["offset"])


def pan_offset_words(offset: int) -> str:
    if offset == 0:
        return "centre"
    if offset <= -100:
        return "hard left"
    if offset >= 100:
        return "hard right"
    return f"{-offset} percent left" if offset < 0 else f"{offset} percent right"


def action_link_pan(name: str, on: bool, left: float, right: float) -> dict:
    """Link or unlink a stereo channel's two pan legs. `left` and `right` are where the legs are
    now, in percent, which becomes the link's centre."""
    ch = channels.ui_channel_map().get(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    links = pan_links()
    if on:
        links[name] = {"base": [int(round(left)), int(round(right))], "offset": 0}
    else:
        links.pop(name, None)
    _save_pan_links(links)
    return {"ok": True, "pan_links": links,
            "message": f"{ch['label']} pan sides {'linked' if on else 'unlinked'}."}


def action_set_linked_pan(name: str, offset: int, left: float, right: float) -> dict:
    """Move a linked stereo pan: both legs shift by `offset` (-100 to 100) from where they were
    when linked. `left` and `right` are where the caller shows the legs now, in percent; if they
    have moved since, the link starts again from them, as `linked_pan_offset` showed it."""
    ch = channels.ui_channel_map().get(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    links = pan_links()
    link = links.get(name)
    if link is None:
        return {"ok": False, "message": f"{ch['label']}'s pan sides are not linked."}
    if linked_pan_offset(link, left, right) != link["offset"]:
        link = {"base": [int(round(left)), int(round(right))], "offset": 0}
    off = max(-100, min(100, int(offset)))
    legs = shift_pan_legs(link["base"], off)
    try:
        with desk_access.hid_lock:
            for prop, pct in zip(PAN_LEGS, legs):
                channels.set_channel_processing(name, prop, pct / 100)
    except Exception as exc:
        return {"ok": False, "message": f"Could not set the pan: {exc}"}
    links[name] = {"base": link["base"], "offset": off}
    _save_pan_links(links)
    return {"ok": True, "pan_links": links, "legs": dict(zip(PAN_LEGS, legs)),
            "message": f"{ch['label']} pan {pan_offset_words(off)}."}
