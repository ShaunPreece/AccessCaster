"""
AccessCaster - generate a known burst of read-only traffic to the RODECaster.

Used only to find which USBPcap root hub the unit is on: run this while
capturing every hub, then see which capture caught the burst. Read-only, sends
nothing that changes device state.
"""

import sys
import time

import _bootstrap  # noqa: F401  - puts Project/ on sys.path
from rcp_hid import RodeCaster


def main(rounds: int = 40) -> int:
    try:
        dev = RodeCaster.open()
    except Exception as exc:
        print(f"open failed: {exc}")
        return 1
    print("generating read-only burst...")
    with dev:
        for n in range(rounds):
            dev.get_version()
            dev.ping()
            dev.get_info()
            time.sleep(0.02)
    print(f"done: {rounds} rounds (~{rounds * 3} commands)")
    return 0


if __name__ == "__main__":
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    raise SystemExit(main(rounds))
