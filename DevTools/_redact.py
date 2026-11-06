"""Which property values must never be printed, saved or published.

A model dump carries the wifi password and the SIP account credentials in plain text, so every
DevTools script that writes values anywhere passes them through `redact`.
"""
from __future__ import annotations

import re

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import desk_secrets

# Beyond the app's secrets, what identifies a setup: a published file names no network, account
# or unit.
_IDENTIFYING = re.compile(r"username|ssid|serial", re.IGNORECASE)

REDACTED = "<redacted>"


def redact(name: str, value):
    """`value`, or `REDACTED` when `name` (a property name or a `BLOCK.name` key) is secret."""
    prop = name.rpartition(".")[2]
    return REDACTED if desk_secrets.is_secret(prop) or _IDENTIFYING.search(prop) else value
