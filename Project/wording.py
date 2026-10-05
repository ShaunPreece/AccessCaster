"""AccessCaster - small helpers for sentences that are spoken as well as shown.

Everything the app says passes through a screen reader or the announcer, so counts, lists and
lengths are written as a person says them: "2 minutes 5 seconds", never "2:05"; "Mic 1, Mic 2 and
Chat", never "Mic 1 + Mic 2 + Chat".
"""
from __future__ import annotations


def plural(n: int, word: str, many: str | None = None) -> str:
    """`word` or its plural for a count of `n`, without the number: plural(2, "pad") is "pads".

    `many` is the plural when it is not simply `word` + "s" (plural(n, "entry", "entries")), and
    also serves verbs that agree with a count (plural(n, "was", "were"))."""
    if n == 1:
        return word
    return many if many is not None else word + "s"


def count(n: int, word: str, many: str | None = None) -> str:
    """The number and the word together: count(3, "mic") is "3 mics"."""
    return f"{n} {plural(n, word, many)}"


def join_and(items) -> str:
    """A spoken list: "A", "A and B", "A, B and C". Empty gives ""."""
    items = list(items)
    if len(items) <= 1:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " and " + items[-1]


def describe_duration(seconds) -> str:
    """A length said the way a person says it. Never "0:07.3": a screen reader reads punctuation
    and a decimal, and neither carries meaning here. The smaller unit is dropped when it is zero,
    and seconds are dropped altogether once the length passes an hour."""
    if seconds is None:
        return "length unknown"
    seconds = int(round(seconds))
    if seconds < 60:
        return count(seconds, "second")
    mins, secs = divmod(seconds, 60)
    if mins < 60:
        return count(mins, "minute") + (f" {count(secs, 'second')}" if secs else "")
    hours, mins = divmod(mins, 60)
    return count(hours, "hour") + (f" {count(mins, 'minute')}" if mins else "")
