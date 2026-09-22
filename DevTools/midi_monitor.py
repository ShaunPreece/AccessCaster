"""Accessible MIDI monitor - listens to the RODECaster's MIDI output and announces every
message in plain English, so a blind user can confirm a MIDI Trigger pad actually transmits.

The RODECaster Pro II appears to the PC as a USB MIDI input port ("MIDI function 0"). When you
press a Trigger-type SMART pad (padType 4) it sends a MIDI message out of that port. This script
opens the port, decodes each incoming message, prints it, and (with --speak) says it aloud via
say.ps1 (SAPI) so you hear it at the device without reading the terminal.

Usage:
  python midi_monitor.py                # print each message; run until Ctrl+C
  python midi_monitor.py --speak        # also say each message aloud (SAPI)
  python midi_monitor.py --seconds 30   # auto-stop after 30s (good for a background run)

Every decoded line is also appended to midi_monitor.log so a background run can be read back.
"""
import subprocess
import sys
import time

import rtmidi

LOG = "midi_monitor.log"


def _find_port(midi_in):
    """Return the index of the RODECaster / MIDI input port, or None."""
    for i, name in enumerate(midi_in.get_ports()):
        if "RODE" in name or "MIDI" in name:
            return i
    return None


def describe(msg):
    """Decode a raw MIDI message (list of ints) into a plain-English string."""
    if not msg:
        return None
    status = msg[0]
    kind, lo = status & 0xF0, status & 0x0F
    ch = lo + 1                                   # MIDI channels are 1-16 to humans
    if kind == 0xB0 and len(msg) >= 3:
        return f"Control Change: control {msg[1]}, value {msg[2]}, channel {ch}"
    if kind == 0x90 and len(msg) >= 3:
        if msg[2] == 0:                           # note-on velocity 0 == note-off
            return f"Note Off: note {msg[1]}, channel {ch}"
        return f"Note On: note {msg[1]}, velocity {msg[2]}, channel {ch}"
    if kind == 0x80 and len(msg) >= 3:
        return f"Note Off: note {msg[1]}, channel {ch}"
    if kind == 0xC0 and len(msg) >= 2:
        return f"Program Change: program {msg[1]}, channel {ch}"
    return "MIDI message: " + " ".join(f"{b:#04x}" for b in msg)


def speak(text):
    # Match the project's working SAPI pattern (announce_controls.py / server.py): this system's
    # PowerShell execution policy blocks .ps1 files, so say.ps1 must be invoked with the bypass.
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-File", "say.ps1", "-Text", text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def main():
    do_speak = "--speak" in sys.argv
    seconds = None
    if "--seconds" in sys.argv:
        seconds = float(sys.argv[sys.argv.index("--seconds") + 1])

    midi_in = rtmidi.MidiIn()
    port = _find_port(midi_in)
    if port is None:
        print("No RODECaster / MIDI input port found. Is the unit connected and MIDI enabled?")
        return 1
    name = midi_in.get_ports()[port]
    midi_in.open_port(port)
    banner = f"Listening on '{name}'. Press a MIDI Trigger pad now. Ctrl+C to stop."
    print(banner)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"\n--- monitor start ({name}) ---\n")
    if do_speak:
        speak("MIDI monitor ready. Press a trigger pad.")

    deadline = (time.time() + seconds) if seconds else None
    try:
        while deadline is None or time.time() < deadline:
            item = midi_in.get_message()        # (message, delta) or None
            if item:
                text = describe(item[0])
                if text:
                    # Milliseconds AND the raw bytes, because the question is usually about TIMING
                    # and DUPLICATES: whether a pad sends on press and off on release (momentary) or
                    # on alternate presses (latching), and whether an apparent double is really two
                    # messages or one 0x80 plus one 0x90-with-velocity-0, which decode alike.
                    stamp = time.strftime("%H:%M:%S") + ".%03d" % int((time.time() % 1) * 1000)
                    raw = " ".join("%02x" % b for b in item[0])
                    line = stamp + "  " + text + "   [" + raw + "]"
                    print(line, flush=True)
                    with open(LOG, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
                    if do_speak:
                        speak(text)
            else:
                time.sleep(0.005)               # poll; rtmidi has no blocking read here
    except KeyboardInterrupt:
        pass
    finally:
        midi_in.close_port()
        del midi_in
    print("Monitor stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
