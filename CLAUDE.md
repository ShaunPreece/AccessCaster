# AccessCaster: project instructions

Project-specific rules only. **Shaun's standing global rules are NOT duplicated here** (they were
until 28th August 2026, which meant they loaded twice on his own PC). They live in his personal
`~/.claude/CLAUDE.md`. On any machine that does not have that file, copy it over before working on
this project: the accessibility, confirmation-required and punctuation rules all matter here.

## Where this project's knowledge lives

**THE SPLIT IS FINDINGS HERE, CONVERSATION PRIVATE.** Anything that states a fact about the
hardware or the code ships WITH the code, in `docs/`. Anything carrying decisions, quotes, session
detail or open work is in a private sibling repo, `..\_Notes\Access Caster\`, so this repo can be
public without publishing it.

- **`docs/` in THIS repo** is tracked and public: the protocol guide, the code map, the parity
  target, the soundpad reference and the generated property lists. Resolve it in code with
  `_bootstrap.docs_path`.
- **`ProjectNotes.md` and `ProjectMemory/` are in the NOTES repo.** Every path below naming them is
  relative to THAT folder. Resolve it with `_bootstrap.notes_path`. A note must never be written
  back into this repo: `.gitignore` denies both names as a safety net, but a note pushed to a
  public remote cannot be taken back.
- **CHOOSING THE WRONG ONE OF THOSE TWO HELPERS EITHER PUBLISHES A NOTE OR HIDES A FINDING**, which
  is the whole reason there are two.
- `_Notes` is a SEPARATE repo, so committing the code does not save the notes.
- `CLAUDE.md`, `README.md` and `LICENSE` stay at this repo's root. Everything else public is in
  `docs/`, including the protocol spec and the leads file.

- `docs/RODECASTER_PROTOCOL.txt`: the definitive protocol spec (addresses, selectors,
  encodings, per-device deltas). **Written for anyone building their own controller**, which is why
  it is the one reference that stays on the public side.
- `ProjectNotes.md`: CURRENT only. Now / Next, standing setup and traps, tests owed, open
  items, version. Read it when resuming.
- `ProjectMemory/Completed.md`: what shipped and why, grouped by area, newest first. **A FEATURE
  LOG: if a user could not notice it, it does not go there.** A cleanup, a refactor, a new DevTools
  script or a bug fix leaves its invariant in `PROJECT_GUIDE.md` and its reasoning in the code, and
  nothing in Completed. See that file's own header.
- `docs/PROJECT_GUIDE.md`: **the user guide to this project, and the first thing to read.**
  Part 1 is how it all works, for someone starting from scratch. Part 2 is the load-bearing
  invariants. **Grep part 2 for the property, member or helper you are about to change**, not for
  the file name.
- `docs/CodeMap.md`: where things are, with line-numbered landmarks for `server.py` and
  `app.js`.
- `RodeDocs/`: RODE's own user guide for both desks, plus a walkthrough of their desktop app. Read
  it before exploring the hardware. **Theirs, not ours, which is why it is on the private side.**
- `docs/PROPERTY_LIST_<DESK>.md` (`_PRO2` / `_DUO`): GENERATED, ONE PER DESK. Every
  property that desk publishes, names and types only. The raw material; regenerate with
  `DevTools/property_list.py`, which writes the file for whichever desk is attached. Device-scoped
  since 31st August 2026, because a single shared file was silently overwritten by whichever desk
  was plugged in. **The Duo's names are a SUBSET of the Pro II's, not a match**, and the gap is
  STORAGE AND FIRMWARE rather than capability. On equal firmware (1.7.3, measured 22nd September
  2026) it was 463 against 472, the nine extra all `show*` / `recording*`, which a desk publishes
  only with storage attached that has shows on it. It is now 463 against 499 because the Pro II
  went to 1.7.6, which adds the Icecast set and the OAUTH block. Compare a count only against a
  desk on the same firmware. An earlier claim of "identical, 463 each" predates the drive going in.
- `docs/PARITY.md`: the parity TARGET, every feature the desk and app have. No status, by
  design, so it does not go stale. What is LEFT to build is in `ProjectNotes.md`.
- **A PUBLIC FILE MUST NOT CITE A PRIVATE ONE.** Anything in `docs/` is read by strangers, so a
  pointer to `ProjectNotes.md`, `Completed.md`, `DUO_SUPPORT.md` or a coverage report is a dead
  link for them. State the fact instead. Check with:
  `grep -nE "ProjectNotes|Completed\.md|DUO_SUPPORT|PROFILE_COVERAGE|PROPERTY_COVERAGE|RodeDocs" docs/*`
  (`docs/*`, not `docs/*.md`: the protocol spec and the leads file are `.txt`.)
- `docs/POSSIBLE_LEADS.txt`: what we could NOT settle, written FOR A STRANGER WITH HARDWARE WE DO
  NOT HAVE - a second Pro 2, a Duo with a card, a wireless transmitter, a headset for the 3.5mm
  jack. When an open item turns out to need hardware nobody here has, it belongs there as well as in
  `ProjectNotes.md`. It opens with the write safety rules, so keep those current.
- `docs/SOUNDPADS.md`: read this first for any pad or voice-FX work.

## Where a NEW fact goes, and when it goes nowhere

Ask what KIND of thing it is, in this order, and stop at the first yes:

1. Could a USER NOTICE it shipped? -> `Completed.md`.
2. Could it be RE-BROKEN? -> `PROJECT_GUIDE.md` part 2.
3. Is it STILL OPEN? -> `ProjectNotes.md`.
4. Is it a BYTE-LEVEL fact? -> `RODECASTER_PROTOCOL.txt`.
5. Does it only matter at ONE CALL SITE? -> a comment there.
6. **None of the above -> NOWHERE. Git history has it.**

**Line 6 is the one that matters.** The default was "write it down somewhere", and that is what grew
ProjectMemory to 25 files, most of them spent plans and decayed status. Run
`python DevTools/check_notes.py --check` before you finish; it catches what a script can.

Four prohibitions, each earned on 30th August 2026:

- **NO STATUS COLUMN in a reference file.** It is a hand-maintained derived fact, so it rots.
  `PARITY.md` listed 14 features as outstanding; 8 were already built and 5 were settled negatives.
- **NO PLAN SURVIVES ITS OWN EXECUTION.** It then describes a future that has happened, which means
  it lies about the present. Write the outcome to its home above and delete the plan.
- **NEVER NARRATE A CHANGE INSIDE A FILE.** Write the pointer, not the story of what moved and why.
- **NEVER CITE A FILE FOR A FACT.** State the fact and date it. If a citation is the only reason a
  file still exists, delete the file and inline the fact.

## Spoken status announcements

**During a self-drive session, speak a 1-2 line SPOKEN SUMMARY of EVERY chat message you send.**
While a self-driven app (e.g. the RODECaster App) is foregrounded, NVDA is reading THAT app, not the
chat, so written chat messages are NOT read out: Shaun hears a background beep and has no idea what
they said. So for the whole time he is self-driving, EVERY message must be accompanied by a
`Project/say.ps1` call summarising it. This is not optional and not just for action announcements:
it applies to progress updates, findings, questions, the lot. Also call it before each meaningful
driving step (navigating, starting or stopping a capture, toggling a control, confirming a restore).

`powershell -File Project/say.ps1 -Text "..."`

`say.ps1` SELF-GATES: it checks the foreground window and stays silent (prints "SAY skipped") when
the Claude GUI ("Claudio") is focused, and only speaks when another app has focus. So it is always
safe to call. Because it self-gates, default to calling it on every message during a driving session
rather than trying to guess focus. Keep each line to 1-2 lines, in addition to the written reply.

## Self-driven device tests

If a test is needed to confirm something about the RODECaster and you can do it by self-driving
(drive the RODE app or our own browser and HTTP, capture the bytes, compare to our builders, read the
model back, then restore and verify byte-clean), JUST DO IT. Do not stop to ask. This is a personal,
local, reversible device, so read-back-verified self-drive tests are NOT the "shared or production
system" case in the global confirmation rules. Announce what you are doing via `say.ps1` as you go.

Still pause and ask ONLY for genuinely irreversible or risky steps: a factory reset, anything that
can persist a bad state you cannot cleanly undo (recall the STRAYS incident), or a write whose
selector is unmapped or unconfirmed (stray-append hazard). Building, installing and pushing still
follow the global confirmation rules. Prefer reversible, read-back-verified writes and restore to
baseline afterwards.
