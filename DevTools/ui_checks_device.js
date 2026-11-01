// probe: route=status timeout=600
/* Machine checks for the Device page's tab strip.
   Run with: python DevTools/run_ui_probes.py device
   (the default 90 runs out before the page reports back.)
   Same shape as ui_checks_routing.js: this is NOT a screen-reader pass, it pins the things a screen
   reader depends on and the eye cannot see (roles, states, one tab stop, what is exposed, and which
   part of the unit each tab reads), so the NVDA run is about how it sounds. */

/* FOUR TABS, ALL ABOUT THE UNIT. MIDI and Technical are NOT tabs of their own: MIDI is one switch
   and a setting of the unit, so it belongs in Unit settings, and Technical is three facts and a
   list of the kind Hardware already carries, so it folds in as that tab's last section. A tab each
   costs a stop on the strip on every pass, which is the thing a tab strip is meant to save.
   Everything about the APP is on App settings (ui_checks_settings.js). */
const TABS = ["state", "hardware", "unit", "network"];
const TITLES = ["Current state", "Hardware", "Unit settings", "Wireless connections"];
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
  network: ["/api/network"],
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
  check("four tabs, in the agreed order",
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
      === "Hardware status | Unit settings | Wireless connections",
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
  await openTab("unit");
  const sw = document.getElementById("midi-toggle");
  sw.focus();
  check("a control inside the visible pane can hold focus", document.activeElement === sw);
  document.getElementById(tabId("network")).click();
  check("focus is moved to the tab BEFORE its old pane is hidden",
    document.activeElement === document.getElementById(tabId("network")),
    document.activeElement.id || document.activeElement.tagName);
  check("the pane that had focus is now hidden",
    document.querySelector("#device-panel > .cat-tab[data-tab-key='unit']").hidden);
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
  // list and has a Refresh button.
  const NO_CONTROLS = ["state"];
  const HAS_CONTROLS = ["hardware", "unit", "network"];
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
  await openTab("network");        // leave the tab first, or the click below selects nothing
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

  // ---- which readouts are on Hardware and which on Wireless connections ----
  // Firmware and the wired network are on Hardware, the wireless mics on Wireless connections. The
  // thing to pin is that each is actually painted on its own tab: a readout that renders nowhere
  // still looks fine from the other tab.
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
  // TEN rights over FOUR tabs lands on Unit settings, index 2. The TARGET is what matters, not the
  // number: Unit settings takes exactly ONE call, so "one read, not one per tab" is a clean count.
  // Hardware would muddy it, being the one tab that legitimately makes two. **Recompute this when a
  // tab is added.**
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
  try { stored = JSON.parse(window.localStorage.getItem("accesscaster.tabs") || "{}"); } catch {}
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

  // ---- nothing was written, in the whole run --------------------------------------
  const writes = await probeWrites();
  check("nothing in the run tried to write to the device", writes.length === 0,
    writes.map((e) => e.path).join(" "));

  await probeDone();
}

probeRun(run);
