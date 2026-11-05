"""Which property values must never be printed, saved or published.

A model dump carries the wifi password and the SIP account credentials in plain text, so every
DevTools script that writes values anywhere passes them through `redact`.
"""
from __future__ import annotations

import re

# Any property whose NAME matches this never has its value printed or saved.
SECRET_NAME = re.compile(r"psk|password|paircode|rodecode|authusername|username|ssid|serial",
                         re.IGNORECASE)

REDACTED = "<redacted>"


def redact(name: str, value):
    """`value`, or `REDACTED` when `name` (a property name or a `BLOCK.name` key) is secret."""
    return REDACTED if SECRET_NAME.search(name.rpartition(".")[2]) else value
