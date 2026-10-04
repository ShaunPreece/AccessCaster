/* Machine checks for the Routing page's tab strip (UI rebuild phase 2).
   Run with: python DevTools/ui_probe.py --checks DevTools/ui_checks_routing.js --route routing
   This is NOT a screen-reader pass. It pins the things a screen reader depends on and the eye cannot
   see (roles, states, one tab stop, what is exposed, what a tab reads from the device), so Shaun's
   NVDA run is about how it SOUNDS rather than hunting for a broken attribute. */

const TABS = ["mixes", "faders", "levels", "master"];
// TITLES AND KEYS DIVERGE ON PURPOSE, so that a remembered tab position still lands: the last
// tab's KEY is "recording" though it is titled for auto-mute, the multitrack setting having its
// home on the Recording page, and "master" covers Master Mix including its USB audio SECTION. USB
// audio is not a tab of its own - it and the master mix are both the main mix on its way out, and
// one tab fewer is one fewer arrow press to everything past it.
const TITLES = ["Output mixes", "Faders", "Levels", "Master Mix"];
const tabId = (k) => "tab-routing-" + k;
const strip = () => document.querySelector("#sec-routing [role='tablist']");
const buttons = () => Array.from(strip().querySelectorAll("[role='tab']"));
const panes = () => Array.from(document.querySelectorAll("#routing-panel > .cat-tab"));
const selectedKey = () => {
  const b = buttons().find((x) => x.getAttribute("aria-selected") === "true");
  return b ? b.id.replace("tab-routing-", "") : null;
};
const visiblePanes = () => panes().filter((p) => !p.hidden);
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

/* What each tab reads from the device when it is opened. The reads are real device dumps behind a
   debounce, so nothing here may assume a fixed delay: wait for the read to actually land, then wait
   for the traffic to go quiet, and only then assert what did NOT happen. */
const TAB_READS = {
  // Output mixes reads the live matrix AND the saved mix schemes (added), so a
  // scheme saved in another window is in the list when you come back to this one.
  mixes: ["/api/routing/matrix/live", "/api/routing/schemes"],
  faders: ["/api/routing/live"],
  levels: ["/api/faders/levels", "/api/monitormix/live", "/api/routing/live"],
  master: ["/api/routing/live"],
};

async function apiLog() {
  const log = await probeLog();
  return log.filter((e) => e.path.indexOf("/api/") === 0 && e.path.indexOf("/api/ping") < 0);
}

/* Wait until every one of these endpoints has been asked for. Returns false on timeout, which is
   what makes "opening this tab reads that" a real check rather than a sleep. */
async function waitForApi(wanted, ms) {
  const until = Date.now() + (ms || 20000);
  while (Date.now() < until) {
    const seen = (await apiLog()).map((e) => e.path);
    if (wanted.every((w) => seen.some((p) => p.indexOf(w) >= 0))) return true;
    await sleep(120);
  }
  return false;
}

/* Wait for the device traffic to stop, so a "nothing else was read" check cannot pass simply by
   looking too early. */
async function quiet(ms) {
  let last = (await apiLog()).length;
  let lastChange = Date.now();
  const until = Date.now() + 15000;
  while (Date.now() < until) {
    await sleep(150);
    const n = (await apiLog()).length;
    if (n !== last) { last = n; lastChange = Date.now(); }
    else if (Date.now() - lastChange > (ms || 1200)) return;
  }
}

/* Open a tab the way a mouse user does, and do not come back until its reads have landed. */
async function openTab(k) {
  document.getElementById(tabId(k)).click();
  await waitForApi(TAB_READS[k]);
  await quiet();
}

