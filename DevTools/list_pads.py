"""Read-only pad-table listing in STREAM ORDER, with the bank and pad number each padIdx means.

The left column is the STREAM POSITION (0-based), which is the pad write selector. padIdx is the
pad's fixed place in the grid and does not move; position does, whenever any pad is created or
cleared. Every field comes off the tree (`soundpad.pad_nodes`).

Usage: python DevTools/list_pads.py
"""
import _bootstrap  # noqa: F401  - puts Project/ on sys.path
import devices
import rcp_read as rr
import soundpad

# The enumeration is capture-confirmed; see SOUNDPADS.md. Type 3 is MIXER (its subtypes via
# padMixerMode are Censor, Trash Talk, Fade In/Out, Back Channel, Ducking), which is why a Duo's
# "Censor" and "Ducking" pads read as 3 and are not voice effects.
TYPE_LABEL = {1: "sound", 2: "voiceFX", 3: "mixer", 4: "MIDI", 5: "quickDial", 6: "system"}


def list_pads():
    blob = rr.read_model_stable(fresh=True)
    pads = soundpad.pad_nodes(blob)
    per_bank = devices.get_active_device().pad.pads_per_bank
    print(f"model {len(blob)} bytes, {len(pads)} pad records, {per_bank} pads per bank "
          f"on the {devices.get_active_device().name}\n")
    print(f"{'pos':>3}  {'name':<18} {'type':>5} {'':<8} {'padIdx':>6} {'bank/pad':>9} "
          f"{'col':>4}  filePath")
    for p in pads:
        fpath = p.props.get("padFilePath")
        fpath = fpath if isinstance(fpath, str) else ""
        tail = fpath.split("/")[-2:] if "/" in fpath else [fpath]
        where = "-" if p.idx is None else f"{p.idx // per_bank + 1}/{p.idx % per_bank + 1}"
        print(f"{p.position:>3}  {str(p.name):<18} {str(p.type):>5} "
              f"{TYPE_LABEL.get(p.type, '?'):<8} {str(p.idx):>6} {where:>9} "
              f"{str(p.props.get('padColourIndex')):>4}  {'/'.join(tail)}")


if __name__ == "__main__":
    list_pads()
