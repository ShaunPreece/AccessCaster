"""Keeping AccessCaster itself up to date.

NOT `driver_install.py`, which fetches RODE's driver and has nothing to do
with AccessCaster's own version.

AccessCaster reads one small signed file per track from a bucket, compares the
version in it with its own, and tells the user when a newer one exists. The
rule the whole feature is built on is that CHECKING IS AUTOMATIC AND
INSTALLING NEVER IS: an app that rearranges itself unannounced is worst for a
screen-reader user, who finds out by walking into the change.

The index format, version ordering and signature rules belong to the publishing
tool that writes the index, not to this package. `versions.py`, the file names
and the signature checks copy its rules, so a change made here alone leaves
AccessCaster misreading what that tool publishes. Hydra and Claudio read the
same scheme.

What goes where:

  versions.py   SemVer ordering, and the track read off the running version
  signing.py    the public key, and Ed25519 verification that fails closed
  indexfile.py  the index document, parsed strictly
  check.py      the verdict, and where a track's files live
  interval.py   how often to look, including never
  checker.py    fetch, verify, then parse, in that order
  downloader.py the installer download and the proof it is the right file
  restart.py    being started again by the installer
  phrases.py    every sentence the feature says

The requests go through `web.opened`, beside this package, with the app's own User-Agent.

When to ask, how to tell the user, and the hand-off to the installer are
`app_updates.py`, beside this package.
"""
