"""Read-only pad-table listing in STREAM ORDER, with the bank and pad number each padIdx means.

The left column is the STREAM POSITION (0-based), which is the targeting selector for assign_empty.
padIdx is the pad's fixed place in the grid and does not move; position does, whenever any pad is
created or cleared.

TYPE AND NAME COME FROM `soundpad.pad_positions`, NOT FROM A WINDOW AFTER padName, and that is the
whole correctness of this file. padType is the one field that can sit BEFORE padName: factory
records store padName first, records written by us or the RODE app store padType first. So a search
of the bytes FOLLOWING padName drops the type into the previous pad's window and prints the WRONG
TYPE silently - the Duo's "Cut" pad reads as type None when it is type 6. `pad_positions` pairs the
two BY INDEX across the whole dump, which is robust to the field order either way round. The
remaining windowed reads here (colour, file path) are fields that genuinely do follow padName.

Usage: python DevTools/list_pads.py
"""
import _bootstrap  # noqa: F401  - puts Project/ on sys.path
import devices
import rcp_read as rr
import soundpad

# Fields that really do travel AFTER a padName, so a bounded window is safe for these.
FOLLOW = ["padColourIndex", "padFilePath"]

# The enumeration is capture-confirmed; see SOUNDPADS.md. Type 3 is MIXER (its subtypes via
# padMixerMode are Censor, Trash Talk, Fade In/Out, Back Channel, Ducking), which is why a Duo's
# "Censor" and "Ducking" pads read as 3 and are not voice effects.
TYPE_LABEL = {1: "sound", 2: "voiceFX", 3: "mixer", 4: "MIDI", 5: "quickDial", 6: "system"}


def list_pads():
    blob = rr.read_model_stable(fresh=True)
    pads = soundpad.pad_positions(blob)
    per_bank = devices.get_active_device().pad.pads_per_bank
    print(f"model {len(blob)} bytes, {len(pads)} pad records, {per_bank} pads per bank "
          f"on the {devices.get_active_device().name}\n")
    print(f"{'pos':>3}  {'name':<18} {'type':>5} {'':<8} {'padIdx':>6} {'bank/pad':>9} "
          f"{'col':>4}  filePath")
    starts = [i for i in _name_starts(blob)]
    for pos, padidx, padtype, name in pads:
        a = starts[pos] if pos < len(starts) else 0
        end = starts[pos + 1] if pos + 1 < len(starts) else len(blob)
        props = {}
        for fp in FOLLOW:
            k = blob.find(fp.encode(), a, end)
            props[fp] = rr._val_after(blob, k + len(fp))[0] if k >= 0 else None
        fpath = props["padFilePath"] if isinstance(props["padFilePath"], str) else ""
        tail = fpath.split("/")[-2:] if "/" in fpath else [fpath]
        where = ("-" if padidx is None
                 else f"{padidx // per_bank + 1}/{padidx % per_bank + 1}")
        print(f"{pos:>3}  {str(name):<18} {str(padtype):>5} "
              f"{TYPE_LABEL.get(padtype, '?'):<8} {str(padidx):>6} {where:>9} "
              f"{str(props['padColourIndex']):>4}  {'/'.join(tail)}")


def _name_starts(blob):
    """Every padName start offset, in stream order: the window boundaries for the fields above."""
    start, key = 0, b"padName"
    while True:
        i = blob.find(key, start)
        if i < 0:
            return
        yield i
        start = i + len(key)


if __name__ == "__main__":
    list_pads()
