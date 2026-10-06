/* EVERY CONTROL A READ-ONLY DESK CANNOT DRIVE MUST STAY WHERE IT IS AND SAY WHY.

   This is the check for the eleven converted raw `disabled` sites. The raw
   attribute takes a control OUT OF THE TAB ORDER and out of the accessibility tree with it, so on a
   desk missing a write cap the page did not read as "you cannot change this" - it read as though the
   control had never existed, and the explanation went with it. aria-disabled keeps the control
   readable, keeps its label and its description, and lets pressing it say why.

   IT CANNOT BE DRIVEN FROM A DESK. All six gates (unit_settings, pad_write, fader_write,
   output_write, level_write, matrix_write) are ON for both the Pro II and the Duo, so no desk in
   existence reaches these branches. The payloads come from DevTools/make_readonly_stub.py, which
   takes what the attached desk really answers and flips only the gates.

   Run:
     python DevTools/make_readonly_stub.py
     python DevTools/ui_probe.py --checks DevTools/ui_checks_readonly.js \
         --stub DevTools/_probe_stub_readonly.json --route routing
*/

const el = (id) => document.getElementById(id);
const alertText = () => (el("alert-region") || {}).textContent || "";

async function waitFor(fn, ms) {
  const until = Date.now() + (ms || 10000);
  while (Date.now() < until) {
    try { if (fn()) return true; } catch (e) { /* not ready yet */ }
    await sleep(80);
  }
  return false;
}

async function goTo(route, sectionId) {
  location.hash = "#/" + route;
  await waitFor(() => el(sectionId) && !el(sectionId).hidden, 15000);
  await sleep(900);
}

/* Open one tab of a page's strip by its key, e.g. tab("routing", "levels"). */
async function tab(pageKey, key) {
  const b = el("tab-" + pageKey + "-" + key);
  if (!b) return false;
  b.click();
  await sleep(1400);          // the strip debounces its device read by 350ms
  return true;
}

/* THE WHOLE POINT, asserted per control: still here, still reachable, marked unavailable, and
   carrying a reason somebody can hear. A control that fails the second test is the exact fault this
   change was made to fix, so it is checked explicitly rather than inferred from the attribute. */
function blocked(label, node) {
  if (!node) { check(label + ": the control is on the page", false, "not found"); return; }
  check(label + ": not the raw disabled attribute", !node.disabled && !node.hasAttribute("disabled"),
    node.id);
  check(label + ": marked unavailable", node.getAttribute("aria-disabled") === "true", node.id);
  const why = node.dataset.offReason || "";
  check(label + ": and it says why", why.length > 10, why);
  // Reachable means focusable. A negative tabindex would be the same disappearance by another route.
  node.focus();
  check(label + ": can be reached with the keyboard", document.activeElement === node,
    document.activeElement && document.activeElement.id);
}

function blockedAll(label, nodes) {
  const list = Array.from(nodes);
  check(label + ": there are some to check", list.length > 0, list.length);
  const bad = list.filter((n) => n.disabled || n.getAttribute("aria-disabled") !== "true"
                                 || !(n.dataset.offReason || "").length);
  check(label + ": all " + list.length + " are unavailable in place, with a reason",
    bad.length === 0, bad.map((n) => n.id || n.tagName).join(","));
}

/* Every write the page attempted. `/api/ping` is the page's own keepalive heartbeat, posted on a
   timer whatever the user does, so it is not a write and must not count as one. */
async function writesSince() {
  return (await probeLog()).filter((e) => e.method === "POST" && e.path.indexOf("/api/ping") < 0);
}

/* Press it and prove NOTHING went to the device and the reason was spoken. The probe never forwards
   a POST, so the log is the honest record of what the page tried to write. */
async function pressAndProveSilent(label, node) {
  if (!node) return;
  await probeClear();
  el("alert-region").textContent = "";
  node.click();
  await sleep(900);
  const posts = await writesSince();
  check(label + ": pressing it writes nothing to the device", posts.length === 0,
    posts.map((p) => p.path).join(","));
  check(label + ": and it says why out loud", alertText().length > 10, alertText());
}

/* Drag it and prove it goes back to the unit's own number. A slider left where it was put would be
   reporting a level the desk is not at. */
async function moveAndProveReverts(label, node) {
  if (!node) return;
  await probeClear();
  const was = node.value;
  const target = String(Math.min(100, Math.max(0, parseInt(was, 10) + 25)));
  node.value = target;
  node.dispatchEvent(new Event("input", { bubbles: true }));
  await sleep(1200);
  check(label + ": moving it puts it straight back", node.value === was,
    "was " + was + ", asked for " + target + ", ended at " + node.value);
  const posts = await writesSince();
  check(label + ": and nothing was written", posts.length === 0,
    posts.map((p) => p.path).join(","));
  check(label + ": and it says why out loud", alertText().length > 10, alertText());
}

