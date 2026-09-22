/* Machine checks for the About page's tab strip (UI rebuild phase 5, the last page converted).
   Run with: python DevTools/ui_probe.py --checks DevTools/ui_checks_about.js --route about
   Same shape as ui_checks_device.js. NOT a screen-reader pass: it pins the things a screen reader
   depends on and the eye cannot see (roles, states, one tab stop, what is exposed, heading order),
   so the NVDA run is about how it sounds.

   This page is different from the other four in two ways worth checking hard:
     - it is TEXT ONLY (with one exception: Support gains a link once a donation URL is set), so a
       pane with no control needs the PANEL itself to be the tab stop - the Device page's
       Network/Technical fault, and here it is nearly every tab;
     - it reads NOTHING from the unit, so a tab change must fire no device traffic at all.
   It is also where the app-wide absence of collapsible groups is asserted: "no ac-group is left
   anywhere in the app" and "the whole group store is dropped". */

/* FOUR TABS, AND NO Limitations TAB. Its content is a short list in the page's own intro, above
   the strip: as a tab it was one screen of prose that went stale precisely because it duplicated
   facts the Help guide also carries. Support is LAST - the tab you visit deliberately and least
   often. */
const TABS = ["can", "help", "issues", "support"];
const TITLES = ["What it can control", "Help", "Known issues", "Support this project"];
const H2S = ["What AccessCaster can control", "Help: what each page is for", "Known issues",
             "Support this project"];
const HELP_PANE = 1;   // index into panes(); moves whenever a tab is added or removed

const tabId = (k) => "tab-about-" + k;
const strip = () => document.querySelector("#sec-about [role='tablist']");
const buttons = () => Array.from(strip().querySelectorAll("[role='tab']"));
const panes = () => Array.from(document.querySelectorAll("#about-panel > .cat-tab"));
const selectedKey = () => {
  const b = buttons().find((x) => x.getAttribute("aria-selected") === "true");
  return b ? b.id.replace("tab-about-", "") : null;
};
const visiblePanes = () => panes().filter((p) => !p.hidden);
const panel = () => document.getElementById("about-panel");
const key = (ev, target) => target.dispatchEvent(
  new KeyboardEvent("keydown", { key: ev, bubbles: true, cancelable: true }));

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

/* Wait for the traffic the app's own startup fires to stop, so a "nothing was read" check cannot
   pass by looking too early or fail by catching a startup load. */
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

function openTab(k) {
  document.getElementById(tabId(k)).click();
}

