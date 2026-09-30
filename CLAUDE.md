# AccessCaster: project instructions

Accessibility is the first requirement on this project, not a feature: everything must work by
screen reader and keyboard alone, to WCAG 2.2 AA and the ARIA Authoring Practices, on NVDA, JAWS,
Narrator and VoiceOver alike. Nothing ships that fails that. Dates in prose are written as
"11th July 2026", never all digits, and no em or en dashes anywhere, because screen readers read
both badly.

## Where this project's knowledge lives

Every fact about the hardware or the code ships with the code, in `docs/`. The maintainer's working
notes are kept outside this repo: `ProjectNotes.md` and `ProjectMemory/` are gitignored, must never
be committed, and are resolved in code with `_bootstrap.notes_path` while `docs/` is resolved with
`_bootstrap.docs_path`. **A public file must not cite a private one**: a pointer to a notes file is a
dead link for everyone else, so state the fact instead. Check with:
`grep -nE "ProjectNotes|Completed\.md|DUO_SUPPORT|PROFILE_COVERAGE|PROPERTY_COVERAGE|RodeDocs" docs/*`
(`docs/*`, not `docs/*.md`: the protocol spec and the leads file are `.txt`.)

- `docs/RODECASTER_PROTOCOL.txt`: the definitive protocol spec (addresses, selectors, encodings,
  per-device deltas). Written for anyone building their own controller.
- `docs/PROJECT_GUIDE.md`: **the first thing to read.** Part 1 is how it all works, for someone
  starting from scratch. Part 2 is the load-bearing invariants: **grep it for the property, member
  or helper you are about to change**, not for the file name.
- `docs/CodeMap.md`: where things are, with line-numbered landmarks for `server.py` and `app.js`.
  Update it in the same edit that adds, deletes, renames or moves anything.
- `docs/PARITY.md`: the parity target, every feature the desk and the app have. No status column,
  by design, so it cannot go stale.
- `docs/POSSIBLE_LEADS.txt`: what could not be settled here for want of hardware, written for a
  stranger who has it. It opens with the write safety rules, so keep those current.
- `docs/SOUNDPADS.md`: read this first for any pad or voice-FX work.
- `docs/PROPERTY_LIST_<DESK>.md` (`_PRO2` / `_DUO`): generated, one per desk, every property that
  desk publishes. Regenerate with `DevTools/property_list.py` for whichever desk is attached. The
  two desks are usually on different firmware, so compare a count only against a desk on the same
  firmware: the Duo's names are a subset of the Pro II's, and the gap is storage and firmware, not
  capability.

## Where a new fact goes, and when it goes nowhere

In this order, stopping at the first yes:

1. Could it be re-broken? -> `PROJECT_GUIDE.md` part 2.
2. Is it a byte-level fact? -> `RODECASTER_PROTOCOL.txt`.
3. Does it only matter at one call site? -> a comment there.
4. **None of the above -> nowhere. Git history has it.**

Four rules for the reference files, each learnt the hard way:

- **No status column in a reference file.** It is a hand-maintained derived fact, so it rots.
- **No plan survives its own execution.** It then describes a future that has happened. Write the
  outcome to its home above and delete the plan.
- **Never narrate a change inside a file.** Write the pointer, not the story of what moved and why.
- **Never cite a file for a fact.** State the fact and date it.

Comments explain the code, in the present tense: what is true now, not what happened. No dates, no
"this used to be", no history. A comment earns its place only where a competent reader coming to
the code cold would otherwise be puzzled.

Run `python DevTools/check_notes.py --check` before you finish; it catches what a script can.

## Working on the desk

Reads are free. A write to the desk must be reversible and read back, and restored to baseline
afterwards. Never write to a selector that is unmapped or unconfirmed: a wrong selector does not
error, it appends a stray property that only a factory reset removes (`docs/PROJECT_GUIDE.md` part 2
and the protocol spec both cover this). Factory reset, and anything else that can persist a state
you cannot cleanly undo, needs the owner's say-so.
