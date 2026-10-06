/* Machine checks for the SMART pads page (UI rebuild phase 4).
   Run with: python DevTools/ui_probe.py --checks DevTools/ui_checks_pads.js --route pads
   Same shape as ui_checks_device.js. This is NOT a screen-reader pass: it pins the things a screen
   reader depends on and the eye cannot see (roles, states, one tab stop, what is exposed, what each
   tab reads), so the NVDA run is about how it SOUNDS.

   The checks below are in two halves: the usual tab-strip checks, then the ones that pin this
   page's own design - the behaviour split (Settings all live, Pad Type all deferred behind one
   Apply), the absence of any pending-type mode, and the empty-pad route through to Pad Type. */

const TABS = ["type", "settings", "fx", "maint"];
// "Pad Settings", not "Settings": the app has settings in three places, and a tab announced as
// just "Settings" says nothing about which.
const TITLES = ["Pad Type", "Pad Settings", "Voice effects", "Maintenance"];
// Settings and Pad Type describe the device's pad state, so they re-read it; the other two do not.
const READS = { type: true, settings: true, fx: false, maint: false };
const PADS_API = "/api/soundpads";

const tabId = (k) => "tab-pads-" + k;
const strip = () => document.querySelector("#sec-pads [role='tablist']");
const buttons = () => Array.from(strip().querySelectorAll("[role='tab']"));
const panel = () => document.getElementById("pads-panel");
const panes = () => Array.from(document.querySelectorAll("#pads-panel > .cat-tab"));
const pane = (k) => document.querySelector("#pads-panel > .cat-tab[data-tab-key='" + k + "']");
const selectedKey = () => {
  const b = buttons().find((x) => x.getAttribute("aria-selected") === "true");
  return b ? b.id.replace("tab-pads-", "") : null;
};
const visiblePanes = () => panes().filter((p) => !p.hidden);
const el = (id) => document.getElementById(id);
const key = (ev, target) => target.dispatchEvent(
  new KeyboardEvent("keydown", { key: ev, bubbles: true, cancelable: true }));
const paths = (log) => log.map((e) => e.method + " " + e.path);
const hit = (log, needle) => paths(log).some((p) => p.indexOf(needle) >= 0);

async function waitFor(fn, ms) {
  const until = Date.now() + (ms || 8000);
  while (Date.now() < until) {
    try { if (fn()) return true; } catch (e) { /* not ready */ }
    await sleep(80);
  }
  return false;
}

async function apiLog() {
  const log = await probeLog();
  return log.filter((e) => e.path.indexOf("/api/") === 0 && e.path.indexOf("/api/ping") < 0);
}

async function waitForApi(wanted, ms) {
  const until = Date.now() + (ms || 25000);
  while (Date.now() < until) {
    const seen = (await apiLog()).map((e) => e.path);
    if (wanted.every((w) => seen.some((p) => p.indexOf(w) >= 0))) return true;
    await sleep(120);
  }
  return false;
}

/* Wait for the device traffic to stop, so a "nothing else was read" check cannot pass by looking too
   early. The window is comfortably longer than the 350 ms tab-read debounce. */
async function quiet(ms) {
  let last = (await apiLog()).length;
  let lastChange = Date.now();
  const until = Date.now() + 25000;
  while (Date.now() < until) {
    await sleep(150);
    const n = (await apiLog()).length;
    if (n !== last) { last = n; lastChange = Date.now(); }
    else if (Date.now() - lastChange > (ms || 1400)) return;
  }
}

/* A logged request is not a finished read: /api/soundpads is a ~1.5 s device dump, and a check that
   looked at the page in between would be reading the PREVIOUS pad state. loadPads replaces padsData
   with a fresh object every time, so waiting for that object to change is what proves the read
   landed, not just that it was asked for. */
async function padsRead(before, ms) {
  return waitFor(() => padsData && padsData !== before, ms || 60000);
}

async function openTab(k) {
  const before = padsData;
  el(tabId(k)).click();
  if (READS[k]) {
    await waitForApi([PADS_API]);
    await padsRead(before);
  }
  await quiet();
}

