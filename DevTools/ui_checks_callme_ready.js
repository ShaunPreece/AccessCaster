/* The CallMe page with NOTHING ready: offline, CallMe 1 on no fader, hosting off. Build the stub
   that says so, then run with it:
     python DevTools/make_callme_unready_stub.py
     python DevTools/ui_probe.py --checks DevTools/ui_checks_callme_ready.js --route callme \
         --stub DevTools/_probe_stub_callme_unready.json --timeout 240

   WHAT IS WORTH ASSERTING: the page is the Not ready section alone, each problem is listed with
   its fix beside it, hosting off is not one of them (a call out still works), the fix posts
   exactly the right thing, and after a fix focus lands on the section heading, because the fix's
   own control has gone. The probe stubs every POST, so nothing here reaches the desk. */

const el = (id) => document.getElementById(id);

async function waitFor(fn, ms) {
  const until = Date.now() + (ms || 8000);
  while (Date.now() < until) {
    try { if (fn()) return true; } catch (e) { /* not ready yet */ }
    await sleep(80);
  }
  return false;
}

const posts = (log) => log.filter((r) => r.method === "POST" && r.path !== "/api/ping");

async function run() {
  await waitFor(() => el("sec-callme") && !el("sec-callme").hidden, 20000);
  await waitFor(() => !el("callme-gate").hidden, 25000);
  await sleep(1500);            // the fader list is filled by a read of its own

  check("the page is the Not ready section alone",
    !el("callme-gate").hidden && el("callme-main").hidden);
  check("under its own heading", el("callme-h-ready").textContent.trim() === "Not ready for calls");
  const items = Array.from(el("callme-ready-list").children).map((li) => li.textContent.trim());
  check("the summary says how many things need sorting",
    /2 things need sorting/i.test(el("callme-ready-summary").textContent),
    el("callme-ready-summary").textContent);
  check("and lists them, offline first, hosting not among them",
    items.length === 2 && /offline/i.test(items[0]) && !items.some((t) => /hosting/i.test(t)),
    items.join(" | "));
  check("the list is named by the section heading",
    el("callme-ready-list").getAttribute("aria-labelledby") === "callme-h-ready");

  const link = el("callme-ready-wifi-link");
  check("offline: a link to Wireless connections is shown",
    !el("callme-ready-wifi").hidden && link.getAttribute("href") === "#/status",
    link.textContent.trim());

  const sel = el("callme-fader-select");
  const lab = document.querySelector("label[for='callme-fader-select']");
  check("no fader: a fader dropdown with a label is shown",
    !el("callme-ready-fader").hidden && /fader for callme 1/i.test(lab ? lab.textContent : ""));
  check("it starts on None, which is what the unit has",
    sel.value === "none" && sel.options[0].text === "None", sel.value);
  check("then lists every fader with what it carries now",
    sel.options.length >= 10 && /^Fader 1 is currently /.test(sel.options[1].text),
    Array.from(sel.options).map((o) => o.text).join(" | "));
  check("and is an Enter-to-apply dropdown, with no button of its own",
    (sel.getAttribute("aria-describedby") || "").split(" ")[0] === "enter-to-apply-hint"
      && !el("callme-ready-fader").querySelector("button"));

  // ---- arrowing posts nothing; Enter posts, and focus goes to the heading ----
  await probeClear();
  const last = sel.options.length - 2;
  sel.focus();
  sel.value = String(last);
  sel.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(600);
  check("choosing a fader from the keyboard posts nothing yet",
    posts(await probeLog()).length === 0);
  sel.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  await waitFor(async () => posts(await probeLog()).length > 0, 10000);
  let log = posts(await probeLog());
  check("Enter posts CallMe 1's code to the chosen fader",
    log.length === 1 && log[0].path === "/api/faders/set"
      && JSON.stringify(log[0].body) === JSON.stringify({ fader: last, code: 16 }),
    JSON.stringify(log));
  await waitFor(() => document.activeElement === el("callme-h-ready"), 8000);
  check("then focus is on the Not ready heading", document.activeElement === el("callme-h-ready"),
    document.activeElement && document.activeElement.id);
  await waitFor(() => /CallMe 1 is on fader/.test(el("callme-state").textContent), 3000);
  check("and the outcome is worded for this page, not as a layout report",
    new RegExp(`^CallMe 1 is on fader ${last + 1}\\.`).test(el("callme-state").textContent),
    el("callme-state").textContent);

  await probeDone();
}

run().catch(async (e) => {
  check("the checks ran to the end", false, String((e && e.stack) || e));
  await probeDone();
});
