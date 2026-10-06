/* Machine checks for the Device page's tab strip (UI rebuild phase 3).
   Run with: python DevTools/ui_probe.py --checks DevTools/ui_checks_device.js --route status --timeout 600
   (127 checks; the default 90, and even 240, run out before the page reports back.)
   Same shape as ui_checks_routing.js: this is NOT a screen-reader pass, it pins the things a screen
   reader depends on and the eye cannot see (roles, states, one tab stop, what is exposed, and which
   part of the unit each tab reads), so the NVDA run is about how it sounds. */

/* EIGHT TABS, AND THE ORDER IS REASONED. MIDI and Technical are NOT tabs of their own: MIDI is one
   switch and a setting of the unit, so it belongs in Unit settings, and Technical is three facts
   and a list of the kind Hardware already carries, so it folds in as that tab's last section. A
   tab each costs a stop on the strip on every pass, which is the thing a tab strip is meant to
   save. Keyboard shortcuts sits next to Announcements, because the first shortcut is the
   announcer's own on/off and that is the case the tab exists for. Remote access is LAST, and not on
   Hardware: Hardware is about what the UNIT is, and the access link is about reaching the APP. */
const TABS = ["state", "hardware", "unit", "speak", "shortcuts", "levels", "network",
              "access"];
const TITLES = ["Current state", "Hardware", "Unit settings", "Announcements", "Keyboard shortcuts",
                "Level warnings", "Wireless connections", "Remote access"];
/* What each tab reads when it is opened. HARDWARE IS THE ONLY ONE THAT TAKES TWO, and they are not
   two dumps: /api/status is a USB enumeration that page entry makes anyway, and it fills the folded
   -in Technical section. Everything else moved in the PAYLOAD rather than costing a second call -
   the MIDI switch's value rides on /api/device/settings, firmware and the wired network on
   /api/device/state, the wireless mics on /api/network - so no tab crosses to another tab's read to
   paint itself. */
const TAB_READS = {
  state: ["/api/device/overview"],
  hardware: ["/api/device/state", "/api/status"],
  unit: ["/api/device/settings"],
  speak: ["/api/announce"],
  // ONE read on arrival: `/api/hotkeys` is the app's own settings, so the tab answers at once and
  // works with the desk unplugged. The CATALOGUE (`/api/hotkeys/commands`) is the slow one that
  // reads the unit, and it is fetched only when the Add dialog is opened.
  shortcuts: ["/api/hotkeys"],
  // Levels reads NOTHING from the unit: the meter comes from Windows' own audio endpoints, not
  // over the control connection. It is the only tab on this page in that position.
  levels: ["/api/meters"],
  network: ["/api/network"],
  // Remote access reads the APP, never the desk: the link is this app's own, so the tab is instant
  // and works with the unit unplugged. Like `levels` and `shortcuts` in that respect.
  access: ["/api/access"],
};