async function run() {
  // The driver asks for #/routing and the app opens on Channels, so wait for the page and its model
  // rather than assuming a fixed delay.
  await waitFor(() => !document.getElementById("sec-routing").hidden && strip());
  await waitFor(() => document.querySelectorAll("#routing-inputs select").length > 0, 15000);

  // ---- structure -------------------------------------------------------
  check("the routing page has a tab strip", !!strip());
  // DELIBERATELY UNNAMED. A tablist's accessible name is announced every time you
  // arrive at the strip, and the panel then repeats the selected tab's name on the way in, so one
  // move along a strip read as four things of which three were furniture. Every strip sits under a
  // heading that already says what page it is.
  check("the strip is a tablist, and deliberately has no name of its own",
    strip().getAttribute("role") === "tablist" && !strip().getAttribute("aria-label"),
    strip().getAttribute("aria-label"));
  check("four tabs, in the agreed order",
    buttons().length === TABS.length && buttons().every((b, i) => b.id === tabId(TABS[i])),
    buttons().map((b) => b.id).join(","));
  check("the tab titles are the agreed ones",
    buttons().every((b, i) => b.textContent.trim() === TITLES[i]),
    buttons().map((b) => b.textContent.trim()).join(" | "));
  check("the strip sits ABOVE the panel",
    strip().nextElementSibling === document.getElementById("routing-panel"));
  check("no collapsible groups left on this page",
    document.querySelectorAll("#sec-routing details.ac-group").length === 0);
  check("the page status line stays OUTSIDE the panel",
    !document.getElementById("routing-panel").contains(document.getElementById("routing-status")));
  // The panel is deliberately NOT labelled by the selected tab either: a reader announces the tab
  // name as you arrive on the tab, and again as you enter the panel. Once is enough.
  check("the panel is a tabpanel, and does not repeat the tab's name",
    document.getElementById("routing-panel").getAttribute("role") === "tabpanel"
    && !document.getElementById("routing-panel").getAttribute("aria-labelledby"));
  check("every tab points at the panel it controls",
    buttons().every((b) => b.getAttribute("aria-controls") === "routing-panel"));
  check("every pane here has controls, so the panel adds no extra tab stop",
    !document.getElementById("routing-panel").hasAttribute("tabindex"),
    document.getElementById("routing-panel").getAttribute("tabindex"));

  // ---- one pane exposed, one tab stop ----------------------------------
  check("exactly one pane is on the page", visiblePanes().length === 1, visiblePanes().length);
  check("the visible pane is the selected tab's",
    visiblePanes()[0].dataset.tabKey === selectedKey());
  check("the strip is ONE tab stop (roving tabindex)",
    buttons().filter((b) => b.tabIndex === 0).length === 1
    && buttons().find((b) => b.tabIndex === 0).getAttribute("aria-selected") === "true");
  check("exactly one tab is selected",
    buttons().filter((b) => b.getAttribute("aria-selected") === "true").length === 1);

  // ---- headings and sub-groups ----------------------------------------
  await openTab("mixes");
  const mixPane = document.querySelector("#routing-panel > .cat-tab[data-tab-key='mixes']");
  check("the pane opens with its own h2",
    mixPane.firstElementChild.tagName === "H2"
    && mixPane.firstElementChild.textContent.trim() === "Output mixes",
    mixPane.firstElementChild.tagName);
  check("one h2 per pane, never two",
    panes().every((p) => p.querySelectorAll("h2").length === 1));
  check("no heading level is skipped inside a pane",
    panes().every((p) => p.querySelectorAll("h4, h5, h6").length === 0));
  const mergedH3 = Array.from(mixPane.querySelectorAll("h3")).map((h) => h.textContent.trim());
  check("the merged sections keep their h3 headings",
    // "Mix presets" on the page, "schemes" in the code: the user-facing name and the internal one
    // differ on purpose, so assert the PAGE's wording here.
    mergedH3.join(" | ") === "Mix presets | Reset all output mixes", mergedH3.join(" | "));
  // NO NAMED GROUPS ANY MORE. A role="group" with a name is announced on the way in,
  // before the control you were heading for, and again as "out of grouping" on the way out. The
  // h3 above each section says the same thing, is reachable with H, and costs nothing when you are
  // not looking for it. What is pinned now is that the HEADINGS are still there, since they are
  // what carries the structure.
  check("no named groups are left on this pane",
    mixPane.querySelectorAll("[role='group']").length === 0,
    mixPane.querySelectorAll("[role='group']").length);

  // ---- mix schemes ----------------------------------
  // The two Chat preset buttons became built-in schemes, so what is pinned here is that the ONE
  // control that replaced them is properly labelled, says what it would do before you commit, and
  // reports through a live region rather than silently.
  check("the Chat preset buttons are gone, replaced by the scheme control",
    !document.getElementById("chat-mic-only-btn") && !document.getElementById("chat-full-btn"));
  const schemeSel = document.getElementById("scheme-select");
  check("the scheme dropdown has a real <label>",
    !!document.querySelector("label[for='scheme-select']"));
  // The dropdown must NOT be described by the line that changes with the selection: changing a
  // focused control's description makes a reader speak the description on its own, so arriving back
  // at the dropdown read the hint instead of the scheme. The BUTTONS carry it instead.
  check("the dropdown is not described by the line that changes under it",
    !schemeSel.getAttribute("aria-describedby"));
  check("but the buttons that act on the selection are",
    ["scheme-apply-btn", "scheme-delete-btn"].every((id) =>
      document.getElementById(id).getAttribute("aria-describedby") === "scheme-description"));
  check("the name box has a real <label> too",
    !!document.querySelector("label[for='scheme-name']"));
  // NOT a live region: this section speaks through announce() alone. The same words landing in two
  // polite regions at once leaves the outcome unspoken, so Apply is heard as the button label
  // again and nothing else.
  check("the scheme status line is NOT a second live region",
    (() => {
      const st = document.getElementById("scheme-status");
      return st && !st.getAttribute("role") && !st.getAttribute("aria-live");
    })());
  check("the built-ins the server derives are all in the list",
    ["builtin:normal", "builtin:chat-mic-only", "builtin:chat-normal"]
      .every((v) => Array.from(schemeSel.options).some((o) => o.value === v)),
    Array.from(schemeSel.options).map((o) => o.value).join(","));
  check("a saved scheme is listed under its own name space, so the two cannot collide",
    Array.from(schemeSel.options).every((o) => /^(builtin|saved):/.test(o.value)),
    Array.from(schemeSel.options).map((o) => o.value).join(","));
  // EVERY OPTION SAYS ITS SCOPE. Michael could not tell that the Chat
  // presets leave other outputs alone; the description says so but hangs off Apply, which is read
  // after the choice. This is the one piece of information needed BEFORE choosing.
  check("every option says how much of the desk it would rewrite",
    Array.from(schemeSel.options).every((o) => /(every output|only)\)$/.test(o.text)),
    Array.from(schemeSel.options).map((o) => o.text).join(" | "));
  check("and the Chat presets are marked as touching Chat alone",
    Array.from(schemeSel.options)
      .filter((o) => /^builtin:chat-/.test(o.value))
      .every((o) => /Chat only/.test(o.text)),
    Array.from(schemeSel.options).map((o) => o.text).join(" | "));
  // The one-output save NAMES the output chosen above it, so its label alone says what it does and
  // there is no second control whose state has to be checked first.
  check("the one-output save button names the chosen output",
    (document.getElementById("scheme-save-output-btn") || {}).textContent
      .indexOf(currentMatrixOutput().label) !== -1,
    (document.getElementById("scheme-save-output-btn") || {}).textContent);
  schemeSel.value = "builtin:chat-mic-only";
  schemeSel.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(120);
  check("choosing a scheme says what it would do",
    /Chat/.test(document.getElementById("scheme-description").textContent),
    document.getElementById("scheme-description").textContent);
  // aria-disabled, and STILL IN THE TAB ORDER with a reason attached. A natively disabled button
  // vanishes and reappears as you arrow the list, so it is not heard as unavailable at all - it is
  // simply skipped over, with nothing to say why.
  const delSchemeBtn = document.getElementById("scheme-delete-btn");
  check("Delete is unavailable for a built-in, and says why",
    delSchemeBtn.getAttribute("aria-disabled") === "true" && !!delSchemeBtn.dataset.offReason,
    delSchemeBtn.dataset.offReason);
  check("but it keeps its place in the tab order", !delSchemeBtn.disabled);
  await openTab("levels");
  const lvlPane = document.querySelector("#routing-panel > .cat-tab[data-tab-key='levels']");
  // The output mutes joined this tab on (outputMonMute / outputBTMute / outputBTLevel).
  // The order is asserted, not just the membership: the sliders come first, then the mutes, then
  // the per-output mixes, so the tab reads from "what level is it at" to "is it on at all".
  // Auto-mute joined this tab on and sits DIRECTLY AFTER the manual mutes: it is
  // the automatic version of them, so everything that silences an output reads as one group.
  check("Levels merges the mutes, auto-mute, the monitor mix and the headphones under h3s",
    Array.from(lvlPane.querySelectorAll("h3")).map((h) => h.textContent.trim()).join(" | ")
      === "Output mutes and Bluetooth level | Auto-mute | Monitor mix levels | Headphones",
    Array.from(lvlPane.querySelectorAll("h3")).map((h) => h.textContent.trim()).join(" | "));
  check("no named groups on Levels either",
    lvlPane.querySelectorAll("[role='group']").length === 0,
    lvlPane.querySelectorAll("[role='group']").length);
  // The output mutes must be filled by the tab's own read, not left as unticked switches that
  // silently claim nothing is muted. A mute the unit never reported is the one state worth catching.
  check("the output mute switches exist and are real checkboxes",
    ["out-mute-monitor", "out-mute-bluetooth"].every((id) => {
      const el = document.getElementById(id);
      return el && el.type === "checkbox";
    }));
  check("the Bluetooth OUTPUT level slider is present and reads a real value",
    (() => {
      const el = document.getElementById("bt-out-level");
      return el && el.type === "range" && el.getAttribute("aria-valuetext") !== "not read yet";
    })(),
    (document.getElementById("bt-out-level") || {}).getAttribute
      && document.getElementById("bt-out-level").getAttribute("aria-valuetext"));
  check("its label says OUTPUT, so it cannot be read as the Bluetooth input channel",
    (() => {
      const el = document.getElementById("bt-out-level");
      const lab = el && (el.closest("label") || document.querySelector("label[for='bt-out-level']"));
      return !!lab && /output/i.test(lab.textContent);
    })());
  check("a hidden pane's controls are not rendered at all",
    document.getElementById("matrix-output").offsetParent === null
    && document.getElementById("monmix-output").offsetParent !== null);

  // ---- keyboard --------------------------------------------------------
  const first = document.getElementById(tabId("mixes"));
  first.focus();
  first.click();
  await sleep(450);
  key("ArrowRight", document.activeElement);
  check("Right arrow moves to the next tab and takes focus with it",
    selectedKey() === "faders" && document.activeElement.id === tabId("faders"),
    selectedKey() + "/" + document.activeElement.id);
  key("End", document.activeElement);
  check("End jumps to the last tab", selectedKey() === "master", selectedKey());
  key("Home", document.activeElement);
  check("Home jumps to the first tab", selectedKey() === "mixes", selectedKey());
  key("ArrowLeft", document.activeElement);
  check("Left arrow from the first tab wraps to the last", selectedKey() === "master", selectedKey());
  key("ArrowDown", document.activeElement);
  check("Up and down are deliberately unbound on a horizontal strip",
    selectedKey() === "master", selectedKey());
  check("selection follows the arrows (automatic activation)",
    visiblePanes().length === 1 && visiblePanes()[0].dataset.tabKey === "master");
  await waitForApi(TAB_READS.master);
  await quiet();

  // ---- FADER ASSIGNMENT STAGES, IT DOES NOT WRITE ---------
  // The fault this replaced: every dropdown wrote on `change`, a closed select fires change on
  // EVERY arrow press, and arrowing a closed select is how a screen reader reads one - so browsing
  // the list performed the swap. These checks are the guard against it coming back.
  await openTab("faders");
  await probeClear();
  const fSel = document.querySelector("#routing-inputs select");
  const fBtn = document.getElementById(fSel.id + "-apply");
  check("every fader has an Apply button of its own", !!fBtn, fSel.id);
  check("and it NAMES its fader, so nine of them are still tellable apart",
    /apply to fader/i.test(fBtn.textContent), fBtn.textContent);
  check("with nothing changed, Apply is marked unavailable rather than being a no-op",
    fBtn.getAttribute("aria-disabled") === "true");
  check("and there is no hint repeating what the dropdown already says",
    (document.getElementById(fSel.id + "-now").textContent || "").trim() === "");
  // Move the dropdown the way an arrow press does, and check NOTHING went to the unit.
  const other = [...fSel.options].find((o) => o.value !== fSel.value);
  fSel.value = other.value;
  fSel.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(1200);                     // longer than the old commit delay
  check("changing the dropdown writes NOTHING to the unit",
    !(await apiLog()).some((e) => e.method === "POST" && e.path.indexOf("/api/faders/set") === 0),
    paths(await apiLog()).join(" "));
  check("Apply becomes available once something is staged",
    fBtn.getAttribute("aria-disabled") !== "true");
  check("and the hint now says what the unit still has",
    /still has/i.test(document.getElementById(fSel.id + "-now").textContent),
    document.getElementById(fSel.id + "-now").textContent);
  check("but the hint is NOT wired as a description, or it would be spoken between every option",
    (fSel.getAttribute("aria-describedby") || "").indexOf(fSel.id + "-now") < 0,
    fSel.getAttribute("aria-describedby"));
  // Put it back the way it was; the probe stubs POSTs anyway, so nothing here reaches the desk.
  fSel.value = fSel.dataset.deviceValue;
  fSel.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(200);
  check("returning to the unit's value makes Apply unavailable again",
    fBtn.getAttribute("aria-disabled") === "true");
  const note = document.getElementById("routing-inputs-note").textContent || "";
  check("the page no longer promises that changes are sent immediately",
    !/sent to the unit immediately/i.test(note) && /Apply/.test(note));

  // ---- focus safety ----------------------------------------------------
  await openTab("faders");
  const inner = document.querySelector("#routing-inputs select");
  inner.focus();
  check("a control inside the visible pane can hold focus", document.activeElement === inner);
  document.getElementById(tabId("master")).click();
  check("focus is moved to the tab BEFORE its old pane is hidden",
    document.activeElement === document.getElementById(tabId("master")),
    document.activeElement.id || document.activeElement.tagName);
  check("the pane that had focus is now hidden",
    document.querySelector("#routing-panel > .cat-tab[data-tab-key='faders']").hidden);
  await waitForApi(TAB_READS.master);
  await quiet();

  // ---- what each tab READS from the device -----------------------------
  await probeClear();
  document.getElementById(tabId("levels")).click();
  check("opening Levels reads the fader/monitor levels", await waitForApi(["/api/faders/levels"]));
  check("the auto-mute switches are on this tab and are real checkboxes",
    ["automute-monitor", "automute-bt"].some((id) => document.getElementById(id))
    || !!lvlPane.querySelector("#routing-automute input[type=checkbox]"),
    (lvlPane.querySelector("#routing-automute") || {}).innerHTML ? "built" : "empty");
  check("opening Levels reads the monitor mix levels", await waitForApi(["/api/monitormix/live"]));
  await quiet();
  let log = await probeLog();
  // Levels DOES take the routing-live read: the auto-mute switches are on this tab and they come
  // from that one payload. What it must not do is read the things that belong to other tabs.
  check("opening Levels reads the auto-mute switches it carries",
    hit(log, "/api/routing/live"), paths(log).join(" "));
  check("and it does not read the output mixes, which are another tab's",
    !hit(log, "/api/routing/matrix/live"), paths(log).join(" "));

  await probeClear();
  document.getElementById(tabId("mixes")).click();
  check("opening Output mixes reads the live matrix",
    await waitForApi(["/api/routing/matrix/live"]));
  await quiet();
  log = await probeLog();
  check("opening Output mixes does not read the levels",
    !hit(log, "/api/faders/levels"), paths(log).join(" "));

  await probeClear();
  document.getElementById(tabId("master")).click();
  check("opening Master takes the one live read that fills it",
    await waitForApi(["/api/routing/live"]));
  await quiet();

  // ---- the reads are debounced ----------------------------------------
  // Arrowing along the strip selects as it goes, so four tabs must NOT mean four device reads.
  await openTab("mixes");          // let this one's own read land before the log is cleared
  document.getElementById(tabId("mixes")).focus();
  await probeClear();
  for (let i = 0; i < 3; i++) { key("ArrowRight", document.activeElement); await sleep(40); }
  await waitForApi(["/api/routing/live"]);   // the tab it ENDED on
  await quiet();
  log = await probeLog();
  const apiCalls = (await apiLog()).map((e) => e.method + " " + e.path);
  check("arrowing across the strip fires ONE tab read, not one per tab",
    apiCalls.length === 1 && apiCalls[0].indexOf("/api/routing/live") >= 0,
    apiCalls.join(" "));
  check("the tabs it arrowed THROUGH did not read anything",
    !hit(log, "/api/faders/levels") && !hit(log, "/api/routing/matrix/live"),
    apiCalls.join(" "));

  // ---- 0b.1: the levels must not be stale on re-entry -------------------
  await openTab("levels");
  await probeClear();
  location.hash = "#/status";
  await sleep(400);
  location.hash = "#/routing";
  await waitFor(() => !document.getElementById("sec-routing").hidden);
  // WAIT FOR THE READS, never a fixed sleep - this file's own rule, and the place it is easiest to
  // break. Anything that delays startup (the summary's own model dump, say) pushes the reads past
  // a fixed sleep, and the checks then fail for a reason that has nothing to do with what they test.
  await waitForApi(["/api/faders/levels", "/api/monitormix/live"]);
  await quiet();
  log = await probeLog();
  check("coming back to the page re-reads the levels (the stale-levels bug)",
    hit(log, "/api/faders/levels"), paths(log).join(" "));
  check("re-entry also re-reads the monitor mix levels",
    hit(log, "/api/monitormix/live"), paths(log).join(" "));

  // ---- a re-read must not rebuild what it is reading into ---------------
  // THE PRINCIPLE: nothing may replace a subtree a screen reader may be sitting in, whatever
  // triggered the replacement. Both of these tabs re-read the device every time they are opened, so
  // the controls are replaced at the moment you arrive on them unless the loader updates in place.
  // Nothing asserted this before, which is exactly why the shape survived two fixes.
  await openTab("levels");
  const lvlHost = document.getElementById("fader-levels");
  const lvlFirst = lvlHost.firstElementChild;
  const lvlCount = lvlHost.children.length;
  const monLevel = document.getElementById("monitor-level");
  const mmHost = document.getElementById("monmix-sliders");
  const mmFirst = mmHost.firstElementChild;
  const mmCount = mmHost.children.length;
  const firstFader = lvlHost.querySelector("input[type=range]");
  firstFader.focus();
  await loadFaderLevels(false);
  await loadMonmixOutput();
  check("re-reading keeps the fader level sliders' own nodes",
    lvlHost.firstElementChild === lvlFirst && lvlHost.children.length === lvlCount,
    lvlCount + " -> " + lvlHost.children.length);
  check("re-reading keeps the monitor speaker slider",
    document.getElementById("monitor-level") === monLevel);
  check("re-reading keeps the monitor mix sliders' own nodes",
    mmHost.firstElementChild === mmFirst && mmHost.children.length === mmCount,
    mmCount + " -> " + mmHost.children.length);
  check("the slider the reader was on is still the focused element after the re-read",
    document.activeElement === firstFader,
    document.activeElement.id || document.activeElement.tagName);
  check("a re-read still puts a real value on the slider",
    /percent$/.test(firstFader.getAttribute("aria-valuetext") || ""),
    firstFader.getAttribute("aria-valuetext"));
  // Leaving and coming back is the real-world version of the same thing.
  await openTab("master");
  await openTab("levels");
  check("leaving the tab and coming back keeps the same slider nodes",
    lvlHost.firstElementChild === lvlFirst
    && document.getElementById("monitor-level") === monLevel
    && mmHost.firstElementChild === mmFirst);

  await openTab("mixes");
  const modeSel = document.getElementById("matrix-mode");
  const modeFirst = modeSel.firstElementChild;
  const srcList = document.getElementById("matrix-sources-list");
  const srcFirst = srcList.firstElementChild;
  await loadMatrixState();
  check("re-reading the output mixes keeps the source switches' own nodes",
    srcList.firstElementChild === srcFirst);
  onMatrixOutputChange();
  check("re-showing an output keeps the mode dropdown's option nodes",
    modeSel.firstElementChild === modeFirst);

  // ---- the fader channel list follows the USB mode -----------------------
  // The page is built once; a live read whose list differs (Expanded switched on or off since) must
  // rebuild every fader's options, and the same list again must leave the nodes alone.
  const f0 = routingCtl.faders[0];
  const was = Array.from(f0.options).map((o) => [o.value, o.textContent]);
  const swapped = [[0, "Mic 1"], [12, "Game"], [13, "Music"]];
  applyFaderChannels(swapped);
  check("a different channel list from the live read rebuilds the fader options",
    Array.from(f0.options).map((o) => o.textContent).join("|") === "Mic 1|Game|Music",
    Array.from(f0.options).map((o) => o.textContent).join("|"));
  const kept = f0.firstElementChild;
  applyFaderChannels(swapped);
  check("the same list again leaves the option nodes alone", f0.firstElementChild === kept);
  applyFaderChannels(was.map((p) => [parseInt(p[0], 10), p[1]]));

  // ---- remembered tab, and the retired group state ----------------------
  await openTab("master");
  let stored = null;
  try { stored = JSON.parse(window.localStorage.getItem("accesscaster.tabs") || "{}"); } catch (e) {}
  check("the selected tab is remembered per page, by key",
    stored && stored.routing === "master", stored && stored.routing);
  // Every page is on tabs now, so the WHOLE collapsible-group store goes rather than one prefix at
  // a time. A remembered "closed" must not outlive a design with no closed state.
  window.localStorage.setItem("accesscaster.groups",
    JSON.stringify({ "rt.mixes": false, "about.help": true }));
  dropRetiredGroupState();
  check("the retired collapsible-group state is dropped wholesale",
    window.localStorage.getItem("accesscaster.groups") === null,
    window.localStorage.getItem("accesscaster.groups"));

  // ---- nothing was written --------------------------------------------
  // Writes are stubbed by the probe anyway, but a check that tried to write is a check that would
  // have moved the desk in a live app, so it is worth failing on. Housekeeping calls are exempt.
  const HOUSEKEEPING = ["/api/ping", "/api/focus", "/api/quit"];
  const all = await probeLog();
  const writes = all.filter((e) => e.method === "POST" && e.path.indexOf("/api/") === 0
                                   && HOUSEKEEPING.indexOf(e.path) < 0);
  check("nothing in the run tried to write to the device", writes.length === 0,
    writes.map((e) => e.path).join(" "));

  await probeDone();
}

run().catch(async (e) => {
  check("the checks ran to the end", false, String((e && e.stack) || e));
  await probeDone();
});
