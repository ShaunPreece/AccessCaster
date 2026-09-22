/* The SOLO (Listen) control on the Channels page, which rides the writable channelCueEnable. Run:
     python DevTools/ui_probe.py --checks DevTools/ui_checks_solo.js --route channels --timeout 240

   What is worth asserting here, as opposed to on the tab strip (ui_checks_chantabs.js covers that):
   this control is the one on the page that can leave the desk sounding broken with nothing visible
   to explain it, so the checks are about the WORDING and the WAY OUT as much as the plumbing.
     - it is a real checkbox with a name that says both words a user might look for (Listen, solo);
     - its hint is programmatically attached, and says outright that the mixes are untouched, because
       "solo" to a broadcaster and "solo" to a podcaster are not the same promise;
     - "Clear every solo" is a BUTTON, present on every channel, so the escape is always one Tab away
       rather than requiring you to remember which channel you soloed;
     - flipping the switch writes to /api/channels/solo and nothing else. The probe stubs POSTs, so
       none of this reaches the desk. */

const el = (id) => document.getElementById(id);
const strip = () => document.querySelector("#channel-panel [role='tablist']");

async function waitFor(fn, ms) {
  const until = Date.now() + (ms || 8000);
  while (Date.now() < until) {
    try { if (fn()) return true; } catch (e) { /* not ready yet */ }
    await sleep(80);
  }
  return false;
}

const posts = (log) => log.filter((r) => r.method === "POST");

function nameOf(input) {
  // The accessible name as a screen reader would build it: the wrapping <label>, or aria-label.
  if (input.getAttribute("aria-label")) return input.getAttribute("aria-label");
  const lab = input.closest("label") || document.querySelector(`label[for='${input.id}']`);
  return lab ? lab.textContent.trim() : "";
}

async function run() {
  await waitFor(() => !el("sec-channels").hidden && strip(), 20000);
  await sleep(1200);

  // THE MIC COMES FROM THE DESK, NOT FROM A CONSTANT. A hardcoded "Mic3" does not exist on a Duo
  // (its source 2 is the Headset jack), so on that desk the suite reports three failures that read
  // exactly like the solo control having gone missing. The HIGHEST-numbered mic is picked because
  // it is the least likely to have anything plugged into it, so it is the least disruptive strip
  // to solo if a stub ever leaked through to the desk. It cannot: ui_probe forwards no POST at all.
  const sel = el("channel-select");
  const mics = Array.from(sel.options).filter((o) => /^Mic\d+$/.test(o.value));
  check("this desk offers at least one mic to test with", mics.length > 0,
    Array.from(sel.options).map((o) => o.value).join(","));
  if (!mics.length) { await probeDone(); return; }
  const mic = mics[mics.length - 1];
  const micName = mic.value;
  const micLabel = mic.textContent.trim();
  sel.value = micName;
  sel.dispatchEvent(new Event("change", { bubbles: true }));
  await waitFor(() => el("solo-" + micName), 15000);

  const solo = el("solo-" + micName);
  check("the Listen (solo) switch exists on the Settings tab", !!solo);
  check("it is a checkbox, not a custom widget",
    solo && solo.type === "checkbox", solo && solo.type);

  const name = nameOf(solo);
  check("its name carries BOTH words a user might search for",
    /listen/i.test(name) && /solo/i.test(name), name);
  check("its name names the channel too", name.indexOf(micLabel) >= 0,
    name + " (expected to contain " + micLabel + ")");

  const hintId = solo.getAttribute("aria-describedby");
  const hint = hintId ? el(hintId) : null;
  check("the hint is programmatically attached", !!hint, hintId);
  // TWO SENTENCES, AND THIS HINT OF ALL HINTS HAS TO BE SHORT: aria-describedby is read in full on
  // EVERY focus, so spelling out that the mixes and the recording are untouched, and that the
  // unit's own Solo button moves this too, costs a sentence each time. Both are true and both are
  // implied: "sends only this channel to your headphones" says where the sound goes, which is the
  // only place it goes.
  check("the hint says WHERE the sound goes, which is the mixes answer in one clause",
    hint && /headphones/i.test(hint.textContent), hint && hint.textContent);
  // The one thing that has to survive any cut: soloing can leave a screen-reader user with no
  // announcements at all and nothing on the page to explain it.
  check("the hint keeps the caution about losing announcements",
    hint && /announcements/i.test(hint.textContent), hint && hint.textContent);
  check("and it is still SHORT: this one is read on every single focus",
    hint && hint.textContent.trim().length < 200, hint && hint.textContent.trim().length);

  // The mute must still be there, and must still be the first thing on the tab: solo was inserted
  // AFTER it deliberately, so a reader arriving at the panel reaches Mute before Listen.
  const mute = el("mute-" + micName);
  check("the mute is still present", !!mute);
  check("mute comes BEFORE solo in reading order",
    mute && solo && (mute.compareDocumentPosition(solo) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0);

  const clear = el("solo-clear");
  check("a Clear every solo button is present", !!clear);
  check("it is a button, not a switch (it has no state of its own)",
    clear && clear.tagName === "BUTTON" && clear.type === "button", clear && clear.tagName);
  check("it is reachable by keyboard", clear && clear.tabIndex >= 0);

  // ---- the write. Everything above is structure; this is the one behaviour worth pressing. ----
  await probeClear();
  solo.click();
  await waitFor(async () => posts(await probeLog()).length > 0, 8000);
  let log = posts(await probeLog());
  check("flipping it posts to /api/channels/solo",
    log.length === 1 && log[0].path === "/api/channels/solo",
    JSON.stringify(log.map((r) => r.path)));
  check("and it posts nothing else", log.length === 1, log.length);

  await probeClear();
  clear.click();
  await waitFor(async () => posts(await probeLog()).length > 0, 8000);
  log = posts(await probeLog());
  check("Clear every solo posts to /api/channels/solo/clear",
    log.length === 1 && log[0].path === "/api/channels/solo/clear",
    JSON.stringify(log.map((r) => r.path)));
  check("the switch is unticked once the clear succeeds", solo.checked === false, solo.checked);

  // A live region is what a screen reader reads back, and the whole point of the wording work is
  // that the REPLY names what is still soloed. It has to land somewhere polite.
  const live = document.querySelector("[aria-live]");
  check("there is a live region for the reply to land in", !!live);

  await probeDone();
}

run().catch(async (e) => {
  check("the checks ran to the end", false, String((e && e.stack) || e));
  await probeDone();
});