const tabId = (k) => "tab-device-" + k;
const strip = () => document.querySelector("#sec-status [role='tablist']");
const buttons = () => Array.from(strip().querySelectorAll("[role='tab']"));
const panes = () => Array.from(document.querySelectorAll("#device-panel > .cat-tab"));
const selectedKey = () => {
  const b = buttons().find((x) => x.getAttribute("aria-selected") === "true");
  return b ? b.id.replace("tab-device-", "") : null;
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

async function apiLog() {
  const log = await probeLog();
  return log.filter((e) => e.path.indexOf("/api/") === 0 && e.path.indexOf("/api/ping") < 0);
}

/* Wait until every one of these endpoints has been asked for. Returns false on timeout, which is
   what makes "opening this tab reads that" a real check rather than a sleep. */
async function waitForApi(wanted, ms) {
  const until = Date.now() + (ms || 25000);
  while (Date.now() < until) {
    const seen = (await apiLog()).map((e) => e.path);
    if (wanted.every((w) => seen.some((p) => p.indexOf(w) >= 0))) return true;
    await sleep(120);
  }
  return false;
}

/* Wait for the device traffic to stop, so a "nothing else was read" check cannot pass by looking
   too early. */
async function quiet(ms) {
  let last = (await apiLog()).length;
  let lastChange = Date.now();
  const until = Date.now() + 20000;
  while (Date.now() < until) {
    await sleep(150);
    const n = (await apiLog()).length;
    if (n !== last) { last = n; lastChange = Date.now(); }
    else if (Date.now() - lastChange > (ms || 1200)) return;
  }
}

async function openTab(k) {
  document.getElementById(tabId(k)).click();
  await waitForApi(TAB_READS[k]);
  await quiet();
}

function skip(name, why) {
  check("SKIPPED " + name, true, why);
}

async function run() {
  await waitFor(() => !document.getElementById("sec-status").hidden && strip(), 20000);
  await quiet();

  // ---- structure -------------------------------------------------------
  check("the device page has a tab strip", !!strip());
  // DELIBERATELY UNNAMED. A tablist's accessible name is announced every time you
  // arrive at the strip, and the panel then repeats the selected tab's name on the way in, so one
  // move along a strip read as four things of which three were furniture. Every strip sits under a
  // heading that already says what page it is.
  check("the strip is a tablist, and deliberately has no name of its own",
    strip().getAttribute("role") === "tablist" && !strip().getAttribute("aria-label"),
    strip().getAttribute("aria-label"));
  check("eight tabs, in the agreed order",
    buttons().length === TABS.length && buttons().every((b, i) => b.id === tabId(TABS[i])),
    buttons().map((b) => b.id).join(","));
  check("the tab titles are the agreed ones",
    buttons().every((b, i) => b.textContent.trim() === TITLES[i]),
    buttons().map((b) => b.textContent.trim()).join(" | "));
  // NO LANDMARK ROUND THE STRIP: tried and rejected. Jumping to a landmark leaves
  // a screen reader in browse mode, so the arrow keys do not move along the tabs until you switch
  // out of it - more confusing than walking to the strip. The section headings are the landmarks.
  check("the strip is NOT wrapped in a landmark", strip().parentElement.tagName !== "NAV",
    strip().parentElement.tagName);
  check("and the tablist itself stays unnamed, so a move along it is short",
    !strip().getAttribute("aria-label") && !strip().getAttribute("aria-labelledby"));
  check("the strip sits ABOVE the panel",
    strip().nextElementSibling === document.getElementById("device-panel"));
  check("no collapsible groups left on this page",
    document.querySelectorAll("#sec-status details.ac-group").length === 0);
  check("the connection line stays ABOVE the strip, outside the panel",
    !document.getElementById("device-panel").contains(document.getElementById("status-line")));
  // The panel is deliberately NOT labelled by the selected tab either: a reader announces the tab
  // name as you arrive on the tab, and again as you enter the panel. Once is enough.
  check("the panel is a tabpanel, and does not repeat the tab's name",
    document.getElementById("device-panel").getAttribute("role") === "tabpanel"
    && !document.getElementById("device-panel").getAttribute("aria-labelledby"));
  check("every tab points at the panel it controls",
    buttons().every((b) => b.getAttribute("aria-controls") === "device-panel"));

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
  // The FIRST heading is written by loadOverview so it names the connected desk ("RODECaster Pro II
  // connected. Current state:"), which means comparing it to a fixed string passes or fails purely
  // on whether that read had landed yet - a flake, not a check. Its own assertion is below.
  const paneHeads = panes().map((p) => p.querySelector("h2").textContent.trim());
  check("the panes are named after their tabs",
    paneHeads.slice(1).join(" | ")
      === "Hardware status | Unit settings | Announce Interactions on Mixer | Keyboard shortcuts | "
       + "Level warnings | Wireless connections | Remote access from another device",
    paneHeads.join(" | "));
  check("and the first one names the connected unit, whether or not that read has landed",
    /current state/i.test(paneHeads[0]), paneHeads[0]);

  // ---- keyboard --------------------------------------------------------
  await openTab("state");
  document.getElementById(tabId("state")).focus();
  key("ArrowRight", document.activeElement);
  check("Right arrow moves to the next tab and takes focus with it",
    selectedKey() === "hardware" && document.activeElement.id === tabId("hardware"),
    selectedKey() + "/" + document.activeElement.id);
  const LAST = TABS[TABS.length - 1];
  key("End", document.activeElement);
  check("End jumps to the last tab", selectedKey() === LAST, selectedKey());
  key("Home", document.activeElement);
  check("Home jumps to the first tab", selectedKey() === "state", selectedKey());
  key("ArrowLeft", document.activeElement);
  check("Left arrow from the first tab wraps to the last", selectedKey() === LAST, selectedKey());
  key("ArrowUp", document.activeElement);
  check("Up and down are deliberately unbound on a horizontal strip",
    selectedKey() === LAST, selectedKey());
  check("selection follows the arrows (automatic activation)",
    visiblePanes().length === 1 && visiblePanes()[0].dataset.tabKey === LAST);
  await quiet();

  // ---- focus safety ----------------------------------------------------
  await openTab("speak");
  const sw = document.getElementById("announce-toggle");
  sw.focus();
  check("a control inside the visible pane can hold focus", document.activeElement === sw);
  document.getElementById(tabId("levels")).click();
  check("focus is moved to the tab BEFORE its old pane is hidden",
    document.activeElement === document.getElementById(tabId("levels")),
    document.activeElement.id || document.activeElement.tagName);
  check("the pane that had focus is now hidden",
    document.querySelector("#device-panel > .cat-tab[data-tab-key='speak']").hidden);
  await quiet();

  // ---- what each tab READS from the device -----------------------------
  for (const k of TABS) {
    await probeClear();
    document.getElementById(tabId(k)).click();
    check("opening " + k + " reads " + TAB_READS[k].join(" + "),
      await waitForApi(TAB_READS[k]));
    await quiet();
    const log = await probeLog();
    // Nothing else on the page should be re-read for a tab that does not need it.
    const others = Object.keys(TAB_READS)
      .filter((o) => TAB_READS[o][0] !== TAB_READS[k][0])
      .map((o) => TAB_READS[o][0]);
    const strays = others.filter((p) => hit(log, p));
    check("opening " + k + " reads NOTHING else", strays.length === 0, strays.join(" "));
  }

  // ---- a panel with no controls must be a tab stop itself ---------------
  // Otherwise Tab from the strip sails past the content and lands at the top of the page, which is
  // what a panel of pure prose (a description list, a bullet list) does.
  // THE RULE IS "a panel with no focusable content needs the tabindex", not "these tabs get it",
  // so which tabs appear in which list below follows from their content. Network is in the second
  // list because it carries the wifi and Bluetooth controls.
  // Current state is the only pane with nothing focusable on it; Hardware carries the Technical
  // list and has a Refresh button. Levels is in NEITHER list, because what it builds depends on
  // whether Windows can see the unit's streams at all.
  const NO_CONTROLS = ["state"];
  const HAS_CONTROLS = ["hardware", "unit", "speak", "shortcuts", "network"];
  for (const k of NO_CONTROLS) {
    await openTab(k);
    check(k + " has no controls, so the panel itself is a tab stop",
      document.getElementById("device-panel").getAttribute("tabindex") === "0",
      document.getElementById("device-panel").getAttribute("tabindex"));
  }
  for (const k of HAS_CONTROLS) {
    await openTab(k);
    check(k + " has controls, so the panel adds no extra tab stop",
      !document.getElementById("device-panel").hasAttribute("tabindex"),
      document.getElementById("device-panel").getAttribute("tabindex"));
  }

  // ---- Unit settings: re-read must not rebuild the controls -------------
  // Rebuilding them replaced the DOM while a screen reader was reading the panel, which came out as
  // fragments of settings in a random order.
  await openTab("unit");
  const host = document.getElementById("dev-unit-controls");
  const firstBefore = host.firstElementChild;
  const countBefore = host.children.length;
  await loadUnitSettings();
  check("re-reading Unit settings updates the controls IN PLACE",
    host.firstElementChild === firstBefore && host.children.length === countBefore,
    countBefore + " -> " + host.children.length);
  check("the value copy beside a brightness slider is not its own live region",
    Array.from(host.querySelectorAll("output"))
      .every((o) => o.getAttribute("aria-hidden") === "true"
                    && o.getAttribute("aria-live") === "off"),
    host.querySelectorAll("output").length + " outputs");
  check("each choice carries the unit's value, which Escape and leaving put back",
    Array.from(host.querySelectorAll("select")).every((s) => s.dataset.deviceValue === s.value),
    Array.from(host.querySelectorAll("select")).map((s) => s.dataset.deviceValue).join(","));
  check("and each is described as applying on Enter",
    Array.from(host.querySelectorAll("select")).every((s) =>
      (s.getAttribute("aria-describedby") || "").includes("enter-to-apply-hint")));

  // The TAB is what triggers the Unit settings read - there is no group to expand.
  await openTab("speak");          // leave the tab first, or the click below selects nothing
  await probeClear();
  document.getElementById(tabId("unit")).click();
  check("Unit settings reads when its tab is opened",
    await waitForApi(["/api/device/settings"]));
  await quiet();
  check("and the controls it builds are actually there",
    document.querySelectorAll("#dev-unit-controls select, #dev-unit-controls input").length > 0,
    document.querySelectorAll("#dev-unit-controls select, #dev-unit-controls input").length);

  // ---- the folded-in Technical section: the USB list is the same shape, so it gets the same check
  // Hardware re-reads /api/status when it is opened. The interfaces do not change while the app is
  // open, so the list's NODES must survive the read rather than being emptied and rebuilt.
  await openTab("hardware");
  const ifList = document.getElementById("interfaces-list");
  const ifFirst = ifList.firstElementChild;
  const ifCount = ifList.children.length;
  await loadStatus(false);
  check("re-reading the status keeps the USB interface list's own nodes",
    ifList.firstElementChild === ifFirst && ifList.children.length === ifCount,
    ifCount + " -> " + ifList.children.length);
  check("the USB list is never left empty (it says so instead)",
    ifList.children.length > 0, ifList.children.length);

  // ---- what moved when Network became Wireless connections ----
  // Three readouts changed tab, so the thing to pin is that each landed WHERE IT WAS SENT and is
  // actually painted there. A moved readout that renders nowhere still looks fine on the tab it left.
  const flat = (n) => (n ? n.textContent.replace(/\s+/g, " ").trim() : "");
  const hwPane = () => document.querySelector("#device-panel > .cat-tab[data-tab-key='hardware']");
  const wlPane = () => document.querySelector("#device-panel > .cat-tab[data-tab-key='network']");

  await openTab("hardware");
  await waitFor(() => flat(document.getElementById("dev-firmware")) !== "Unknown", 25000);
  check("Hardware shows the firmware, read from its own /api/device/state",
    /\d/.test(flat(document.getElementById("dev-firmware"))),
    flat(document.getElementById("dev-firmware")));
  check("and whether a network cable is in",
    /connected/i.test(flat(document.getElementById("dev-wired"))),
    flat(document.getElementById("dev-wired")));
  check("Hardware no longer talks about wireless mics",
    !/wireless mic/i.test(flat(document.getElementById("dev-state"))),
    flat(document.getElementById("dev-state")));
  check("the moved readouts are a description list, not lines in the live region",
    hwPane().querySelectorAll("#dev-fw-details dt").length === 2,
    hwPane().querySelectorAll("#dev-fw-details dt").length);

  await openTab("network");
  await waitFor(() => flat(document.getElementById("net-mics")) !== "Not read yet.", 25000);
  const mics = flat(document.getElementById("net-mics"));
  check("Wireless connections reads out the wireless mics", /wireless mic/i.test(mics), mics);
  // THREE OUTCOMES, not two: "the unit reported none" is a different fact from "none connected",
  // and saying the second when we mean the first invents an answer (server._wireless_mics).
  check("and says which of the three cases it is, without inventing one",
    /none connected|did not report any|connected/i.test(mics), mics);
  check("the mic readout is NOT a second live region on this tab",
    wlPane().querySelectorAll("[aria-live]").length === 1,
    Array.from(wlPane().querySelectorAll("[aria-live]")).map((n) => n.id).join(","));
  check("firmware and the wired network are gone from this tab",
    !wlPane().querySelector("#dev-firmware, #dev-wired, #net-firmware, #net-wired"));
  await quiet();

  // ---- the two tabs that were retired into other tabs ----------------------
  check("the USB interfaces are on Hardware now, not on a tab of their own",
    hwPane().contains(document.getElementById("interfaces-list"))
    && hwPane().contains(document.getElementById("status-details")));
  check("and they are a section under an h3, not a second h2 on the pane",
    document.getElementById("dev-h-usb").tagName === "H3",
    document.getElementById("dev-h-usb").tagName);

  await openTab("unit");
  const unitPane = document.querySelector("#device-panel > .cat-tab[data-tab-key='unit']");
  const midi = document.getElementById("midi-toggle");
  check("the MIDI switch is on Unit settings now", unitPane.contains(midi));
  check("it is still a real checkbox with its own name and hint",
    midi.type === "checkbox" && !!document.getElementById(midi.getAttribute("aria-describedby")),
    midi.type);
  // It is NOT part of the built spec, so it has its own paint step in loadUnitSettings. The failure
  // this pins is that step being dropped: the switch would then sit at whatever the page last left
  // it on and quietly misreport the unit. Compared against the SETTINGS payload, which is where its
  // value moved to - if it were still riding on /api/network this would be reading a stale switch.
  const unitRead = await (await fetch("/api/device/settings")).json();
  check("and this tab's own read paints it from the unit",
    unitRead.midi === null || unitRead.midi === undefined
      ? false : midi.checked === !!unitRead.midi,
    "payload " + unitRead.midi + ", switch " + midi.checked);
  await quiet();

  // ---- the reads are debounced ----------------------------------------
  await openTab("state");
  document.getElementById(tabId("state")).focus();
  await probeClear();
  // TEN rights over EIGHT tabs lands on Unit settings, index 2 (it was nine over seven, and eight
  // over six before Keyboard shortcuts). The TARGET is what matters, not the number: Unit settings
  // takes exactly ONE call, so "one read, not one per tab" is a clean count. Hardware would muddy
  // it, being the one tab that legitimately makes two; Keyboard shortcuts reads the service rather
  // than the desk; Remote access reads nothing at all. **Recompute this when a tab is added.**
  for (let i = 0; i < 10; i++) { key("ArrowRight", document.activeElement); await sleep(40); }
  check("the tab it ended on reads", await waitForApi(["/api/device/settings"]));
  await quiet();
  const apiCalls = (await apiLog()).map((e) => e.method + " " + e.path);
  check("arrowing across the strip fires ONE tab read, not one per tab",
    apiCalls.length === 1, apiCalls.join(" "));

  // ---- coming back to the page re-reads the tab you are on --------------
  await openTab("unit");
  await probeClear();
  location.hash = "#/routing";
  await sleep(500);
  location.hash = "#/status";
  await waitFor(() => !document.getElementById("sec-status").hidden);
  check("re-entry reads the status line", await waitForApi(["/api/status"]));
  check("re-entry re-reads the tab you left on", await waitForApi(["/api/device/settings"]));
  await quiet();

  // ---- remembered tab, and the retired group state ----------------------
  await openTab("network");
  let stored = null;
  try { stored = JSON.parse(window.localStorage.getItem("accesscaster.tabs") || "{}"); } catch (e) {}
  check("the selected tab is remembered per page, by key",
    stored && stored.device === "network", stored && stored.device);
  // Every page is on tabs now, so the WHOLE collapsible-group store goes rather than one prefix at
  // a time.
  window.localStorage.setItem("accesscaster.groups",
    JSON.stringify({ "dev.unit": false, "about.help": true }));
  dropRetiredGroupState();
  check("the retired collapsible-group state is dropped wholesale",
    window.localStorage.getItem("accesscaster.groups") === null,
    window.localStorage.getItem("accesscaster.groups"));

  // ---- nothing was written --------------------------------------------
  const HOUSEKEEPING = ["/api/ping", "/api/focus", "/api/quit"];
  const all = await probeLog();
  // ---- the LEVELS tab -------------------------------------------------------------
  // What is worth pinning here is not the controls, it is the HONEST DEGRADATION. This is the only
  // part of the app that cannot work over Remote Desktop, and it was written IN a Remote Desktop
  // session, so the failure path is the one path that has definitely been exercised. It must
  // explain itself rather than showing dead controls or an empty dropdown.
  await openTab("levels");
  await sleep(600);
  const lv = document.querySelector("#device-panel > .cat-tab[data-tab-key='levels']");
  // THREE JOBS, AND THE MIDDLE ONE EARNS ITS PLACE: Windows' own level for the stream sits between
  // the stream chooser and the warnings, because it is the hidden multiplier that decides what
  // both of them mean. The level CHECK is not here at all - it is per channel on Channels - so the
  // first heading is the stream chooser the background watch needs.
  check("the Levels pane has its three jobs under their own h3s, in that order",
    Array.from(lv.querySelectorAll("h3")).map((h) => h.textContent.trim()).join(" | ")
      === "Which stream to watch | Windows level | Warn me while I work",
    Array.from(lv.querySelectorAll("h3")).map((h) => h.textContent.trim()).join(" | "));
  // The control itself: dB and a number box, NOT a percentage slider. On this endpoint 50 percent
  // is -1.08 dB and unity is about 55 percent, so a percentage is a number nobody can reason
  // about, and dragging cannot land on "no change".
  // WHEN THERE IS NOTHING TO METER, THESE FIVE ARE SKIPPED, NOT FAILED. The Windows-level control
  // is only built for a real endpoint, and in a Remote Desktop session the desk's audio streams
  // belong to the console session, so it is honestly absent. Failing here trains everyone to
  // ignore five failures, which is how a real one gets missed - the recording suite makes the same
  // move when there is no drive in.
  const lvlBox = document.getElementById("meter-input-db");
  if (!lvlBox) {
    const why = (document.getElementById("meter-status") || {}).textContent || "no endpoint";
    ["the Windows level is a NUMBER box in dB",
     "its label carries the unit",
     "its range cannot silently mute the machine",
     "there is a one-press way back to no change",
     "its hint says this applies to EVERY application"].forEach((n) => skip(n, why.slice(0, 90)));
  } else {
  check("the Windows level is a NUMBER box in dB, not a percentage slider",
    lvlBox && lvlBox.type === "number", lvlBox && lvlBox.type);
  // "dB", not the word "decibels": the label is read on every arrival at the control, and dB is
  // what the unit is called everywhere else in the app.
  check("its label carries the unit, so a bare number cannot be mistaken for a percentage",
    lvlBox && /\bdB\b/.test((document.querySelector("label[for='meter-input-db']") || {})
      .textContent || ""),
    (document.querySelector("label[for='meter-input-db']") || {}).textContent);
  check("its range is the narrowed one: nothing can silently mute the machine",
    lvlBox && parseFloat(lvlBox.min) >= -30, lvlBox && lvlBox.min);
  const unity = document.getElementById("meter-input-unity");
  check("there is a one-press way back to no change", !!unity && unity.tagName === "BUTTON");
  check("and its hint says this applies to EVERY application, not just this one",
    /every application/i.test((document.getElementById("meter-input-hint") || {})
      .textContent || ""));
  }
  // NO NAMED GROUPS ANY MORE. A role="group" with a name is announced on the way in,
  // before the control you were heading for, and again as "out of grouping" on the way out. The
  // h3 above each section says the same thing, is reachable with H, and costs nothing when you are
  // not looking for it. What is pinned now is that the HEADINGS are still there, since they are
  // what carries the structure.
  check("no named groups are left on this pane",
    lv.querySelectorAll("[role='group']").length === 0,
    lv.querySelectorAll("[role='group']").length);
  const meterStatus = document.getElementById("meter-status");
  const streamSel = document.getElementById("meter-stream");
  const streamOff = streamSel && streamSel.getAttribute("aria-disabled") === "true";
  const available = streamSel && streamSel.options.length > 0 && !streamOff;
  check("the stream dropdown is never left empty AND usable with nothing in it",
    !(streamSel.options.length === 0 && !streamOff),
    streamSel.options.length + " options, unavailable=" + streamOff);
  // It is marked unavailable, NEVER removed from the tab order: a natively disabled select is
  // skipped silently, which is how a whole control disappears with no explanation.
  check("and when it is unavailable it keeps its place and carries the reason",
    !streamSel.disabled && (!streamOff || !!streamSel.dataset.offReason),
    streamSel.dataset.offReason);
  check("when there is nothing to meter, the page SAYS why instead of going quiet",
    available || (meterStatus && meterStatus.textContent.trim().length > 30),
    available ? "(streams available)" : (meterStatus && meterStatus.textContent));
  check("and the reason names the real cause rather than saying it failed",
    available || /Remote Desktop|not connected|transfer mode/i.test(meterStatus.textContent),
    meterStatus && meterStatus.textContent.slice(0, 80));
  check("the stream dropdown is named as a DEVICE, which is what it lists",
    /audio device/i.test((document.querySelector("label[for='meter-stream']") || {})
      .textContent || ""),
    (document.querySelector("label[for='meter-stream']") || {}).textContent);
  check("the warn-me switches are built whether or not a stream is there, so the settings are "
    + "reachable before you plug in",
    ["meter-watch-on", "meter-warn-clip", "meter-warn-silence", "meter-clip-sound"]
      .every((id) => {
        const el = document.getElementById(id);
        return el && el.type === "checkbox";
      }));
  // A SOUND as well as the words for clipping: it is the one thing on this tab you react to
  // rather than think about, and a spoken sentence arrives a word at a time.
  check("clipping can be a sound as well as a sentence, and it is on by default",
    document.getElementById("meter-clip-sound").checked);
  // Windows' own MUTE, which reads exactly like a dead microphone and is fixed in a Windows
  // dialog rather than anywhere on the desk. Found live on with both desk inputs muted.
  const mutedSw = document.getElementById("meter-input-muted");
  check("a Windows mute can be seen and undone from here",
    !mutedSw || mutedSw.type === "checkbox", mutedSw && mutedSw.type);
  check("the two thresholds are NUMBER boxes, not sliders (they are values you know)",
    ["meter-silence-after", "meter-speak-every"].every((id) => {
      const el = document.getElementById(id);
      return el && el.type === "number" && el.labels && el.labels.length === 1;
    }));
  check("speaking the level on a timer is OFF by default: a commentary during a take is unusable",
    document.getElementById("meter-speak-every").value === "0",
    document.getElementById("meter-speak-every").value);
  // THE CHECK IS NOT ON THIS TAB ANY MORE. It measured a Windows STREAM, and every
  // stream this desk offers is a mix, so it could never answer "is Mic 2 right". It is per channel
  // now (DevTools/ui_checks_levelcheck.js). What is pinned here is that it LEFT CLEANLY: no orphan
  // button, and the tab says where the check went rather than leaving a reader to find out.
  check("the stream check button is gone from this tab",
    !document.getElementById("meter-check-btn"));
  check("and so is its result line, which would now never be written to",
    !document.getElementById("meter-check-result"));
  const checkHint = ((document.getElementById("meter-check-hint") || {}).textContent || "")
    .replace(/\s+/g, " ").trim();
  check("the tab says where the per-channel check lives now",
    /channels page/i.test(checkHint), checkHint);
  // The stream dropdown STAYS: the background watch below still meters one stream.
  check("the stream dropdown is still here for the watch, and named for watching",
    /watch/i.test((document.querySelector("label[for='meter-stream']") || {}).textContent || ""),
    (document.querySelector("label[for='meter-stream']") || {}).textContent);

  // ---- the KEYBOARD SHORTCUTS tab -------------------------------------------------
  // The options are BUILT FROM /api/hotkeys, because server.py owns the list of actions and this
  // page must not become a second copy of it. So these checks assert the SHAPE, not a fixed set of
  // shortcut names.
  // A LISTBOX WITH ONE TAB STOP: Enter edits in a dialog with Save, Delete deletes after asking,
  // and the Applications key (a `contextmenu` event) or a click opens a menu of the rest.
  await openTab("shortcuts");
  const hkList = document.getElementById("hotkey-list");
  const hkOpts = () => [...hkList.querySelectorAll("[role='option']")];
  const hkKey = (target, key) => target.dispatchEvent(new KeyboardEvent("keydown",
    { key, bubbles: true, cancelable: true }));
  check("the shortcuts are a listbox of options",
    hkList && hkList.getAttribute("role") === "listbox" && hkOpts().length >= 1, hkOpts().length);
  check("the list is labelled", !!document.getElementById(hkList.getAttribute("aria-labelledby")));
  // A description is read every time focus comes back into the list, after every dialog.
  check("and NOT described, so coming back from a dialog is quick",
    !hkList.getAttribute("aria-describedby"));
  const hkHelp = document.getElementById("hotkey-list-help");
  check("its hint comes BEFORE it, where browse mode meets it first",
    hkHelp && (hkHelp.compareDocumentPosition(hkList) & Node.DOCUMENT_POSITION_FOLLOWING)
    && /enter/i.test(hkHelp.textContent) && /applications key/i.test(hkHelp.textContent));
  check("ONE tab stop: exactly one option is in the tab order, the selected one",
    hkOpts().filter((o) => o.tabIndex === 0).length === 1
    && hkOpts().some((o) => o.tabIndex === 0 && o.getAttribute("aria-selected") === "true"));
  check("each option says what the shortcut is on, so the list alone answers 'what are my keys'",
    hkOpts().every((o) => /, /.test(o.textContent)),
    hkOpts().slice(0, 3).map((o) => o.textContent).join(" | "));
  if (hkOpts().length > 1) {
    selectHotkey(hkOpts()[0].dataset.id);
    hkKey(document.activeElement, "ArrowDown");
    check("Down moves the selection AND focus to the next shortcut",
      document.activeElement === hkOpts()[1]
      && hkOpts()[1].getAttribute("aria-selected") === "true");
    hkKey(document.activeElement, "Home");
    check("Home goes back to the first", document.activeElement === hkOpts()[0]);
  }

  // EDIT IS A DIALOG WITH SAVE, so browsing its key list registers nothing.
  const editDlg = document.getElementById("hotkey-edit-dialog");
  hkOpts()[0].focus();
  hkKey(document.activeElement, "Enter");
  check("Enter on a shortcut opens it for editing, as a modal", editDlg.open);
  check("the dialog is named after the shortcut",
    /edit shortcut: ./i.test(document.getElementById("hotkey-edit-title").textContent),
    document.getElementById("hotkey-edit-title").textContent);
  const keySel = document.getElementById("hotkey-key");
  check("focus lands on the key, a real select with a real label",
    document.activeElement === keySel && keySel.labels.length === 1 && keySel.options.length > 30);
  check("there are no modifier switches in it: the keys held are one setting for all",
    !editDlg.querySelector("#hotkey-mods"));
  const offSwitch = document.getElementById("hotkey-off");
  check("a shortcut can be switched off, and the switch says OFF",
    /off/i.test(offSwitch.closest("label").textContent), offSwitch.closest("label").textContent.trim());
  check("and it is inverted correctly: an enabled shortcut shows it unticked",
    !offSwitch.checked === !/, off$/.test(hkOpts()[0].textContent), hkOpts()[0].textContent);
  await probeClear();
  keySel.value = keySel.options[(keySel.selectedIndex + 1) % keySel.options.length].value;
  keySel.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(600);
  check("browsing the key list writes NOTHING", !(await probeLog()).some((e) => e.method === "POST"));
  document.getElementById("hotkey-edit-cancel").click();
  check("Cancel closes it and focus goes back to the shortcut",
    !editDlg.open && document.activeElement === hkOpts()[0]);

  // THE MENU, from the Applications key, which arrives as a `contextmenu` event.
  const menu = document.getElementById("hotkey-menu");
  const ctx = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
  hkOpts()[0].dispatchEvent(ctx);
  check("the Applications key opens the app's menu, not the browser's",
    !menu.hidden && menu.getAttribute("role") === "menu" && ctx.defaultPrevented);
  check("with focus on its first item", document.activeElement === menu.querySelector("[role^='menuitem']"));
  check("its speech item is a checkable item that says its state",
    document.getElementById("hotkey-menu-speak").getAttribute("role") === "menuitemcheckbox"
    && document.getElementById("hotkey-menu-speak").hasAttribute("aria-checked"));
  hkKey(document.activeElement, "Escape");
  check("Escape closes it and returns to the shortcut",
    menu.hidden && document.activeElement === hkOpts()[0]);
  hkOpts()[0].click();
  check("a click opens it too, the way in on a touch screen", !menu.hidden);
  hkKey(document.activeElement, "Escape");

  // DELETE asks first, as a true alertdialog.
  const delDlg = document.getElementById("hotkey-delete-dialog");
  hkKey(document.activeElement, "Delete");
  check("the Delete key asks first, in an alertdialog, with Cancel focused",
    delDlg.open && delDlg.getAttribute("role") === "alertdialog"
    && document.activeElement === document.getElementById("hotkey-delete-cancel"));
  document.getElementById("hotkey-delete-cancel").click();
  check("Cancel deletes nothing and returns to the shortcut",
    !delDlg.open && document.activeElement === hkOpts()[0]);

  // THE KEYS TO HOLD are one setting, in a dialog of its own, named on its button.
  const modsBtn = document.getElementById("hotkey-mods-btn");
  check("the keys-to-hold button says what they are now",
    /change the keys to hold, now .+ and /i.test(modsBtn.textContent), modsBtn.textContent);
  modsBtn.click();
  const modsDlg = document.getElementById("hotkey-mods-dialog");
  check("it opens a dialog of four labelled switches", modsDlg.open
    && document.querySelectorAll("#hotkey-mods input[type='checkbox']").length === 4
    && document.getElementById("hotkey-mods").getAttribute("role") === "group");
  document.getElementById("hotkey-mods-cancel").click();
  check("Cancel closes it and returns to the button", !modsDlg.open && document.activeElement === modsBtn);

  // RESTORE asks first, because it throws the whole list away, and says what it will leave.
  const restoreDlg = document.getElementById("hotkey-restore-dialog");
  document.getElementById("hotkey-restore-btn").click();
  check("Restore default shortcuts asks first, in an alertdialog, with Cancel focused",
    restoreDlg.open && restoreDlg.getAttribute("role") === "alertdialog"
    && document.activeElement === document.getElementById("hotkey-restore-cancel"));
  check("and says what it will leave",
    /replaces your \d+ shortcuts? with the \d+ standard ones and the \d+ SMART pad keys/i
      .test(document.getElementById("hotkey-restore-desc").textContent),
    document.getElementById("hotkey-restore-desc").textContent);
  document.getElementById("hotkey-restore-cancel").click();
  check("Cancel changes nothing", !restoreDlg.open);

  // THE PAD SET is a switch that shows whether it is there, partly ticked when only some is.
  const padSet = document.getElementById("hotkeys-pad-set");
  check("Use the SMART pad keys is a described switch",
    padSet && padSet.type === "checkbox"
    && !!document.getElementById(padSet.getAttribute("aria-describedby") || ""));
  check("and it shows the pad set's state",
    (hotkeyData.pad_set === "all") === padSet.checked
    && (hotkeyData.pad_set === "some") === padSet.indeterminate, hotkeyData.pad_set);
  check("the tab points at Backups for copying shortcuts elsewhere",
    !!document.querySelector("[data-tab-key='shortcuts'] a[href='#/profiles']"));

  // ADD offers what it does AND the key, starting on a free one.
  const addDlg = document.getElementById("hotkey-add-dialog");
  document.getElementById("hotkey-add-btn").click();
  check("Add opens as a modal", addDlg.open);
  await waitFor(() => document.getElementById("hotkey-add-category").options.length > 1, 25000);
  const cats = document.getElementById("hotkey-add-category");
  const cmds = document.getElementById("hotkey-add-command");
  const addKey = document.getElementById("hotkey-add-key");
  check("focus lands on the first thing there is to do, not on the heading",
    document.activeElement === cats, document.activeElement && document.activeElement.id);
  check("the catalogue is offered as a category and then what is in it",
    cats.options.length >= 4 && cmds.options.length >= 1
    && cats.labels.length === 1 && cmds.labels.length === 1,
    cats.options.length + " categories, " + cmds.options.length + " in the first");
  check("the categories are the app's own areas, not a flat dump",
    [...cats.options].some((o) => o.textContent === "Channels")
    && [...cats.options].some((o) => o.textContent === "Levels"),
    [...cats.options].map((o) => o.textContent).join(", "));
  check("and the key is chosen there too, starting on one no shortcut uses",
    addKey.labels.length === 1 && addKey.value
    && !hotkeyData.shortcuts.some((s) => s.enabled && s.key === addKey.value), addKey.value);
  document.getElementById("hotkey-add-cancel").click();
  check("Cancel closes it again", !addDlg.open);
  check("nothing here uses the raw disabled attribute, which takes a control out of the tab order",
    document.querySelectorAll("#sec-status .cat-tab[data-tab-key='shortcuts'] [disabled]").length === 0);
  const master = document.getElementById("hotkeys-enabled");
  check("there is one master switch, described by its own hint",
    master && master.type === "checkbox"
    && !!document.getElementById(master.getAttribute("aria-describedby") || ""));
  const hkNote = (document.getElementById("hotkeys-note") || {}).textContent || "";
  check("the tab says a shortcut needs Ctrl, Alt or Windows in it",
    /ctrl, alt or windows/i.test(hkNote), hkNote.replace(/\s+/g, " ").trim().slice(0, 120));

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
