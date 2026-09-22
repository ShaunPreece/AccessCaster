/* THE STAGED DROPDOWNS: every control that changes the DESK'S AUDIO waits for an Apply.
   Run with: python DevTools/ui_probe.py --checks DevTools/ui_checks_staged.js --route routing

   THE RULE THIS GUARDS: a dropdown that changes the AUDIO needs an Apply button; one that only
   shows information, like the pad selector, can act instantly. A CLOSED select fires `change` on
   every arrow press, and arrowing a closed select is how a screen reader user reads one - so
   without the gate, browsing the list IS the action.

   The probe never forwards a POST, so nothing here can reach the unit. What it proves is that
   changing a dropdown sends NOTHING, and that the Apply button is the only thing that would. */

const el = (id) => document.getElementById(id);
const paths = (log) => log.map((e) => e.method + " " + e.path);

async function apiLog() {
  return (await probeLog()).filter((e) => e.path.indexOf("/api/") === 0
                                          && e.path.indexOf("/api/ping") < 0);
}

async function waitFor(fn, ms) {
  const until = Date.now() + (ms || 20000);
  while (Date.now() < until) {
    try { if (fn()) return true; } catch (e) { /* not ready */ }
    await sleep(100);
  }
  return false;
}

/* Every staged dropdown on the page, by the ids the helper gives them. */
function stagedRows() {
  return [...document.querySelectorAll("select[id]")]
    .map((sel) => ({ sel, btn: el(sel.id + "-apply"), hint: el(sel.id + "-now") }))
    .filter((r) => r.btn && r.hint);
}

async function checkOne(row, writePath) {
  const { sel, btn, hint } = row;
  const other = [...sel.options].find((o) => o.value !== sel.value);
  if (!other) { check("SKIP " + sel.id + ": only one option", true); return; }

  check(sel.id + ": Apply is unavailable with nothing staged",
    btn.getAttribute("aria-disabled") === "true", btn.getAttribute("aria-disabled"));
  check(sel.id + ": no hint repeating what the dropdown already says",
    (hint.textContent || "").trim() === "", hint.textContent);
  check(sel.id + ": its Apply button names what it applies to",
    btn.textContent.trim().length > "Apply".length, btn.textContent.trim());

  await probeClear();
  sel.value = other.value;
  sel.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(1200);                       // longer than the old 500 ms auto-commit
  const wrote = (await apiLog()).filter((e) => e.method === "POST");
  check(sel.id + ": arrowing it writes NOTHING", wrote.length === 0, paths(wrote).join(" "));
  check(sel.id + ": Apply becomes available", btn.getAttribute("aria-disabled") !== "true");
  check(sel.id + ": the hint now says what the unit still has",
    /still has/i.test(hint.textContent), hint.textContent);
  // AND THE HINT IS NOT SPOKEN WHILE YOU ARROW. A description is re-read as the
  // value changes, so linking it announced "not applied yet" between every option. The text is on
  // the page; the Apply button is what says there is something waiting.
  check(sel.id + ": the hint is NOT announced between options",
    (sel.getAttribute("aria-describedby") || "").indexOf(hint.id) < 0,
    sel.getAttribute("aria-describedby"));

  await probeClear();
  btn.click();
  await sleep(800);
  const sent = (await apiLog()).filter((e) => e.method === "POST");
  check(sel.id + ": Apply is what writes, and it writes ONCE",
    sent.length === 1 && (!writePath || sent[0].path.indexOf(writePath) === 0),
    paths(sent).join(" "));

  // Put the page back: the probe stubbed the write, so the unit's value is unchanged.
  sel.value = sel.dataset.deviceValue;
  sel.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(150);
}

async function run() {
  // TWO PAGES, ONE SUITE: mic type lives on Channels and the rest on Routing, and the rule they
  // follow is the same one. Run it with --route routing and again with --route channels.
  // WAIT FOR THE ROUTER FIRST: every section starts hidden, so asking which page is showing before
  // the hash route has run picks the wrong one (it did, and ran the Channels half twice).
  await waitFor(() => !el("sec-routing").hidden || !el("sec-channels").hidden, 25000);
  if (!el("sec-routing").hidden) await runRouting();
  else await runChannels();
  await probeDone();
}

/* MIC TYPE is the most important of the lot: choosing a voicing makes the DESK move that channel's
   gain, so an arrow press through the list changed the level as well as the sound. */
async function runChannels() {
  const found = await waitFor(() => [...document.querySelectorAll("select[id^='mictype-']")]
    .some((s) => el(s.id + "-apply")), 25000);
  check("a mic channel offers a staged Mic type with its own Apply", found);
  if (!found) return;
  const sel = document.querySelector("select[id^='mictype-']");
  await checkOne({ sel, btn: el(sel.id + "-apply"), hint: el(sel.id + "-now") },
                 "/api/channel/mic-type");
  // The mic PRESET list beside it has had an Apply since before this rule was written down, for
  // exactly the same reason. It is not built by the shared helper, so it is checked by name.
  const preset = document.querySelector("select[id^='preset-apply-']");
  if (preset) {
    check("the mic preset list also waits for its own Apply",
      !!document.querySelector("button[id^='preset-apply-btn']")
      || !!el(preset.id + "-btn"), preset.id);
  }
}

async function runRouting() {
  await waitFor(() => el("routing-fader-0"), 25000);
  await sleep(1500);

  // ---- Faders (one per fader, because a swap moves two) ----------------
  const fader = { sel: el("routing-fader-0"), btn: el("routing-fader-0-apply"),
                  hint: el("routing-fader-0-now") };
  check("a fader row has a dropdown, a hint and its own Apply",
    !!fader.sel && !!fader.btn && !!fader.hint);
  await checkOne(fader, "/api/faders/set");

  // ---- The output mix mode --------------------------------------------
  el("tab-routing-mixes").click();
  await sleep(1200);
  const modeRow = { sel: el("matrix-mode"), btn: el("matrix-mode-apply"),
                    hint: el("matrix-mode-now") };
  check("the output mix mode has an Apply", !!modeRow.btn);
  await checkOne(modeRow, "/api/routing/outputmode");
  // The OUTPUT chooser beside it is deliberately NOT staged: it changes what you are looking at.
  check("but choosing WHICH output to configure has no Apply, because it changes nothing on the "
    + "unit", !el("matrix-output-apply"));

  // ---- Headphone type and USB audio -----------------------------------
  el("tab-routing-master").click();
  await sleep(1500);
  const hp = { sel: el("routing-hp-type-0"), btn: el("routing-hp-type-0-apply"),
               hint: el("routing-hp-type-0-now") };
  if (hp.sel) {
    check("headphone type has an Apply", !!hp.btn);
    await checkOne(hp, "/api/routing/headphone");
  }
  const usb = { sel: el("routing-usb-mode"), btn: el("routing-usb-mode-apply"),
                hint: el("routing-usb-mode-now") };
  if (usb.sel) {
    check("what this computer receives has an Apply", !!usb.btn);
    await checkOne(usb, "/api/routing/usb-multitrack");
  }
}

run().catch(async (e) => {
  check("the checks ran to the end", false, String((e && e.stack) || e));
  await probeDone();
});