async function run() {
  await waitFor(() => !document.getElementById("sec-about").hidden && strip(), 20000);
  await quiet();

  // ---- structure -------------------------------------------------------
  check("the About page has a tab strip", !!strip());
  // DELIBERATELY UNNAMED. A tablist's accessible name is announced every time you
  // arrive at the strip, and the panel then repeats the selected tab's name on the way in, so one
  // move along a strip read as four things of which three were furniture. Every strip sits under a
  // heading that already says what page it is.
  check("the strip is a tablist, and deliberately has no name of its own",
    strip().getAttribute("role") === "tablist" && !strip().getAttribute("aria-label"),
    strip().getAttribute("aria-label"));
  check("three tabs, in the agreed order",
    buttons().length === TABS.length && buttons().every((b, i) => b.id === tabId(TABS[i])),
    buttons().map((b) => b.id).join(","));
  check("the tab titles are the agreed ones",
    buttons().every((b, i) => b.textContent.trim() === TITLES[i]),
    buttons().map((b) => b.textContent.trim()).join(" | "));
  check("the strip sits ABOVE the panel", strip().nextElementSibling === panel());
  // The panel deliberately does NOT repeat the tab's name: a reader announces it on arriving at
  // the tab, and would announce it again on entering the panel. Once is enough.
  check("the panel is a tabpanel, and does not repeat the tab's name",
    panel().getAttribute("role") === "tabpanel"
    && !panel().getAttribute("aria-labelledby"));
  check("every tab points at the panel it controls",
    buttons().every((b) => b.getAttribute("aria-controls") === "about-panel"));

  // The page's own context stays above the strip: the version number is the one live value here and
  // it must not be buried on a tab.
  check("the version line stays ABOVE the strip, outside the panel",
    !panel().contains(document.getElementById("app-version")));
  // The intro carries the project's own words plus the "what is still missing" list, and it is
  // headed so that H-key navigation reaches it. Anchor the check on the HEADING: hanging it off a
  // neighbouring paragraph leaves it throwing on a null the day that paragraph goes.
  const intro = () => document.getElementById("h-about-intro");
  check("the intro heading stays above the strip, outside the panel",
    intro() && !panel().contains(intro())
    && !!(intro().compareDocumentPosition(strip()) & Node.DOCUMENT_POSITION_FOLLOWING));

  // ---- the collapsible groups are GONE, app-wide ------------------------
  check("no collapsible group is left on this page",
    document.querySelectorAll("#sec-about details.ac-group").length === 0);
  check("no collapsible group is left ANYWHERE in the app",
    document.querySelectorAll("details.ac-group, .ac-group-body").length === 0,
    document.querySelectorAll("details.ac-group, .ac-group-body").length);
  check("the group machinery itself is gone from app.js",
    typeof window.buildGroup === "undefined"
    && typeof window.wireStaticGroups === "undefined"
    && typeof window.groupState === "undefined",
    [typeof window.buildGroup, typeof window.wireStaticGroups,
     typeof window.groupState].join(","));
  check("every page now has a tab strip except Profiles, which was left alone on purpose",
    document.querySelectorAll(".app-section [role='tablist']").length >= 4,
    document.querySelectorAll(".app-section [role='tablist']").length);

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
  check("the panes are named after their tabs",
    panes().map((p) => p.querySelector("h2").textContent.trim()).join(" | ") === H2S.join(" | "),
    panes().map((p) => p.querySelector("h2").textContent.trim()).join(" | "));
  check("no heading level is skipped inside a pane (page h1, pane h2, sub-headings h3)",
    panes().every((p) => p.querySelectorAll("h4, h5, h6").length === 0));
  check("the Help pane keeps an h3 per page, so H still walks the guide",
    panes()[HELP_PANE].querySelectorAll("h3").length >= 6,
    panes()[HELP_PANE].querySelectorAll("h3").length);
  check("the page's own h1 is outside the panel and still the only one",
    document.querySelectorAll("#sec-about h1").length === 1
    && !panel().contains(document.getElementById("h-about")));

  // Every list still points at a heading that exists, which is what names it for a screen reader.
  const labelled = Array.from(document.querySelectorAll("#about-panel [aria-labelledby]"));
  check("every labelled list points at a heading that exists",
    labelled.length > 0
    && labelled.every((el) => document.getElementById(el.getAttribute("aria-labelledby"))),
    labelled.map((el) => el.getAttribute("aria-labelledby")).join(","));

  // ---- a hidden pane really is out of the way ---------------------------
  openTab("can");
  check("the Help guide's headings are NOT exposed from another tab",
    panes()[HELP_PANE].hidden && !panes()[HELP_PANE].querySelector("h3").offsetParent,
    panes()[HELP_PANE].hidden);

  // ---- keyboard --------------------------------------------------------
  document.getElementById(tabId("can")).focus();
  key("ArrowRight", document.activeElement);
  check("Right arrow moves to the next tab and takes focus with it",
    selectedKey() === "help" && document.activeElement.id === tabId("help"),
    selectedKey() + "/" + document.activeElement.id);
  key("End", document.activeElement);
  check("End jumps to the last tab", selectedKey() === "support", selectedKey());
  key("Home", document.activeElement);
  check("Home jumps to the first tab", selectedKey() === "can", selectedKey());
  key("ArrowLeft", document.activeElement);
  check("Left arrow from the first tab wraps to the last",
    selectedKey() === "support", selectedKey());
  key("ArrowUp", document.activeElement);
  check("Up and down are deliberately unbound on a horizontal strip",
    selectedKey() === "support", selectedKey());
  check("selection follows the arrows (automatic activation)",
    visiblePanes().length === 1 && visiblePanes()[0].dataset.tabKey === "support");

  // ---- EVERY panel here must be a tab stop -----------------------------
  // No pane on this page holds a control, so without tabindex="0" on the panel the Tab key sails
  // past the whole page and lands on the footer or the nav, which reads as being dumped somewhere
  // else entirely. This is the Device page's Network/Technical fault, and here it is every tab.
  for (const k of TABS) {
    openTab(k);
    const hasControl = visiblePanes()[0].querySelectorAll(
      "a[href], button, input, select, textarea, [tabindex]").length > 0;
    if (!hasControl) {
      check(k + " is prose only, so the panel itself is a tab stop",
        panel().getAttribute("tabindex") === "0", panel().getAttribute("tabindex"));
    }
    // SUPPORT IS THE ONE THAT MAY HOLD A CONTROL, and only when a donation URL has been set: the
    // anchor carries no href until then, which keeps it out of the tab order entirely rather than
    // parking a dead link in the way. So assert the RULE (a pane with focusable content must not
    // also make the panel a tab stop) rather than a fixed count.
    const focusables = visiblePanes()[0].querySelectorAll(
      "a[href], button, input, select, textarea, [tabindex]").length;
    if (k === "support" && focusables > 0) {
      check("support has a link, so the panel is NOT also a tab stop",
        panel().getAttribute("tabindex") === null, panel().getAttribute("tabindex"));
    } else {
      check(k + " really has nothing else the Tab key could reach", focusables === 0, focusables);
    }
  }

  // ---- nothing here reads the device -----------------------------------
  for (const k of TABS) {
    await probeClear();
    openTab(k);
    await sleep(900);            // longer than the other pages' 350 ms read debounce
    await quiet(600);
    const calls = (await apiLog()).map((e) => e.method + " " + e.path);
    check("opening " + k + " reads nothing from the unit", calls.length === 0, calls.join(" "));
  }

  await probeClear();
  location.hash = "#/routing";
  await sleep(600);
  location.hash = "#/about";
  await waitFor(() => !document.getElementById("sec-about").hidden);
  await quiet();
  check("the About page still shows its tab after leaving and coming back",
    visiblePanes().length === 1 && visiblePanes()[0].dataset.tabKey === selectedKey(),
    selectedKey());

  // ---- remembered tab, and the retired group state ----------------------
  // Deliberately the tab we are NOT on: the run ends on Help, so switching to Can is a real change.
  openTab("can");
  let stored = null;
  try { stored = JSON.parse(window.localStorage.getItem("accesscaster.tabs") || "{}"); } catch (e) {}
  check("the selected tab is remembered per page, by key",
    stored && stored.about === "can", stored && stored.about);

  // NO page uses collapsible groups, so the WHOLE store goes rather than one prefix at a time.
  window.localStorage.setItem("accesscaster.groups",
    JSON.stringify({ "about.help": true, "ch.eq": false }));
  dropRetiredGroupState();
  check("the retired collapsible-group state is dropped wholesale",
    window.localStorage.getItem("accesscaster.groups") === null,
    window.localStorage.getItem("accesscaster.groups"));

  // ---- nothing was written --------------------------------------------
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
