/* Reproduce the Channels-tab focus fault: tab into the strip on Mic 2, arrow right
   to Presets (announced correctly), arrow LEFT back to tab 1 and the screen reader reads a combo box
   from inside the panel ("Line-in, 1 of 11" = the mic type list) instead of the tab; arrow right
   again and it reads the preset list. So the question this answers is simply: WHERE DOES FOCUS GO?
   Run: python DevTools/ui_probe.py --checks DevTools/ui_checks_chantabs.js --route channels */

const el = (id) => document.getElementById(id);
const strip = () => document.querySelector("#channel-panel [role='tablist']");
const buttons = () => Array.from(strip().querySelectorAll("[role='tab']"));
const selected = () => {
  const b = buttons().find((x) => x.getAttribute("aria-selected") === "true");
  return b ? b.id : null;
};
const key = (k, t) => t.dispatchEvent(
  new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true }));

function where() {
  const a = document.activeElement;
  if (!a) return "none";
  return (a.id || a.tagName) + " [" + (a.getAttribute("role") || a.tagName.toLowerCase()) + "]"
    + (a.isConnected ? "" : " DETACHED");
}

async function waitFor(fn, ms) {
  const until = Date.now() + (ms || 8000);
  while (Date.now() < until) {
    try { if (fn()) return true; } catch (e) { /* not ready */ }
    await sleep(80);
  }
  return false;
}

async function run() {
  await waitFor(() => !el("sec-channels").hidden && strip(), 20000);
  await sleep(1200);

  // Mic 2, the channel he was on.
  const sel = el("channel-select");
  sel.value = "Mic2";
  sel.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(600);
  check("on Mic 2 with a tab strip", !!strip() && sel.value === "Mic2", sel.value);
  check("seven tabs for a mic", buttons().length === 7, buttons().length);

  // Tab into the strip: focus the selected tab, as the Tab key would.
  buttons().find((b) => b.tabIndex === 0).focus();
  const first = where();
  check("tabbing into the strip lands on a TAB", /\[tab\]/.test(first), first);

  key("ArrowRight", document.activeElement);
  await sleep(500);
  const afterRight = where();
  check("arrow right stays on a tab (Presets)",
    /\[tab\]/.test(afterRight) && selected() === "tab-channels-presets",
    afterRight + " selected=" + selected());

  // THE FAULT: arrow LEFT back to tab one.
  key("ArrowLeft", document.activeElement);
  const leftImmediate = where();
  await sleep(900);                       // let anything asynchronous land
  const leftSettled = where();
  check("arrow left stays on a tab IMMEDIATELY", /\[tab\]/.test(leftImmediate), leftImmediate);
  check("arrow left is STILL on a tab a moment later", /\[tab\]/.test(leftSettled), leftSettled);
  check("and it is the first tab that is selected",
    selected() === "tab-channels-settings", selected());

  // And right again, where he heard the preset list.
  key("ArrowRight", document.activeElement);
  const rightImmediate = where();
  await sleep(900);
  const rightSettled = where();
  check("arrow right again stays on a tab IMMEDIATELY", /\[tab\]/.test(rightImmediate),
    rightImmediate);
  check("arrow right again is STILL on a tab a moment later", /\[tab\]/.test(rightSettled),
    rightSettled);

  /* THE REAL FIX: a tab change no longer rebuilds ANYTHING. Every pane is built once when the
     channel is rendered, and switching tab only flips `hidden`. So the subtree the screen reader is
     reading can no longer be replaced from under it, which is what made it announce a combo box from
     inside the panel instead of the tab. Same lesson as the Device page's Unit settings. */
  const paneOf = (k) => document.querySelector("#tabpanel-channels > .cat-tab[data-tab-key='" + k + "']");
  check("every tab has its own pane, built up front",
    buttons().every((b) => !!paneOf(b.id.replace("tab-channels-", ""))),
    document.querySelectorAll("#tabpanel-channels > .cat-tab").length + " panes");
  buttons()[0].click();
  await sleep(400);
  const settingsPane = paneOf("settings");
  const dynPane = paneOf("dyn");
  const micType = el("mictype-Mic2");
  check("exactly one pane is visible", Array.from(
    document.querySelectorAll("#tabpanel-channels > .cat-tab")).filter((p) => !p.hidden).length === 1);
  check("the mic type control exists while its own tab is showing", !!micType);
  el("tab-channels-dyn").click();
  await sleep(400);
  check("switching tab does NOT rebuild the panes: the SAME nodes are still there",
    paneOf("settings") === settingsPane && paneOf("dyn") === dynPane);
  check("and the control the screen reader was reading is the same node, just hidden now",
    el("mictype-Mic2") === micType && settingsPane.hidden === true);
  check("the incoming pane is the only one visible",
    dynPane.hidden === false && Array.from(
      document.querySelectorAll("#tabpanel-channels > .cat-tab")).filter((p) => !p.hidden).length === 1);
  check("a hidden pane's heading is out of heading navigation",
    settingsPane.hidden && settingsPane.querySelector("h2") !== null);
  // DELIBERATELY UNNAMED. Naming the panel by the selected tab announces the tab's title twice on
  // every move along the strip, once by the tab and again on entering the panel. So buildTabs sets
  // no aria-labelledby, and no name on the tablist either.
  check("the panel is deliberately NOT named by the selected tab",
    !document.getElementById("tabpanel-channels").getAttribute("aria-labelledby"),
    document.getElementById("tabpanel-channels").getAttribute("aria-labelledby"));
  check("and the strip itself has no name of its own",
    !strip().getAttribute("aria-label") && !strip().getAttribute("aria-labelledby"),
    strip().getAttribute("aria-label"));

  // Is the strip that is on the page the one whose buttons we are pressing? A rebuild would leave
  // focus on a node that is no longer in the document, which reads as being somewhere else entirely.
  check("the focused element is still IN the document",
    document.activeElement && document.activeElement.isConnected, where());
  check("only ONE tablist exists on the page",
    document.querySelectorAll("#channel-panel [role='tablist']").length === 1,
    document.querySelectorAll("#channel-panel [role='tablist']").length);
  check("no duplicate tab ids",
    new Set(buttons().map((b) => b.id)).size === buttons().length,
    buttons().map((b) => b.id).join(","));

  await probeDone();
}

run().catch(async (e) => {
  check("the checks ran to the end", false, String((e && e.stack) || e));
  await probeDone();
});
