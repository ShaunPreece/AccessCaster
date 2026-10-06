/* The Recording page. Run:
     python DevTools/ui_probe.py --checks DevTools/ui_checks_recording.js --route recording --timeout 240

   IT NEEDS A SERVER THAT HAS /api/recorder. The probe proxies reads to whatever is on 8770, and the
   installed 2.00 build predates this page, so start DevTools/run_dev_server.py first. The suite
   checks this itself and says so in one line rather than failing seven ways (see the precondition
   below).

   WHAT IS WORTH ASSERTING HERE, and it is not the plumbing. Three things make this page different
   from every other one in the app:

     1. **ONE live region.** Everything spoken goes through `rec-state`. A second status line for
        the result of a press makes Start fire two regions at once, and a reader reads one over the
        other. So: exactly one aria-live region in the section, and the transport buttons write to
        THAT one.
     2. **A flat page, on purpose.** This is the page you may need in a hurry, so there is no tab
        strip between arriving and pressing Stop. The check pins that, because "make it consistent
        with the other pages" is exactly the change someone would make later without knowing why.
     3. **It never claims a recording is running.** The reply comes from a read-back. The probe
        STUBS every POST, so nothing here reaches the desk - which also means the state after a
        stubbed press is not asserted, only what was SENT.
     4. **THE COPY IS SHORT.** Copy that explains why things work rather than what they do reads
        like dev notes. The reasoning belongs in comments; the page gets the functional line. Two
        checks pin that, because verbosity is the thing that creeps back.

   The record button's label follows the state (Start recording / Pause / Resume), rather than there
   being a separate Pause. After an action you expect to hear the state CHANGE, so a button still
   reading "Start recording" once it is recording is worse than a label that moves. */

const el = (id) => document.getElementById(id);
const sec = () => el("sec-recording");

async function waitFor(fn, ms) {
  const until = Date.now() + (ms || 8000);
  while (Date.now() < until) {
    try { if (fn()) return true; } catch (e) { /* not ready yet */ }
    await sleep(80);
  }
  return false;
}

const posts = (log) => log.filter((r) => r.method === "POST" && r.path !== "/api/ping");

function nameOf(node) {
  if (!node) return "";
  if (node.getAttribute("aria-label")) return node.getAttribute("aria-label");
  const lab = node.closest("label") || document.querySelector(`label[for='${node.id}']`);
  return lab ? lab.textContent.trim() : node.textContent.trim();
}

/* The hints are wrapped source, so a check must match across whitespace rather than on an exact
   phrase. An earlier version failed purely on a line break, which is a false alarm about wording
   that was in fact correct. */
function flatText(node) {
  return node ? node.textContent.replace(/\s+/g, " ").trim() : "";
}

/* aria-describedby may name SEVERAL elements, and the multitrack dropdown does exactly that: its
   standing explanation of the modes, plus the "not applied yet" hint the staged-select helper
   adds. Reading only the first id silently loses one of them. */
function described(node) {
  const ids = ((node && node.getAttribute("aria-describedby")) || "").split(/\s+/).filter(Boolean);
  if (!ids.length) return null;
  const box = document.createElement("div");
  ids.forEach((id) => {
    const found = el(id);
    if (found) box.appendChild(found.cloneNode(true));
  });
  return box;
}

/* THE PRECONDITION THIS SUITE HAS, and it is a real one: THERE HAS TO BE SOMEWHERE TO RECORD TO.
   With no card and no USB drive the unit reports state 3, and the page then deliberately marks the
   whole transport unavailable with a reason (applyRecordingUsable). That is correct behaviour, but
   it makes two checks below unanswerable: whether the record button is ever disabled by state
   alone, and what a press SENDS - a press of an aria-disabled button correctly sends nothing.
   Reported as skips, named as skips, rather than as failures with a cause nobody can see from the
   output. Put a drive in the second USB port to get them back.
   A CLUTCH OF FAILURES HERE MEANS NO PENDRIVE IN, not a server lacking /api/recorder. */
