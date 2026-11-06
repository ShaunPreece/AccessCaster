"""The question before a firmware update: what it does and costs, and whether to back up first."""
from native.dialogs import BackupFirstDialog


def question_text(version, current):
    return (f"Firmware {version} replaces {current or 'the firmware'} on the desk. RODE offers no "
            "way back to an older version.\n\n"
            "The desk downloads the update itself, installs it and restarts, which takes about "
            "five minutes. The update keeps your settings, SMART pads, recordings, wifi and "
            "Bluetooth pairings.\n\n"
            "It is possible, but rare, that a firmware update could require touchscreen setup "
            "after the update.\n\n"
            "While the desk restarts its audio stops for a minute or two, so a screen reader "
            "speaking through it goes quiet until it is back. Do not switch the desk off while it "
            "updates.")


class FirmwareDialog(BackupFirstDialog):
    def __init__(self, parent, version, current, backup_name):
        super().__init__(
            parent, f"Install firmware {version}", question_text(version, current),
            "Back the desk up first",
            f"Saves your settings and SMART pads on this computer as {backup_name}, and a copy on "
            "the desk's drive if one is in. It takes a minute or two.",
            f"Install firmware {version}")
