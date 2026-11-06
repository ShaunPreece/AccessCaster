"""Which desk properties hold a secret: a credential or a pairing code, whose value no output of
this app may carry. The diagnostic log, a saved profile and the DevTools' files all withhold these,
so each can be sent to somebody; a caller adds its own extras on top.
"""
from __future__ import annotations

import re

_SECRET = re.compile(r"psk|password|passwd|secret|token|paircode|rodecode|usercode", re.IGNORECASE)


def is_secret(name: str) -> bool:
    """True for a property NAME whose value is a secret."""
    return bool(_SECRET.search(name))