/* IT MUST WAIT FOR THE STATE, NOT SAMPLE IT ONCE. `/api/recorder/live` is answered from the
   watcher's last push, and straight after a service restart there has not been one, so a single
   read returns null. Reading that as "there IS a destination" reports the two unanswerable checks
   below as FAILURES: two red lines that really mean "the server started four seconds ago". So:
   poll briefly, then fall back to the authoritative /api/recorder read, which costs a dump but
   always knows. */
async function liveState() {
  const until = Date.now() + 12000;
  while (Date.now() < until) {
    try {
      const body = await (await fetch("/api/recorder/live")).json();
      if (typeof body.state === "number") return body.state;
    } catch (e) { /* not up yet */ }
    await sleep(400);
  }
  try {
    const body = await (await fetch("/api/recorder")).json();
    return typeof body.state === "number" ? body.state : null;
  } catch (e) {
    return null;
  }
}

function skip(name, why) {
  check("SKIPPED " + name, true, why);
}

async function run() {
  const state0 = await liveState();
  const noDestination = state0 === 3;
  await waitFor(() => sec() && !sec().hidden, 20000);
  // Page entry reads the unit (~1.5 s), so wait for the state line to stop saying "reading".
  await waitFor(() => !/Reading the unit/i.test(el("rec-state").textContent), 25000);
  await sleep(600);

  // ---- shape ------------------------------------------------------------
  check("the Recording page is reachable at its own route", !!sec() && !sec().hidden);
  check("it is a FLAT page: no tab strip between you and the Stop button",
    !sec().querySelector("[role='tablist']"));
  const h1 = sec().querySelector("h1");
  check("it has one h1, focusable so the router can land on it",
    h1 && h1.getAttribute("tabindex") === "-1", h1 && h1.textContent);
  const headings = Array.from(sec().querySelectorAll("h2")).map((h) => h.textContent.trim());
  // FOUR headings, the card and drive last, and the USB multitrack pair is NOT among them: it lives
  // on Routing > USB audio, because it is an OUTPUT and never touches the drive, the transport or
  // the recordings list. A control under "Recording" that has nothing to do with recording is the
  // exact confusion that renaming Routing's own "Recording" tab removes.
  check("four headings, in the order you need them, the card and drive last",
    headings.length === 4 && /card and drive/i.test(headings[3] || ""), headings.join(" | "));
  // WHAT TO RECORD comes before the transport: it is the decision you cannot fix afterwards.
  check("what to record comes first, ahead of the transport",
    /what to record/i.test(headings[0] || ""), headings[0]);
  check("then start and stop, then the reading matter",
    /start/i.test(headings[1] || "") && /recordings/i.test(headings[2] || ""),
    headings.join(" | "));
  // ...and the page SAYS where the USB setting went, so its absence reads as a decision rather
  // than as something missing.
  check("it points at where the USB setting lives now",
    /usb/i.test(flatText(el("rec-usb-pointer"))) && /routing/i.test(flatText(el("rec-usb-pointer"))),
    flatText(el("rec-usb-pointer")));

  // ---- the one live region ----------------------------------------------
  const live = Array.from(sec().querySelectorAll("[aria-live]"));
  check("EXACTLY ONE live region in the whole section", live.length === 1,
    live.map((n) => n.id).join(","));
  check("and it is the state line", live[0] && live[0].id === "rec-state", live[0] && live[0].id);
  check("it is polite, not assertive: this page talks while you work",
    live[0] && live[0].getAttribute("aria-live") === "polite");
  check("it is atomic, so a changed sentence is read whole rather than word by word",
    live[0] && live[0].getAttribute("aria-atomic") === "true");
  check("the state line actually says something after the read",
    (el("rec-state").textContent || "").length > 10, el("rec-state").textContent);

  // ---- the transport ----------------------------------------------------
  // TWO buttons, not three: the record button's label follows the state, which is the ordinary
  // transport pattern. After an action you expect to hear the state CHANGE, so a button still
  // reading "Start recording" once it is recording is worse than a label that moves under you.
  const start = el("rec-start"), stop = el("rec-stop"), chk = el("rec-check");
  check("there is no separate Pause button", !el("rec-pause"));
  check("Record, Stop and Check are all present", !!start && !!stop && !!chk);
  [["record", start], ["stop", stop], ["check", chk]].forEach(([n, b]) => {
    check(`the ${n} control is a real button`,
      b && b.tagName === "BUTTON" && b.type === "button", b && b.tagName);
    check(`the ${n} button is reachable by keyboard`, b && b.tabIndex >= 0);
  });
  check("the status button is named for what it reports",
    /recording status/i.test(nameOf(chk)), nameOf(chk));
  check("the record button names the action, not a symbol",
    /recording|pause|resume/i.test(nameOf(start)), nameOf(start));
  check("Stop says what it stops", /stop recording/i.test(nameOf(stop)), nameOf(stop));
  check("the button carries the command it is currently labelled with",
    !!start.dataset.command, start.dataset.command);
  check("with nothing recording, it offers to start",
    /start recording/i.test(nameOf(start)) && start.dataset.command === "start",
    nameOf(start) + " / " + start.dataset.command);

  // The label follows the STATE, wherever the state came from - including a press of the unit's
  // own REC button, which arrives as a push rather than as anything this page did.
  applyTransportLabels(2);
  check("while recording, the same button offers Pause",
    /^pause$/i.test(nameOf(start)) && start.dataset.command === "pause",
    nameOf(start) + " / " + start.dataset.command);
  applyTransportLabels(1);
  check("while paused, it offers Resume rather than Start",
    /resume/i.test(nameOf(start)) && start.dataset.command === "start",
    nameOf(start) + " / " + start.dataset.command);
  applyTransportLabels(0);
  check("stopped, it is back to Start recording",
    /start recording/i.test(nameOf(start)), nameOf(start));

  // ---- what is USABLE right now -----------------------------------------
  // aria-disabled, never the `disabled` attribute: a natively disabled button leaves the tab order
  // entirely, so Stop would vanish between takes and reappear later, which is far more
  // disorienting than hearing "unavailable" where you expect it to be.
  check("with nothing recording, Stop is unavailable",
    stop.getAttribute("aria-disabled") === "true", stop.getAttribute("aria-disabled"));
  check("but it is still in the tab order, not removed from the page",
    !stop.disabled && stop.tabIndex >= 0, stop.disabled);
  applyTransportLabels(2);
  check("while recording, Stop is available", !stop.hasAttribute("aria-disabled"));
  applyTransportLabels(1);
  check("while paused, Stop is still available", !stop.hasAttribute("aria-disabled"));
  applyTransportLabels(0);
  if (noDestination) {
    // The page's OWN answer to state 3 is the thing worth pinning while we are in it: refuse, but
    // say why, and stay in the tab order so the reason can be heard.
    check("with nowhere to record to, the record button is unavailable AND carries the reason",
      start.getAttribute("aria-disabled") === "true"
      && (start.dataset.offReason || "").length > 10 && start.tabIndex >= 0,
      start.dataset.offReason);
    skip("the record button is never made unavailable by state alone",
      "the unit has no destination (state 3), so it is correctly unavailable for that reason");
  } else {
    check("the record button is never made unavailable by state alone",
      !start.hasAttribute("aria-disabled"));
  }

  // MARKERS. Proven by reading the drive on a PC: requestDropMarker writes a standard
  // WAV cue point (two at exactly 4.0 s and 8.0 s where they were dropped, against a marker-free
  // take with no cue chunk at all). A marker needs a POSITION, so it is offered only while
  // something is actually recording - paused counts as not recording, since nothing is going in.
  const marker = el("rec-marker");
  check("there is a Drop a marker button", !!marker && marker.tagName === "BUTTON");
  check("it is unavailable when nothing is recording",
    marker.getAttribute("aria-disabled") === "true");
  applyTransportLabels(1);
  check("and still unavailable while PAUSED, since nothing is going in",
    marker.getAttribute("aria-disabled") === "true");
  applyTransportLabels(2);
  check("available while recording", !marker.hasAttribute("aria-disabled"));
  applyTransportLabels(0);

  // Pressing it while unavailable SAYS why. Silence would be indistinguishable from a missed press.
  await probeClear();
  stop.click();
  await sleep(600);
  check("pressing an unavailable Stop posts NOTHING",
    posts(await probeLog()).length === 0);
  check("and says why, rather than doing nothing at all",
    /nothing is recording/i.test(el("rec-state").textContent), el("rec-state").textContent);

  await probeClear();
  marker.click();
  await sleep(600);
  check("the same goes for an unavailable marker: nothing posted",
    posts(await probeLog()).length === 0);
  check("and it says markers need something to be recording",
    /only be dropped while/i.test(el("rec-state").textContent), el("rec-state").textContent);

  await probeClear();
  applyTransportLabels(2);
  marker.click();
  await waitFor(async () => posts(await probeLog()).length > 0, 8000);
  const mlog = posts(await probeLog());
  check("while recording it posts to /api/recorder/marker, once",
    mlog.length === 1 && mlog[0].path === "/api/recorder/marker",
    JSON.stringify(mlog.map((r) => r.path)));
  applyTransportLabels(0);

  // ---- the copy is SHORT: the UI is not the place for the reasoning ----
  check("the page intro is one short line, not an essay",
    flatText(el("rec-note")).length < 160, flatText(el("rec-note")).length);
  check("the transport has no hint paragraph at all: the labels say it",
    !el("rec-transport-hint") && !el("rec-check-hint"));

  // ---- announcements going INTO the recording ----------------------------
  // The announcer's output defaults to the system default device, which on a machine with a
  // RODECaster plugged in IS the RODECaster - so this app's speech travels down USB 1 into the mix
  // and into every recording, and nothing shows it until the recording is played back. Shaun
  // spotted it the moment the recorder shipped.
  const warn = el("rec-announcer-warn");
  const mute = el("rec-mute-announcer");
  check("there is a switch for it, so it is a CHOICE and not a policy",
    !!mute && mute.type === "checkbox", mute && mute.type);
  check("its label says what it does", /announcements off while recording/i.test(nameOf(mute)),
    nameOf(mute));
  // The MECHANISM is where announcements are ROUTED, which covers a microphone in the room and a
  // feed into the mix over USB alike. Naming headphones instead names only one way of it.
  check("its hint names the mechanism, ROUTING, not one example of it",
    /routed/i.test(flatText(described(mute))) && !/headphones/i.test(flatText(described(mute))),
    flatText(described(mute)));
  check("there is a place for the statement of what will happen", !!warn);
  check("it is a note, NOT a second live region talking over the state line",
    warn.getAttribute("role") === "note" && !warn.hasAttribute("aria-live"));
  if (!warn.hidden) {
    check("and it says announcements stop and come back",
      /switched off while recording/i.test(flatText(warn)), flatText(warn));
  }
  // The old fix-it button is GONE on purpose: moving our speech to another output still plays it
  // in the room, where the microphones hear it. That control belongs on the Device page, for the
  // separate problem of announcements vanishing when a channel is soloed.
  check("no move-the-output button on this page", !el("rec-announcer-btn"));
  await probeClear();
  mute.click();
  await waitFor(async () => posts(await probeLog()).length > 0, 8000);
  const alog = posts(await probeLog());
  check("the switch posts to /api/recorder/announcer-mute, once",
    alog.length === 1 && alog[0].path === "/api/recorder/announcer-mute",
    JSON.stringify(alog.map((r) => r.path)));

  // ---- what to record ---------------------------------------------------
  const mt = el("rec-multitrack-mode");
  check("multitrack lives on THIS page now", !!mt);
  check("it is a native select", mt && mt.tagName === "SELECT", mt && mt.tagName);
  check("it has a label", nameOf(mt).length > 3, nameOf(mt));
  check("its hint explains pre-fader versus post-fader in plain words",
    /pre-fader/i.test(flatText(described(mt))));
  // EVERY OPTION NAMES THE RESULT. The unit's own words ("Off", "Pre-Fader", "Post-Fader") never
  // say whether you are getting multitrack at all, so "does post-fader mean multi-track?" goes
  // unanswered - and answering it is the label's job.
  const opts = Array.from(mt.options).map((o) => o.textContent.trim());
  check("three choices, and each one says what you GET",
    opts.length === 3 && opts.every((o) => /track/i.test(o)), opts.join(" | "));
  check("the two multitrack ones say SEPARATE TRACKS in so many words",
    opts.filter((o) => /separate tracks/i.test(o)).length === 2, opts.join(" | "));
  // THE LABEL MUST SAY YOU GET THE MIX AS WELL. Multi-track ADDS files, it does
  // not replace the mix, and "Pre-fader multi-track" beside "Single mixed track" read as a choice
  // between the two. A real take writes Stereo Mix plus nine Track*.wav.
  check("and both of them say you still get the mix",
    opts.filter((o) => /plus the mix/i.test(o)).length === 2, opts.join(" | "));
  // FRONT-LOADED: arrowing reads each option from the start, so the word that distinguishes them
  // has to come before the part they share.
  check("the distinguishing word comes FIRST, not after the shared part",
    /^pre-fader/i.test(opts[1]) && /^post-fader/i.test(opts[2]), opts.slice(1).join(" | "));
  check("and the off one says you still get a recording",
    /mixed track only/i.test(opts[0]), opts[0]);

  // ---- REGRESSION: applying a multitrack mode must free the raw switch ---
  // The switch is available only while a multitrack mode is chosen, so that decision has to be
  // re-run after an Apply. Left keyed to the mode the page was last READ with, it goes on saying
  // "choose pre-fader or post-fader above first" - which you just did - and only leaving the page
  // and coming back clears it. This drives the real gesture: arrow, Enter, and it must free up with
  // no re-entry. POSTs are stubbed by the probe, so this tests the WIRING, not the desk write.
  const key = (s, k) => s.dispatchEvent(new KeyboardEvent("keydown",
                                        { key: k, bubbles: true, cancelable: true }));
  const arrowToIndex = (s, i) => {
    key(s, "ArrowDown");
    s.selectedIndex = i;
    s.dispatchEvent(new Event("change", { bubbles: true }));
  };
  const mtSel = el("rec-multitrack-mode");
  const bypass = () => el("rec-bypass");
  if (mtSel && bypass()) {
    const wasIndex = mtSel.selectedIndex;
    // Start from "single mixed track", which is the state that makes the switch unavailable.
    arrowToIndex(mtSel, 0);
    key(mtSel, "Enter");
    await waitFor(() => bypass().hasAttribute("aria-disabled"), 12000);
    check("with no multitrack chosen, the raw switch is unavailable",
      bypass().hasAttribute("aria-disabled"));
    // Now choose a multitrack mode and apply it.
    arrowToIndex(mtSel, 1);
    check("arrowing alone does not free it: nothing is applied yet",
      bypass().hasAttribute("aria-disabled"));
    key(mtSel, "Enter");
    await waitFor(() => !bypass().hasAttribute("aria-disabled"), 12000);
    check("APPLYING a multitrack mode frees the raw switch, with no page re-entry",
      !bypass().hasAttribute("aria-disabled"),
      "aria-disabled=" + bypass().getAttribute("aria-disabled"));
    check("and its hint switches to the real explanation",
      !/does nothing/i.test(flatText(el("rec-bypass-hint"))),
      flatText(el("rec-bypass-hint")).slice(0, 70));
    arrowToIndex(mtSel, wasIndex);                  // leave the control as we found it
    key(mtSel, "Enter");
    await sleep(500);
  }

  // ---- the recording FORMAT (WAV / MP3), added ---------
  // It sits with "what to record" because both are decisions that cannot be fixed after the take.
  // THE COUNT IS THE POINT: `recordingCompressionQuality` is an unclamped store whose enum has two
  // members, and takes at 1, 2 and 3 came back byte-identical. Offering three MP3 "qualities" would
  // be inventing a difference the desk does not make, so a third option here is a REGRESSION.
  const fmt = el("rec-format-mode");
  check("the recording format control is on this page", !!fmt);
  check("it is a native select", fmt && fmt.tagName === "SELECT", fmt && fmt.tagName);
  check("it has a label", nameOf(fmt).length > 3, nameOf(fmt));
  const fopts = fmt ? Array.from(fmt.options).map((o) => o.textContent.trim()) : [];
  check("EXACTLY TWO options: the desk has no third format", fopts.length === 2, fopts.join(" | "));
  check("one names WAV and one names MP3",
    fopts.some((o) => /\bWAV\b/.test(o)) && fopts.some((o) => /\bMP3\b/.test(o)),
    fopts.join(" | "));
  check("its hint gives the trade in plain words (size against quality)",
    /size|smaller|uncompressed/i.test(flatText(described(fmt))), flatText(described(fmt)));

  // ARROWING IT MUST SEND NOTHING. Same rule as every other audio-changing dropdown, and
  // this one decides a whole take's format, so a stray arrow press would be expensive.
  await probeClear();
  if (fmt) {
    arrowToIndex(fmt, (fmt.selectedIndex + 1) % fmt.options.length);
    await sleep(900);
  }
  const fPosts = posts(await probeLog()).filter((r) => r.path.indexOf("/api/recorder/format") === 0);
  check("arrowing the format dropdown writes NOTHING until Enter", fPosts.length === 0,
    JSON.stringify(fPosts.map((r) => r.path)));
  if (fmt) fmt.dispatchEvent(new FocusEvent("blur"));   // leaving puts it back
  check("neither dropdown has an Apply button",
    !el("rec-settings-apply") && !el("rec-format-mode-apply") && !el("rec-multitrack-mode-apply"));
  check("each dropdown is described as applying on Enter",
    ["rec-format-mode", "rec-multitrack-mode"].every((id) =>
      (el(id).getAttribute("aria-describedby") || "").includes("enter-to-apply-hint")));

  // THE STORAGE DETAIL IS NOT HERE AT ALL. It listed every volume including the unit's pad
  // storage and an empty card slot - a reading nobody wants mid-session. It lives on Device >
  // Hardware; where a recording GOES is in the status sentence and in the reply to pressing
  // record. The explaining moved to Help.
  check("no storage volume list on this page", !el("rec-dest-list"));
  check("no destination paragraph either", !el("rec-dest-line"));
  check("and the explaining has moved to the Help page, not this one", !el("rec-dest-hint"));
  check("the record button comes AFTER the what-to-record choice",
    mt.compareDocumentPosition(start) & Node.DOCUMENT_POSITION_FOLLOWING);

  // ---- the recordings list ----------------------------------------------
  const list = el("rec-list");
  check("the recordings list is a real list", !!list && list.tagName === "UL");
  check("it has an accessible name", !!list.getAttribute("aria-labelledby"));
  const listHint = el("rec-list-hint");
  check("it says how to get the recordings off",
    /computer/i.test(flatText(listHint)));
  check("without explaining the protocol reason, which belongs in the code",
    !/selector|address|model/i.test(flatText(listHint)), flatText(listHint));

  // ---- deleting a recording ---------------------------
  // The list stays a LIST: the choice is made in a labelled dropdown, because a list item is not a
  // control. The button carries its own reason when it cannot be used, which is the standing state
  // until the delete write is proven on hardware.
  const delSel = el("rec-delete-select");
  const delBtn = el("rec-delete-btn");
  check("the delete choice is a real dropdown with a label",
    !!delSel && delSel.tagName === "SELECT" && !!document.querySelector("label[for='rec-delete-select']"));
  check("the delete button is described by the line that says it cannot be undone",
    delBtn.getAttribute("aria-describedby") === "rec-delete-hint"
    && /cannot be undone/i.test(flatText(el("rec-delete-hint"))));
  // NOT a live region of its own: this page has exactly one, checked above, and a delete reports
  // through it like every other outcome.
  check("deleting did not bring a second live region with it", !el("rec-delete-status"));
  check("nothing in the list itself became clickable",
    list.querySelectorAll("button, a, input, select").length === 0);
  // With nothing to delete, the dropdown and button are hidden rather than left as two stops that
  // can do nothing; the list already says there are no recordings.
  const noneOnUnit = delSel.options.length === 0;
  check("the delete controls are shown exactly when there is something to delete",
    el("rec-delete-box").hidden === noneOnUnit,
    `${delSel.options.length} recordings, hidden=${el("rec-delete-box").hidden}`);
  if (list.querySelectorAll("li").length && !/No recordings|Nothing to list/i.test(flatText(list))) {
    check("every recording on the unit is offered in the dropdown",
      delSel.options.length === list.querySelectorAll("li").length,
      delSel.options.length + " options for " + list.querySelectorAll("li").length + " items");
    check("and an option names the recording, its length and when it was made",
      /recorded/i.test(delSel.options[0].textContent), delSel.options[0].textContent);
  }
  // aria-disabled, never the disabled attribute: a button that leaves the tab order takes its
  // reason with it. With recordings on the unit and the delete proven it should
  // be USABLE - the gated state is still checked, because a Duo or an empty drive reaches it.
  if (delBtn.hasAttribute("aria-disabled")) {
    check("a delete button that cannot be used still says why",
      !!delBtn.dataset.offReason, delBtn.dataset.offReason);
    check("and it stays in the tab order", !delBtn.disabled);
  } else {
    check("with recordings on the unit, deleting is offered rather than explained away",
      delSel.options.length > 0 && !delBtn.disabled);
  }

  // ---- node identity: a re-read must not replace what a reader is sitting in ----
  const firstItem = list.firstElementChild;
  await loadRecording();
  await sleep(300);
  check("re-reading keeps the recordings list's own item nodes",
    list.firstElementChild === firstItem);
  check("re-reading keeps the what-to-record dropdown's own node",
    el("rec-multitrack-mode") === mt);

  // ---- the writes. Stubbed by the probe, so nothing reaches the unit. ----
  let log;
  if (noDestination) {
    // A press of an unavailable button must SAY why rather than doing nothing silently, and it must
    // not send a command. Both are checkable without a drive, so this is not a lost check, only a
    // different one.
    await probeClear();
    start.click();
    await sleep(800);
    log = posts(await probeLog());
    check("with nowhere to record to, pressing Record sends NOTHING",
      log.length === 0, JSON.stringify(log.map((r) => r.path)));
    check("and it says why instead of failing silently",
      /nowhere to record|memory card|USB/i.test(el("rec-state").textContent || ""),
      el("rec-state").textContent);
    skip("Start posts to /api/recorder/command and nothing else",
      "the transport is correctly refused with no destination; put a drive in the second USB port");
  } else {
    await probeClear();
    start.click();
    await waitFor(async () => posts(await probeLog()).length > 0, 8000);
    log = posts(await probeLog());
    check("Start posts to /api/recorder/command and nothing else",
      log.length === 1 && log[0].path === "/api/recorder/command",
      JSON.stringify(log.map((r) => r.path)));
  }


  await probeClear();
  chk.click();
  await sleep(1200);
  log = posts(await probeLog());
  check("Check the recorder WRITES NOTHING: it only asks", log.length === 0,
    JSON.stringify(log.map((r) => r.path)));

  await probeDone();
}

run().catch(async (e) => {
  check("the checks ran to the end", false, String((e && e.stack) || e));
  await probeDone();
});