async function run() {
  // ---- Routing: faders --------------------------------------------------------------------
  await goTo("routing", "sec-routing");
  await tab("routing", "faders");

  // `select[...]`, not `[...]`: each staged row also carries a hint SPAN whose id ends "-now", and
  // a hint is not a control.
  const faderSels = document.querySelectorAll("select[id^='routing-fader-']");
  blockedAll("fader layout dropdowns", faderSels);
  // The note on this tab promises you can still see which channel is on each fader. With the raw
  // attribute that was untrue, so it is asserted rather than assumed.
  const first = faderSels[0];
  check("a fader dropdown still reports which channel is on it",
    !!(first && first.options[first.selectedIndex]
       && first.options[first.selectedIndex].textContent.trim().length),
    first && first.options[first.selectedIndex] && first.options[first.selectedIndex].textContent);
  blocked("reset faders", el("faders-reset-btn"));
  await pressAndProveSilent("reset faders", el("faders-reset-btn"));

  // ---- Routing: master output settings ----------------------------------------------------
  await tab("routing", "master");
  blocked("master Compellor switch", el("routing-compellor-on"));
  blocked("output delay switch", el("routing-delay-on"));
  blocked("output delay milliseconds", el("routing-delay-ms"));
  blockedAll("compellor parameters",
    document.querySelectorAll("input[id^='routing-compellor-']"));
  blockedAll("headphone type dropdowns",
    document.querySelectorAll("select[id^='routing-hp-type-']"));

  await probeClear();
  el("alert-region").textContent = "";
  const comp = el("routing-compellor-on");
  const wasOn = comp.checked;
  comp.click();
  await sleep(900);
  check("flipping the Compellor switch writes nothing", (await writesSince()).length === 0);
  check("and the switch goes back to what the unit has", comp.checked === wasOn,
    "was " + wasOn + ", now " + comp.checked);
  check("and it says why out loud", alertText().length > 10, alertText());

  // ---- Routing: output mixes --------------------------------------------------------------
  await tab("routing", "mixes");
  blocked("reset all output mixes", el("reset-outputs-btn"));
  await pressAndProveSilent("reset all output mixes", el("reset-outputs-btn"));

  // ---- Routing: levels and the monitor mix ------------------------------------------------
  await tab("routing", "levels");
  blockedAll("fader level sliders", document.querySelectorAll("[id^='fader-level-']"));
  blocked("monitor speaker level", el("monitor-level"));
  await moveAndProveReverts("monitor speaker level", el("monitor-level"));
  blocked("reset this output's mix", el("monmix-reset-btn"));
  await pressAndProveSilent("reset this output's mix", el("monmix-reset-btn"));
  const monmix = document.querySelectorAll("[id^='monmix-'][type='range']");
  blockedAll("monitor mix source sliders", monmix);
  await moveAndProveReverts("a monitor mix source slider", monmix[0]);

  // ---- Device page: unit settings ---------------------------------------------------------
  await goTo("status", "sec-status");
  await tab("device", "unit");
  const unit = Array.from(document.querySelectorAll("#dev-unit-controls input, "
                                                    + "#dev-unit-controls select"));
  blockedAll("unit settings controls", unit);
  // The description is the half the raw attribute took away, so it is checked by itself.
  // aria-describedby can name several ids (a dropdown also carries the Enter-to-apply hint), so
  // the setting's own explanation is found among them rather than taken as the whole attribute.
  const described = unit.filter((n) => (n.getAttribute("aria-describedby") || "").split(/\s+/)
    .some((id) => id.endsWith("-hint") && el(id) && el(id).textContent.trim().length > 0));
  check("every unit setting still carries its explanation",
    described.length === unit.length, described.length + " of " + unit.length);

  const sel = unit.find((n) => n.tagName === "SELECT");
  if (sel) {
    await probeClear();
    el("alert-region").textContent = "";
    const wasVal = sel.value;
    const other = Array.from(sel.options).find((o) => o.value !== wasVal);
    if (other) {
      sel.value = other.value;
      sel.dispatchEvent(new Event("change", { bubbles: true }));
      await sleep(900);
      check("changing a unit setting writes nothing", (await writesSince()).length === 0);
      check("and the dropdown goes back to what the unit has", sel.value === wasVal,
        "was " + wasVal + ", now " + sel.value);
      check("and it says why out loud", alertText().length > 10, alertText());
    }
  }

  // ---- SMART pads -------------------------------------------------------------------------
  await goTo("pads", "sec-pads");
  const padWrite = ["pad-ed-type", "pad-ed-apply-btn", "pad-ed-name", "pad-ed-name-btn",
                    "pad-ed-volume", "pad-ed-clear-btn", "clear-all-btn", "restore-fx-btn"]
    .map(el).filter(Boolean);
  blockedAll("pad write controls", padWrite);
  // Export only READS the unit, so it must stay usable: this is the control that matters most on a
  // desk you cannot write to, and blanket-disabling the page is how it would be lost.
  const exportBtn = el("export-pad-btn");
  check("exporting a pad's sound is still offered, because it only reads",
    !!exportBtn && exportBtn.getAttribute("aria-disabled") !== "true");
  await pressAndProveSilent("clear all pads", el("clear-all-btn"));

  // ---- The headline invariant, across every page just visited -----------------------------
  const raw = Array.from(document.querySelectorAll("[disabled]"))
    .map((n) => n.id || n.tagName);
  check("NOTHING anywhere is left in the raw disabled state", raw.length === 0, raw.join(","));

  await probeDone();
}

run().catch(async (e) => {
  check("the checks ran to the end", false, String((e && e.stack) || e));
  await probeDone();
});
