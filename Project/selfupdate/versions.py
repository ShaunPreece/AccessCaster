"""
versions.py

Version ordering, as a direct port of the scheme's C# `UpdateVersion`. Both ends have to
agree about which of two versions is newer, so this is a translation of the C# reference rather
than a fresh design.

NOT `packaging.version`, which implements PEP 440 and not SemVer. The two disagree on labels
Hydra's builds can produce, PEP 440 accepts two-part versions that the C# parser refuses, and a
tool that orders versions differently from the apps reading its output is the one disagreement
nothing catches until a release goes wrong.

A VERBATIM COPY OF THE PUBLISHING TOOL'S versions.py, line width and all, apart from this
paragraph. The publishing tool and every app that reads its output have to order versions
identically, so this file is kept diff-clean against that one rather than restyled to match the
code around it. Change it there first.
"""

from __future__ import annotations

from functools import total_ordering


@total_ordering
class UpdateVersion:
    """A version as the apps write them, e.g. `1.0.0` or `1.0.0-beta.23`.

    Parsing REFUSES anything it does not understand rather than guessing, because acting on a
    misread version is the one thing an update check must never do."""

    __slots__ = ("major", "minor", "patch", "prerelease", "text")

    def __init__(self, major, minor, patch, prerelease, text):
        self.major = major
        self.minor = minor
        self.patch = patch
        self.prerelease = tuple(prerelease)
        self.text = text

    @property
    def is_prerelease(self):
        """True for a pre-release build, which is also how an app knows WHICH TRACK it is on: a
        build carrying a label reads the beta index, one without reads stable. Derived rather than
        stored, so installing the stable release stops a tester being offered betas."""
        return len(self.prerelease) > 0

    @property
    def track(self):
        return "beta" if self.is_prerelease else "stable"

    @classmethod
    def parse(cls, text):
        """Read a version, or None if it is not one."""
        if not text or not text.strip():
            return None

        # Build metadata ("+<commit sha>") is not part of the version and never reaches the
        # ordering.
        s = text.strip()
        plus = s.find("+")
        if plus >= 0:
            s = s[:plus]

        numbers, prerelease = s, ()
        hyphen = s.find("-")
        if hyphen >= 0:
            numbers = s[:hyphen]
            tail = s[hyphen + 1:]
            if not tail:
                return None
            prerelease = tuple(tail.split("."))
            # "1.0.0-" or "1.0.0-beta..1": an empty identifier is not something to guess at.
            if any(not p for p in prerelease):
                return None

        parts = numbers.split(".")
        if len(parts) != 3 or not all(_is_number(p) for p in parts):
            return None

        return cls(int(parts[0]), int(parts[1]), int(parts[2]), prerelease, s)

    def _key(self):
        return (self.major, self.minor, self.patch)

    def __eq__(self, other):
        if not isinstance(other, UpdateVersion):
            return NotImplemented
        return self._key() == other._key() and self.prerelease == other.prerelease

    def __lt__(self, other):
        if not isinstance(other, UpdateVersion):
            return NotImplemented
        if self._key() != other._key():
            return self._key() < other._key()

        # A RELEASE IS NEWER THAN ANY PRE-RELEASE OF THE SAME NUMBER. 1.0.0 beats 1.0.0-beta.23,
        # which is what hands beta users over to the release when it arrives.
        if not self.prerelease and other.prerelease:
            return False
        if self.prerelease and not other.prerelease:
            return True

        for mine, theirs in zip(self.prerelease, other.prerelease):
            c = _compare_identifier(mine, theirs)
            if c != 0:
                return c < 0

        # Equal as far as the shorter one goes, so more identifiers means later: "beta.1.2" is
        # after "beta.1".
        return len(self.prerelease) < len(other.prerelease)

    def __hash__(self):
        return hash((self.major, self.minor, self.patch, self.prerelease))

    def __str__(self):
        return self.text

    def __repr__(self):
        return f"UpdateVersion({self.text!r})"


def _is_number(part):
    """Digits only. `int()` alone accepts a leading sign and surrounding whitespace, neither of
    which is a version."""
    return len(part) > 0 and all("0" <= c <= "9" for c in part)


def _compare_identifier(a, b):
    """One pre-release identifier against another. Two numbers compare AS NUMBERS, which is the
    whole point: textually, "9" is after "10". A number ranks before a word, per SemVer, so
    "beta.2" comes before "beta.rc"."""
    na, nb = _is_number(a), _is_number(b)
    if na and nb:
        return (int(a) > int(b)) - (int(a) < int(b))
    if na:
        return -1
    if nb:
        return 1
    return (a > b) - (a < b)