// Put the pad chooser on a given grid position, the way a user would.
function choosePad(grid) {
  const sel = el("pad-ed-select");
  sel.value = String(grid);
  sel.dispatchEvent(new Event("change", { bubbles: true }));
}

const somePad = (want) => (padsData.pads || []).find((p) => !!p.present === want);

async function run() {
  await waitFor(() => !el("sec-pads").hidden && strip(), 20000);
  await waitForApi([PADS_API], 40000);
  check("the page's own entry read lands", await waitFor(() => padsData && padsData.pads, 60000));
  await quiet();

  // ---- structure -------------------------------------------------------
  check("the pads page has a tab strip", !!strip());
  // DELIBERATELY UNNAMED. A tablist's accessible name is announced every time you
  // arrive at the strip, and the panel then repeats the selected tab's name on the way in, so one
  // move along a strip read as four things of which three were furniture. Every strip sits under a
  // heading that already says what page it is.
  check("the strip is a tablist, and deliberately has no name of its own",
    strip().getAttribute("role") === "tablist" && !strip().getAttribute("aria-label"),
    strip().getAttribute("aria-label"));
  check("four tabs, in the agreed order",
    buttons().length === 4 && buttons().every((b, i) => b.id === tabId(TABS[i])),
    buttons().map((b) => b.id).join(","));
  check("the tab titles are the agreed ones",
    buttons().every((b, i) => b.textContent.trim() === TITLES[i]),
    buttons().map((b) => b.textContent.trim()).join(" | "));
  check("the strip sits ABOVE the panel", strip().nextElementSibling === panel());
  check("no collapsible groups left on this page",
    document.querySelectorAll("#sec-pads details.ac-group").length === 0);
  // The panel deliberately does NOT repeat the tab's name: a reader announces it on arriving at
  // the tab, and would announce it again on entering the panel. Once is enough.
  check("the panel is a tabpanel, and does not repeat the tab's name",
    panel().getAttribute("role") === "tabpanel"
    && !panel().getAttribute("aria-labelledby"));
  check("every tab points at the panel it controls",
    buttons().every((b) => b.getAttribute("aria-controls") === "pads-panel"));

  // ---- page context and status stay ABOVE the strip ---------------------
  // The pad chooser decides what every tab acts on, so inside a tab it would mean arrowing back to
  // it, and editing a pad you cannot see from where you are.
  check("the pad chooser is OUTSIDE the panel", !panel().contains(el("pad-ed-select")));
  check("the pad chooser comes BEFORE the strip in reading order",
    el("pad-ed-select").compareDocumentPosition(strip()) & Node.DOCUMENT_POSITION_FOLLOWING);
  check("the pad status line is OUTSIDE the panel, so it survives a tab change",
    !panel().contains(el("pad-ed-status")));
  check("the read-only note and the pool health line stay above the strip",
    !panel().contains(el("pads-note")) && !panel().contains(el("pads-health")));

  // ---- one pane exposed, one tab stop ----------------------------------
  check("exactly one pane is on the page", visiblePanes().length === 1, visiblePanes().length);
  check("the visible pane is the selected tab's",
    visiblePanes()[0].dataset.tabKey === selectedKey());
  check("the strip is ONE tab stop (roving tabindex)",
    buttons().filter((b) => b.tabIndex === 0).length === 1
    && buttons().find((b) => b.tabIndex === 0).getAttribute("aria-selected") === "true");
  check("exactly one tab is selected",
    buttons().filter((b) => b.getAttribute("aria-selected") === "true").length === 1);

  // ---- headings --------------------------------------------------------
  check("every pane opens with its own h2",
    panes().every((p) => p.firstElementChild && p.firstElementChild.tagName === "H2"),
    panes().map((p) => p.firstElementChild && p.firstElementChild.tagName).join(","));
  check("one h2 per pane, never two",
    panes().every((p) => p.querySelectorAll("h2").length === 1));
  check("no heading level is skipped inside a pane",
    panes().every((p) => p.querySelectorAll("h4, h5, h6").length === 0));

  // ---- the two pad tabs NAME the pad in their heading -------------------
  // You can be two tabs away from the chooser, so the panel heading has to carry the context.
  const withContent = somePad(true);
  const empty = somePad(false);
  check("the device offers a pad with content and an empty pad to test with",
    !!withContent && !!empty,
    (withContent ? withContent.grid : "none") + "/" + (empty ? empty.grid : "none"));
  if (withContent) {
    choosePad(withContent.grid);
    const where = "bank " + withContent.bank + ", pad " + withContent.pad;
    check("the Settings heading names the pad, not just the category",
      el("pad-h-settings").textContent.trim() === "Settings for " + where,
      el("pad-h-settings").textContent.trim());
    check("the Pad Type heading names the pad too",
      el("pad-h-type").textContent.trim() === "Change what " + where + " does",
      el("pad-h-type").textContent.trim());
  }

  // ---- keyboard --------------------------------------------------------
  await openTab("type");
  el(tabId("type")).focus();
  key("ArrowRight", document.activeElement);
  check("Right arrow moves to the next tab and takes focus with it",
    selectedKey() === "settings" && document.activeElement.id === tabId("settings"),
    selectedKey() + "/" + document.activeElement.id);
  key("End", document.activeElement);
  check("End jumps to the last tab", selectedKey() === "maint", selectedKey());
  key("Home", document.activeElement);
  check("Home jumps to the first tab", selectedKey() === "type", selectedKey());
  key("ArrowLeft", document.activeElement);
  check("Left arrow from the first tab wraps to the last", selectedKey() === "maint", selectedKey());
  key("ArrowUp", document.activeElement);
  check("Up and down are deliberately unbound on a horizontal strip",
    selectedKey() === "maint", selectedKey());
  check("selection follows the arrows (automatic activation)",
    visiblePanes().length === 1 && visiblePanes()[0].dataset.tabKey === "maint");
  await quiet();

  // ---- focus safety ----------------------------------------------------
  await openTab("maint");
  const tidy = el("tidy-audio-btn");
  tidy.focus();
  check("a control inside the visible pane can hold focus", document.activeElement === tidy);
  el(tabId("fx")).click();
  check("focus is moved to the tab BEFORE its old pane is hidden",
    document.activeElement === el(tabId("fx")),
    document.activeElement.id || document.activeElement.tagName);
  check("the pane that had focus is now hidden", pane("maint").hidden);
  await quiet();

  // ---- THE REDESIGN: the behaviour split -------------------------------
  // Settings is all live and has no Apply; Pad Type is all deferred behind one Apply. That is the
  // whole point of the split, so it is worth failing on if anything drifts back.
  check("Settings holds NO Apply button",
    !pane("settings").contains(el("pad-ed-apply-btn")));
  check("Settings holds NO type dropdown (changing what a pad does is not a live edit)",
    !pane("settings").contains(el("pad-ed-type")));
  check("Pad Type holds the type dropdown, the Apply button and Clear this pad",
    pane("type").contains(el("pad-ed-type"))
    && pane("type").contains(el("pad-ed-apply-btn"))
    && pane("type").contains(el("pad-ed-clear-btn")));
  check("Pad Type holds NONE of the live controls",
    ["pad-ed-volume", "pad-ed-colour", "pad-ed-playmode", "pad-ed-loop", "pad-ed-replay",
     "pad-ed-name", "pad-ed-mixer-trigger"].every((id) => !pane("type").contains(el(id))),
    ["pad-ed-volume", "pad-ed-colour", "pad-ed-playmode", "pad-ed-loop", "pad-ed-replay",
     "pad-ed-name", "pad-ed-mixer-trigger"].filter((id) => pane("type").contains(el(id)))
      .join(" "));
  check("Apply is the last control before Clear this pad",
    el("pad-ed-apply-btn").compareDocumentPosition(el("pad-ed-clear-btn"))
      & Node.DOCUMENT_POSITION_FOLLOWING);
  check("the live settings and the deferred controls are in DIFFERENT panes",
    pane("settings").contains(el("pad-ed-settings"))
    && !pane("settings").contains(el("pad-ed-type")));

  // ---- there is NO pending-type mode ------------------------------------
  // Such a mode hides a pad's own settings behind a note while a type change is staged. With the
  // deferred controls on their own tab there is nothing to hide, so neither the note nor the mode
  // exists.
  check("the pending-type note is gone from the page",
    el("pad-ed-settings-pending") === null);
  if (withContent) {
    choosePad(withContent.grid);
    await openTab("type");
    const typeSel = el("pad-ed-type");
    const other = [...typeSel.options].map((o) => o.value)
      .find((v) => v !== typeSel.value && v !== "empty");
    typeSel.value = other;
    typeSel.dispatchEvent(new Event("change", { bubbles: true }));
    check("staging a type change shows that type's own options",
      !el("pad-ed-grp-" + other).hidden && el("pad-ed-apply-row").hidden === false,
      other);
    check("staging a type change does NOT hide the pad's live settings any more",
      !el("pad-ed-settings").hidden);
    check("and the pad chooser still reports what the pad IS, not what is staged",
      el("pad-ed-type-note").textContent.indexOf("Currently:") === 0,
      el("pad-ed-type-note").textContent);
    // Put the dropdown back so later checks start from the pad's real type.
    choosePad(withContent.grid);
  }

  // ---- an empty pad's Settings tab --------------------------------------
  if (empty) {
    await openTab("settings");
    choosePad(empty.grid);
    check("an empty pad's Settings tab says so instead of showing dead controls",
      !el("pad-ed-empty").hidden && el("pad-ed-settings").hidden);
    check("the live-settings help is hidden with them, so it cannot describe nothing",
      el("pad-ed-settings-help").hidden);
    // Unavailable AND still in the tab order, with the reason attached: a natively disabled button
    // vanishes as you move between pads and takes its explanation with it.
    check("Clear this pad is unavailable on an empty pad",
      el("pad-ed-clear-btn").getAttribute("aria-disabled") === "true");
    check("and it keeps its place in the tab order, with the reason",
      !el("pad-ed-clear-btn").disabled && !!el("pad-ed-clear-btn").dataset.offReason,
      el("pad-ed-clear-btn").dataset.offReason);
    el("pad-ed-goto-type").click();
    check("the empty pad's button moves you to Pad Type",
      selectedKey() === "type", selectedKey());
    check("and focus lands on the Pad Type TAB, which announces itself",
      document.activeElement === el(tabId("type")),
      document.activeElement.id || document.activeElement.tagName);
    await quiet();
  }
  if (withContent) {
    await openTab("settings");
    choosePad(withContent.grid);
    check("a pad with content shows its settings and not the empty note",
      !el("pad-ed-settings").hidden && el("pad-ed-empty").hidden);
  }

  /* ---- a MIDI trigger pad's mapping controls ---------------------------
     What a trigger pad SENDS is readable and writable, so the Settings tab carries a fieldset
     built from the server's spec. SKIPPED, and named as skipped, when the grid holds no trigger
     pad: a silent pass would report a page nothing exercised as green. */
  const trigPad = (padsData.pads || []).find((p) => p.present && p.type === 4);
  if (!trigPad) {
    check("SKIPPED the MIDI trigger checks: no trigger pad on the grid", true, "none present");
  } else {
    await openTab("settings");
    choosePad(trigPad.grid);
    const spec = padsData.trigger_edit || {};
    check("a trigger pad shows the mapping fieldset", !el("pad-ed-trigger-row").hidden);
    check("the fieldset is on the SETTINGS tab (these are live edits)",
      pane("settings").contains(el("pad-ed-trigger-row")));
    check("one control per field in the server's spec, by key",
      (spec.fields || []).every((f) => !!el("pad-ed-trig-" + f.key)),
      (spec.fields || []).map((f) => f.key).join(","));
    check("a field with choices is a dropdown, a numeric field is a number box",
      (spec.fields || []).every((f) => {
        const c = el("pad-ed-trig-" + f.key);
        return f.choices ? c.tagName === "SELECT"
                         : (c.tagName === "INPUT" && c.type === "number"
                            && c.min === String(f.min) && c.max === String(f.max));
      }));
    check("every field has a label and its own help text",
      (spec.fields || []).every((f) => {
        const c = el("pad-ed-trig-" + f.key);
        const lab = document.querySelector("label[for='pad-ed-trig-" + f.key + "']");
        const help = el("pad-ed-trig-" + f.key + "-help");
        return lab && lab.textContent.trim() && help && help.textContent.trim()
               && (c.getAttribute("aria-describedby") || "").indexOf(help.id) >= 0;
      }));
    check("the values shown are the ones read off the pad",
      (spec.fields || []).every((f) => trigPad.trigger[f.key] === null
        || el("pad-ed-trig-" + f.key).value === String(trigPad.trigger[f.key])),
      JSON.stringify(trigPad.trigger));
    check("the custom-mapping switch reflects the pad",
      el("pad-ed-trigger-custom").checked === (trigPad.trigger.custom === true));
    // The firmware only honours the numbers behind the custom flag, so with it off they must be
    // announced as unavailable rather than accepting a value that would do nothing.
    const shouldBeOff = trigPad.trigger.custom !== true;
    check("the mapping fields follow the custom flag (unavailable when it is off)",
      (spec.fields || []).every((f) =>
        (el("pad-ed-trig-" + f.key).getAttribute("aria-disabled") === "true") === shouldBeOff),
      "custom=" + trigPad.trigger.custom);
    // aria-disabled, not the attribute: these are fields somebody arrows through while setting a
    // pad up, and a natively disabled one is skipped with nothing said.
    check("and they keep their place in the tab order either way",
      (spec.fields || []).every((f) => !el("pad-ed-trig-" + f.key).disabled));
    check("the pad says in plain words what it sends",
      (el("pad-ed-trigger-current").textContent || "").length > 20,
      el("pad-ed-trigger-current").textContent);
    // Structure once, values every time: this fieldset is rebuilt by every read otherwise.
    const th = el("pad-ed-trigger-fields");
    const thFirst = th.firstElementChild;
    await loadPads();
    check("re-reading keeps the trigger fields' own nodes", th.firstElementChild === thFirst);
    // A pad that is NOT a trigger must not show any of it.
    if (withContent && withContent.type !== 4) {
      choosePad(withContent.grid);
      check("a pad that is not a MIDI trigger hides the mapping fieldset",
        el("pad-ed-trigger-row").hidden);
    }
  }

  /* ---- trim and fade, fade times, censor sound -------------------------
     The last per-pad settings a profile dropped, exposed. Each group must appear ONLY on
     the pad type that owns it: a control that cannot do anything is worse than no control, because
     it reads as available. */
  const soundPad = (padsData.pads || []).find((p) => p.present && p.type === 1);
  const censorPad = (padsData.pads || []).find((p) => p.present && p.censor);
  const fadePad = (padsData.pads || []).find((p) => p.present && p.fade);
  await openTab("settings");
  if (!soundPad) {
    check("SKIPPED the trim and fade checks: no sound pad on the grid", true, "none present");
  } else {
    choosePad(soundPad.grid);
    const env = (padsData.extra_edit || {}).envelope || [];
    check("a sound pad shows the trim and fade fieldset", !el("pad-ed-env-row").hidden);
    check("four envelope points, from the server's spec", env.length === 4
      && env.every((f) => !!el("pad-ed-env-fields-" + f.key)),
      env.map((f) => f.key).join(","));
    check("each point is a percent slider with a label and its own help",
      env.every((f) => {
        const c = el("pad-ed-env-fields-" + f.key);
        const lab = document.querySelector("label[for='pad-ed-env-fields-" + f.key + "']");
        return c.tagName === "INPUT" && c.type === "range" && c.min === "0" && c.max === "100"
               && lab && lab.textContent.trim() && el("pad-ed-env-fields-" + f.key + "-help");
      }));
    check("the slider reads out its unit, so no extra live region is needed",
      env.every((f) => /percent$/.test(
        el("pad-ed-env-fields-" + f.key).getAttribute("aria-valuetext") || "")),
      el("pad-ed-env-fields-start").getAttribute("aria-valuetext"));
    check("the values shown are the pad's own, converted to percent",
      env.every((f) => el("pad-ed-env-fields-" + f.key).value
                       === String(Math.round(soundPad.envelope[f.key] * 100))),
      JSON.stringify(soundPad.envelope));
    // A fade point outside the played part does nothing at all, which is invisible; the page says so.
    check("a factory pad with no fades says so rather than staying quiet",
      (el("pad-ed-env-note").textContent || "").indexOf("no fade") >= 0,
      el("pad-ed-env-note").textContent);
    const eh = el("pad-ed-env-fields");
    const ehFirst = eh.firstElementChild;
    await loadPads();
    check("re-reading keeps the envelope sliders' own nodes", eh.firstElementChild === ehFirst);
  }
  if (!censorPad) {
    check("SKIPPED the censor checks: no Censor pad on the grid", true, "none present");
  } else {
    choosePad(censorPad.grid);
    check("a Censor pad shows the censor fieldset", !el("pad-ed-censor-row").hidden);
    check("the switch reflects the pad", el("pad-ed-censor-custom").checked
      === (censorPad.censor.custom === true));
    check("and it says whether a sound of your own is on the unit at all",
      (el("pad-ed-censor-file").textContent || "").length > 20,
      el("pad-ed-censor-file").textContent);
  }
  check(fadePad ? "a Fade pad shows the fade-times fieldset"
                : "SKIPPED the fade-time checks: no Fade pad on the grid",
    fadePad ? !el("pad-ed-fade-row").hidden : true, fadePad ? fadePad.grid : "none present");
  if (soundPad) {
    choosePad(soundPad.grid);
    check("a sound pad shows NONE of the mixer or MIDI groups",
      el("pad-ed-fade-row").hidden && el("pad-ed-censor-row").hidden
      && el("pad-ed-trigger-row").hidden && el("pad-ed-mixer-trigger-row").hidden);
  }
  if (censorPad) {
    choosePad(censorPad.grid);
    check("a mixer pad shows no trim and fade (it plays no file)", el("pad-ed-env-row").hidden);
  }

  // ---- the chosen pad does not reset the tab ----------------------------
  await openTab("maint");
  if (withContent) choosePad(withContent.grid);
  check("changing pad leaves you on the tab you were on",
    selectedKey() === "maint", selectedKey());

  // ---- every pane has focusable content, so the panel adds no tab stop --
  // (A pane with nothing focusable needs tabindex="0" on the panel; none of these are that case,
  // unless the desk is read-only and every pad control is disabled.)
  if (!padsReadOnly) {
    for (const k of TABS) {
      await openTab(k);
      check(k + " has controls, so the panel adds no extra tab stop",
        !panel().hasAttribute("tabindex"), panel().getAttribute("tabindex"));
    }
  } else {
    check("SKIPPED the panel-tab-stop checks: this desk is read-only", true, "read_only");
  }

  // ---- what each tab READS ---------------------------------------------
  for (const k of TABS) {
    await openTab("maint");          // a tab that reads nothing, so the next click is a real change
    await probeClear();
    el(tabId(k)).click();
    if (READS[k]) {
      check("opening " + k + " re-reads the pads", await waitForApi([PADS_API]));
    } else {
      await quiet();
      check("opening " + k + " reads NOTHING from the device",
        !hit(await probeLog(), PADS_API),
        paths(await apiLog()).join(" "));
    }
    await quiet();
  }

  // ---- the reads are debounced ----------------------------------------
  await openTab("maint");
  el(tabId("maint")).focus();
  await probeClear();
  for (let i = 0; i < 6; i++) { key("ArrowLeft", document.activeElement); await sleep(40); }
  check("the tab it ended on reads", await waitForApi([PADS_API]));
  await quiet();
  const calls = (await apiLog()).map((e) => e.method + " " + e.path);
  check("arrowing across the strip fires ONE pad read, not one per tab",
    calls.length === 1, calls.join(" "));

  // ---- a re-read must not rebuild what it is reading into ---------------
  // Structure once, values every time: this read runs while the pane is on screen, so replacing a
  // control would speak over a screen reader and move it off what it was on.
  await openTab("settings");
  const chooser = el("pad-ed-select");
  const firstOpt = chooser.firstElementChild;
  const optCount = chooser.children.length;
  const bcBox = el("pad-ed-backchannel-list");
  const bcFirst = bcBox.firstElementChild;
  const fxList = el("fx-custom-list");
  const fxFirst = fxList.firstElementChild;
  const colour = el("pad-ed-colour");
  const colourFirst = colour.firstElementChild;
  await loadPads();
  check("re-reading keeps the pad chooser's own option nodes",
    chooser.firstElementChild === firstOpt && chooser.children.length === optCount,
    optCount + " -> " + chooser.children.length);
  check("re-reading keeps the colour dropdown's option nodes",
    colour.firstElementChild === colourFirst);
  check("re-reading keeps the Back Channel checkboxes",
    bcBox.firstElementChild === bcFirst);
  check("re-reading keeps the saved-effects list",
    fxList.firstElementChild === fxFirst);

  // ---- a Settings dropdown applies on Enter, not on a pause ---------------
  // A closed select fires `change` on every arrow press, so an arrowed choice waits for Enter, and
  // leaving puts it back; a mouse pick has no browsing key behind it and writes at once.
  const COLOUR_API = "/api/pad/colour";
  const arrowTo = (value) => {
    key("ArrowDown", colour);
    colour.value = value;
    colour.dispatchEvent(new Event("change", { bubbles: true }));
  };
  check("a present pad is chosen for the dropdown checks", !!(currentPad() && currentPad().present));
  const startColour = colour.value;
  const otherColour = Array.from(colour.options)
    .map((o) => o.value).find((v) => v !== "" && v !== startColour);
  await probeClear();
  arrowTo(otherColour);
  await sleep(1500);
  check("an arrowed colour writes nothing while you listen", !hit(await apiLog(), COLOUR_API));
  const enter = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
  colour.dispatchEvent(enter);
  check("Enter writes the arrowed colour", await waitForApi([COLOUR_API], 5000));
  check("and does not open the list on top", enter.defaultPrevented);
  await quiet();
  await probeClear();
  arrowTo(startColour);
  colour.dispatchEvent(new FocusEvent("blur"));
  check("leaving the dropdown puts an arrowed colour back", colour.value === otherColour,
        colour.value);
  await sleep(800);
  check("and writes nothing", !hit(await apiLog(), COLOUR_API));
  await probeClear();
  colour.dispatchEvent(new Event("pointerdown"));
  colour.value = startColour;
  colour.dispatchEvent(new Event("change", { bubbles: true }));
  check("a mouse pick writes at once", await waitForApi([COLOUR_API], 5000));
  await quiet();
  await loadPads();

  // ---- coming back to the page re-reads --------------------------------
  await openTab("settings");
  const beforeEntry = padsData;
  await probeClear();
  location.hash = "#/status";
  await sleep(600);
  location.hash = "#/pads";
  await waitFor(() => !el("sec-pads").hidden);
  check("re-entering the page re-reads the pads", await waitForApi([PADS_API]));
  await padsRead(beforeEntry);   // let the read land before counting what was asked for
  await quiet();
  const entry = (await apiLog()).filter((e) => e.path.indexOf(PADS_API) >= 0);
  check("entry reads the pads ONCE, and the arrived tab does not read again on top",
    entry.length === 1, entry.length);

  // ---- remembered tab, and the retired group state ----------------------
  await openTab("fx");
  let stored = null;
  try { stored = JSON.parse(window.localStorage.getItem("accesscaster.tabs") || "{}"); } catch (e) {}
  check("the selected tab is remembered per page, by key",
    stored && stored.pads === "fx", stored && stored.pads);
  // Every page is on tabs now, so the WHOLE collapsible-group store goes rather than one prefix at
  // a time.
  window.localStorage.setItem("accesscaster.groups",
    JSON.stringify({ "pad.edit": false, "about.help": true }));
  dropRetiredGroupState();
  check("the retired collapsible-group state is dropped wholesale",
    window.localStorage.getItem("accesscaster.groups") === null,
    window.localStorage.getItem("accesscaster.groups"));

  // ---- nothing was written --------------------------------------------
  const HOUSEKEEPING = ["/api/ping", "/api/focus", "/api/quit"];
  const isWrite = (e) => e.method === "POST" && e.path.indexOf("/api/") === 0
                         && HOUSEKEEPING.indexOf(e.path) < 0;
  const writes = (await probeLog()).filter(isWrite);
  check("nothing up to here tried to write to the device", writes.length === 0,
    writes.map((e) => e.path).join(" "));

  /* ---- LAST, because it is the one check that presses a write button ----
     A successful Apply is supposed to leave Pad Type for Settings, since volume and looping are
     nearly always what you want next. The probe STUBS every POST, so this exercises the page's
     behaviour with nothing reaching the desk: an EMPTY pad needs no confirm dialog, and a MIDI
     trigger needs no file, so Apply can be pressed straight off. */
  if (empty) {
    await openTab("settings");
    choosePad(empty.grid);
    await openTab("type");
    const typeSel = el("pad-ed-type");
    typeSel.value = "trigger";
    typeSel.dispatchEvent(new Event("change", { bubbles: true }));
    check("Apply is on offer for a real type", !el("pad-ed-apply-row").hidden);
    const before = padsData;
    el("pad-ed-apply-btn").click();
    check("a successful Apply moves you to the Settings tab",
      await waitFor(() => selectedKey() === "settings", 60000), selectedKey());
    check("and focus goes with it, onto the Settings tab",
      document.activeElement === el(tabId("settings")),
      document.activeElement.id || document.activeElement.tagName);
    check("Apply re-read the pads before showing you the settings",
      await padsRead(before));
    await quiet();
    const after = (await probeLog()).filter(isWrite);
    check("the only write in the whole run was that one stubbed Apply",
      after.length === 1 && after[0].path.indexOf("/api/pad/create-special") === 0,
      after.map((e) => e.path).join(" "));
  }

  // ---- Maintenance tab: the two jobs added. --------------------------------------
  // Restore the factory voice effects was decoded long ago and never wired up, and exporting a
  // pad's sound is new. Both live under their own h3 on Maintenance, and what is worth pinning is
  // not that the buttons exist but that they are told apart from the ones beside them: two
  // "restore" buttons that destroy different amounts of the grid is exactly the pair a screen
  // reader user must not confuse.
  el("tab-pads-maint").click();
  await waitFor(() => selectedKey() === "maint", 8000);
  await quiet(400);
  const maintPane = pane("maint");
  check("the Maintenance tab has all four jobs, in order",
    Array.from(maintPane.querySelectorAll("h3")).map((h) => h.textContent.trim()).join(" | ")
      === "Clear all pads | Restore the factory voice effects, Censor and Ducking | "
        + "Export a pad's sound to your computer | Tidy up unused pad audio",
    Array.from(maintPane.querySelectorAll("h3")).map((h) => h.textContent.trim()).join(" | "));
  ["restore-fx-btn", "export-pad-btn", "open-exports-btn"].forEach((id) => {
    check("the " + id + " button is present and keyboard reachable",
      !!el(id) && el(id).tagName === "BUTTON" && el(id).tabIndex >= 0);
  });
  const fxHelp = el(el("restore-fx-btn").getAttribute("aria-describedby"));
  check("the voice-effect restore says how MUCH it destroys, so it is not read as the whole grid",
    !!fxHelp && /eight pads/.test(fxHelp.textContent),
    fxHelp && fxHelp.textContent);
  check("and it says how long it takes, because two minutes of silence reads as a hang",
    !!fxHelp && /minute/.test(fxHelp.textContent));
  const exHelp = el(el("export-pad-btn").getAttribute("aria-describedby"));
  check("the export says WHERE the file lands, which is the whole point of a fixed folder",
    !!exHelp && /Documents/.test(exHelp.textContent) && /Pad exports/.test(exHelp.textContent),
    exHelp && exHelp.textContent);
  check("and that only a sound pad has a file to export",
    !!exHelp && /Sound pads only/.test(exHelp.textContent));
  check("both status lines are polite live regions, not assertive ones",
    ["restore-fx-status", "export-pad-status"].every((id) =>
      el(id) && el(id).getAttribute("aria-live") === "polite"));

  await probeDone();
}

run().catch(async (e) => {
  check("the checks ran to the end", false, String((e && e.stack) || e));
  await probeDone();
});
