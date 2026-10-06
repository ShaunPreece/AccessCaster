/* THE DROPDOWNS THAT CHANGE THE DESK: arrowing browses, Enter applies, leaving puts it back.
   Run with: python DevTools/ui_probe.py --checks DevTools/ui_checks_staged.js --route routing
   and again with --route channels.

   THE RULE THIS GUARDS (app.js enterToApply): a CLOSED select fires `change` on every arrow press,
   and arrowing a closed select is how a screen reader user reads one, so neither the change nor a
   pause may write. One that only shows information, like the output chooser, acts at once.

   The probe never forwards a POST, so nothing here can reach the unit. What it proves is that
   browsing sends NOTHING, that Enter sends exactly one write, and that leaving or Escape puts the
   dropdown back. */

const el = (id) => document.getElementById(id);
const paths = (log) => log.map((e) => e.method + " " + e.path);
const ENTER_HINT = "enter-to-apply-hint";

async function apiLog() {
  return (await probeLog()).filter((e) => e.path.indexOf("/api/") === 0
                                          && e.path.indexOf("/api/ping") < 0);
}
const posts = async () => (await apiLog()).filter((e) => e.method === "POST");

async function waitFor(fn, ms) {
  const until = Date.now() + (ms || 20000);
  while (Date.now() < until) {
    try { if (fn()) return true; } catch (e) { /* not ready */ }
    await sleep(100);
  }
  return false;
}

const keydown = (sel, key) => {
  const ev = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
  sel.dispatchEvent(ev);
  return ev;
};
const arrowTo = (sel, value) => {
  keydown(sel, "ArrowDown");
  sel.value = value;
  sel.dispatchEvent(new Event("change", { bubbles: true }));
};

async function checkOne(sel, writePath) {
  const hint = el(sel.id + "-now");
  // A SELECT WITH ONE OPTION IS ALWAYS A BUG, never a skip. It exists to change something, so a
  // single option is a control that cannot be changed away from - and the way it happens is
  // silent: `setStagedValue` invents a "Value true" option for a device value whose label list
  // never arrived, which renders and reads as a real control.
  const start = sel.dataset.deviceValue;
  const other = [...sel.options].find((o) => o.value !== start);
  if (!other) {
    check(sel.id + ": offers more than one option", false,
          [...sel.options].map((o) => o.textContent).join(" | ") || "no options at all");
    return;
  }
  const described = (sel.getAttribute("aria-describedby") || "").split(/\s+/);
  // FIRST, so it is heard before a longer description rather than after it.
  check(sel.id + ": is described as applying on Enter, before anything else",
    described[0] === ENTER_HINT, sel.getAttribute("aria-describedby"));
  check(sel.id + ": no waiting line with nothing chosen",
        !hint || (hint.textContent || "").trim() === "", hint && hint.textContent);
  // A description whose text changes is re-read as you arrow, so the waiting line is page-only.
  check(sel.id + ": the waiting line is NOT announced between options",
        !hint || !described.includes(hint.id), sel.getAttribute("aria-describedby"));

  await probeClear();
  arrowTo(sel, other.value);
  await sleep(1200);                       // longer than any pause a timer would have taken
  check(sel.id + ": arrowing it writes NOTHING", (await posts()).length === 0,
        paths(await posts()).join(" "));
  if (hint) check(sel.id + ": the page says what the unit still has",
                  /still has/i.test(hint.textContent), hint.textContent);

  sel.dispatchEvent(new FocusEvent("blur"));
  check(sel.id + ": leaving without Enter puts it back", sel.value === start, sel.value);
  check(sel.id + ": and writes nothing", (await posts()).length === 0);

  arrowTo(sel, other.value);
  keydown(sel, "Escape");
  check(sel.id + ": Escape puts it back too", sel.value === start, sel.value);

  // AN EXPANDED LIST CLOSED WITH ESCAPE. Windows commits the highlighted option as the list closes,
  // so `change` arrives after the Escape keydown, with nothing staged when the key was pressed.
  await probeClear();
  keydown(sel, "Escape");
  sel.value = other.value;
  sel.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(800);
  check(sel.id + ": closing an expanded list with Escape writes NOTHING",
        (await posts()).length === 0, paths(await posts()).join(" "));
  keydown(sel, "Escape");
  check(sel.id + ": and a second Escape puts it back", sel.value === start, sel.value);

  await probeClear();
  arrowTo(sel, other.value);
  const enter = keydown(sel, "Enter");
  await sleep(800);
  const sent = await posts();
  check(sel.id + ": Enter writes, and writes ONCE",
    sent.length === 1 && (!writePath || sent[0].path.indexOf(writePath) === 0),
    paths(sent).join(" "));
  check(sel.id + ": Enter does not open the list on top", enter.defaultPrevented);

  // Put the page back: the probe stubbed the write, so the unit's value is unchanged.
  if (sel.isConnected) setStagedValue(sel, start, null);
}

async function run() {
  // TWO PAGES, ONE SUITE: mic type lives on Channels and the rest on Routing.
  // WAIT FOR THE ROUTER FIRST: every section starts hidden, so asking which page is showing before
  // the hash route has run picks the wrong one.
  await waitFor(() => !el("sec-routing").hidden || !el("sec-channels").hidden, 25000);
  if (!el("sec-routing").hidden) await runRouting();
  else await runChannels();
  await probeDone();
}

/* MIC TYPE matters most: choosing a voicing makes the DESK move that channel's gain, so an arrow
   press through the list would change the level as well as the sound. */
async function runChannels() {
  const found = await waitFor(() => document.querySelector("select[id^='mictype-']"), 25000);
  check("a mic channel offers a Mic type dropdown", found);
  if (!found) return;
  await checkOne(document.querySelector("select[id^='mictype-']"), "/api/channel/mic-type");
}

async function runRouting() {
  await waitFor(() => el("routing-fader-0"), 25000);
  await sleep(1500);

  // ---- Faders: each applies on its own, because a swap moves two ------
  check("a fader row has no Apply button", !el("routing-fader-0-apply"));
  await checkOne(el("routing-fader-0"), "/api/faders/set");

  // ---- The output mix mode --------------------------------------------
  el("tab-routing-mixes").click();
  await sleep(1200);
  check("the output mix mode has no Apply button", !el("matrix-mode-apply"));
  await checkOne(el("matrix-mode"), "/api/routing/outputmode");
  // The OUTPUT chooser beside it changes what you are looking at, so it acts at once.
  const outSel = el("matrix-output");
  check("but choosing WHICH output to configure is not described as needing Enter",
    !(outSel.getAttribute("aria-describedby") || "").includes(ENTER_HINT));

  // ---- Headphone type and USB audio -----------------------------------
  el("tab-routing-master").click();
  await sleep(1500);
  if (el("routing-hp-type-0")) await checkOne(el("routing-hp-type-0"), "/api/routing/headphone");
  if (el("routing-usb-mode")) await checkOne(el("routing-usb-mode"), "/api/routing/usb-multitrack");
  // Turning Expanded off makes the unit unassign every extended input from its fader, so an
  // accidental arrow press would cost a layout, not just a pause.
  if (el("routing-usb-expanded")) {
    // The receives write above holds the other USB dropdown until the server says the switch is
    // done, which the page learns from its driver poll. The poll skips a hidden page, and a
    // headless one counts as hidden, so this is what the poll does on hearing the switch finished.
    setUsbSwitching(false);
    await checkOne(el("routing-usb-expanded"), "/api/routing/usb-expanded");
  }
}

run().catch(async (e) => {
  check("the checks ran to the end", false, String((e && e.stack) || e));
  await probeDone();
});
