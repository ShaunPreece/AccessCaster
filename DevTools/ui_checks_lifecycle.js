/* The page watching its server. Run:
     python DevTools/ui_probe.py --checks DevTools/ui_checks_lifecycle.js --route device

   Drives `noteServer` with made-up heartbeat replies rather than stopping a real server, so it
   proves the page's side: a blip is not an outage, an outage opens ONE modal, the same version
   coming back closes it and says so, and a different version reloads (the reload itself is stubbed,
   since a reload would end this check). */

const el = (id) => document.getElementById(id);

async function run() {
  await sleep(1500);
  const dlg = el("server-lost-dialog");
  check("the outage dialog is a labelled alertdialog",
    dlg && dlg.getAttribute("role") === "alertdialog"
      && el(dlg.getAttribute("aria-labelledby")) && el(dlg.getAttribute("aria-describedby")));
  check("it starts closed", !dlg.open);

  const seen = serverWatch.seen;
  check("the first ping recorded which server this page came from",
    seen === null || (typeof seen.instance === "string" && typeof seen.version === "string"),
    JSON.stringify(seen));
  serverWatch.seen = { instance: "run-a", version: "9.99" };
  serverWatch.misses = 0;

  noteServer(null);
  noteServer(null);
  check("two missed pings are a blip, not an outage", !dlg.open);
  noteServer(null);
  await sleep(100);
  check("the third opens the dialog", dlg.open);
  check("focus is on its button", document.activeElement === el("server-lost-ok"));

  el("server-lost-ok").click();
  await sleep(100);
  check("OK closes it", !dlg.open);
  noteServer(null);
  await sleep(100);
  check("and it does not reopen during the same outage", !dlg.open);

  noteServer({ ok: true, instance: "run-b", version: "9.99" });
  await sleep(200);
  check("the same version back says so",
    /AccessCaster is back/.test(el("alert-region").textContent), el("alert-region").textContent);
  check("and does not reload", serverWatch.reloading === false);

  noteServer({ ok: true });
  check("a reply with no run id is ignored", serverWatch.seen.instance === "run-b");

  const realSetTimeout = window.setTimeout;
  let reloadQueued = false;
  window.setTimeout = (fn, ms) => { if (ms === 2500) { reloadQueued = true; return 0; }
                                    return realSetTimeout(fn, ms); };
  noteServer({ ok: true, instance: "run-c", version: "10.00" });
  window.setTimeout = realSetTimeout;
  await sleep(200);
  check("a new version reloads the page", reloadQueued && serverWatch.reloading === true);
  check("and says why first", /updated to version 10\.00/.test(el("alert-region").textContent),
    el("alert-region").textContent);

  await probeDone();
}

run().catch(async (e) => {
  check("the checks ran to the end", false, String((e && e.stack) || e));
  await probeDone();
});
