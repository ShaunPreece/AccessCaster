// probe: route=settings timeout=120
/* AccessCaster's own update, on the page: the Updates tab of App settings, the decision
   dialog, and the progress dialog it hands over to. Run:
     python DevTools/run_ui_probes.py updates

   Nothing is downloaded or installed: every POST is stubbed by the probe, and the job the progress
   dialog polls is answered here by wrapping `fetch`, one stage at a time. */

const el = (id) => document.getElementById(id);

const OFFER = {
  version: "9.99.0",
  title: "Install AccessCaster version 9.99.0?",
  summary: "You are running AccessCaster version 9.98.0. Version 9.99.0 was released today.",
  notes: "What is new in AccessCaster 9.99.0\n\nA heading\nA body.",
  after: "Installing closes AccessCaster for a moment.",
  downloading: "Downloading the update. This can take a minute.",
};

// The job the progress dialog sees, set by each step below.
let job = null;
const realFetch = window.fetch.bind(window);
window.fetch = (url, opts) => {
  if (String(url).indexOf("/api/update/job") === 0) {
    return Promise.resolve(new Response(JSON.stringify({ ok: true, job: job })));
  }
  return realFetch(url, opts);
};
const jobAt = (stage, message, percent) => ({
  stage: stage, message: message, percent: percent === undefined ? null : percent,
  version: OFFER.version, not_back: "It has not come back.",
});

async function posted(path) {
  return (await probeLog()).some((e) => e.method === "POST" && e.path.indexOf(path) === 0);
}

async function run() {
  await waitFor(() => el("sec-settings") && !el("sec-settings").hidden, 20000);
  await waitFor(() => el("tab-settings-updates"), 15000);
  el("tab-settings-updates").click();
  await sleep(800);

  // ---- The controls on the Updates tab ---------------------------------------------------
  const sel = el("update-days");
  check("the Updates tab has a heading", el("h-updates") && el("h-updates").tagName === "H2");
  check("the dropdown is labelled", /check for updates/i.test(accessibleName(sel)),
    accessibleName(sel));
  await waitFor(() => sel.options.length > 0, 8000);
  check("it offers the four choices, never and all", sel.options.length === 4,
    sel.options.length);
  check("its hint says how to look now", /F12/.test(describedText(sel)), describedText(sel));
  check("the button names F12 as its shortcut",
    el("update-check-btn").getAttribute("aria-keyshortcuts") === "F12");

  // ---- F12 asks, and the browser does not get it -------------------------------------------
  await probeClear();
  const f12 = new KeyboardEvent("keydown", { key: "F12", bubbles: true, cancelable: true });
  document.body.dispatchEvent(f12);
  check("F12 is taken from the browser", f12.defaultPrevented);
  check("and checks for an update", await waitFor(() => posted("/api/update/check"), 4000));
  await sleep(300);

  // ---- The decision -----------------------------------------------------------------------
  const dlg = el("update-dialog");
  check("the decision is a labelled alertdialog", dlg.getAttribute("role") === "alertdialog"
    && el(dlg.getAttribute("aria-labelledby")) && el(dlg.getAttribute("aria-describedby")));
  const trigger = el("update-check-btn");
  trigger.focus();
  openUpdateDialog(OFFER);
  check("it opens", dlg.open);
  checkEqual("its name is the question", flatText(el("update-title")), OFFER.title);
  checkEqual("its description is the two versions", flatText(el("update-summary")),
    OFFER.summary);
  const notes = el("update-notes");
  check("focus lands in the release notes", document.activeElement === notes,
    document.activeElement && document.activeElement.id);
  check("which are a read-only text box", notes.tagName === "TEXTAREA" && notes.readOnly);
  check("labelled What changed", /what changed/i.test(accessibleName(notes)),
    accessibleName(notes));
  checkEqual("holding the notes", notes.value, OFFER.notes);
  ["update-install", "update-skip", "update-later"].forEach((id) => {
    check(id + " is a real button", el(id).tagName === "BUTTON" && el(id).type === "button");
  });
  el("update-later").click();
  await sleep(150);
  check("Later closes it", !dlg.open);
  check("and focus goes back where it was", document.activeElement === trigger,
    document.activeElement && document.activeElement.id);

  // ---- Install hands over to the progress dialog, then a failure brings Close --------------
  const prog = el("update-progress-dialog");
  const btn = el("update-progress-btn");
  job = jobAt("downloading", OFFER.downloading, 40);
  await probeClear();
  openUpdateDialog(OFFER);
  el("update-install").click();
  check("Install closes the decision", !dlg.open);
  check("and opens the progress dialog in the same moment", prog.open);
  checkEqual("with its first line already there", flatText(el("update-progress-step")),
    OFFER.downloading);
  check("the bar is a native progress bar", el("update-progress").tagName === "PROGRESS");
  check("focus is on Cancel", document.activeElement === btn && flatText(btn) === "Cancel");
  check("the install names the version the dialog showed",
    await waitFor(async () => (await probeLog()).some((e) => e.method === "POST"
      && e.path === "/api/update/install"
      && /9\.99\.0/.test(JSON.stringify(e.body || ""))), 4000),
    JSON.stringify((await probeLog()).filter((e) => e.method === "POST")));
  await sleep(600);
  checkEqual("the bar follows the download", el("update-progress").value, 40);
  job = jobAt("checking", "Checking the download.", 100);
  await sleep(700);
  checkEqual("the check of the download is said", flatText(el("update-progress-step")),
    "Checking the download.");
  job = jobAt("failed", "The downloaded update did not match.", 100);
  await sleep(1300);
  check("a failure turns Cancel into Close", flatText(btn) === "Close" && !btn.hidden);
  check("and focuses it", document.activeElement === btn);
  checkEqual("and says why", flatText(el("update-progress-step")),
    "The downloaded update did not match.");
  checkEqual("the bar keeps what it reached", el("update-progress").value, 100);
  btn.click();
  await sleep(150);
  check("Close closes it", !prog.open);
  check("and focus goes back where it was", document.activeElement === trigger,
    document.activeElement && document.activeElement.id);

  // ---- Cancel while downloading ------------------------------------------------------------
  job = jobAt("downloading", OFFER.downloading, 10);
  await probeClear();
  openUpdateDialog(OFFER);
  el("update-install").click();
  await sleep(600);
  btn.click();
  check("Cancel asks the server to stop", await waitFor(() => posted("/api/update/cancel"), 3000));
  job = jobAt("cancelled", "The update was cancelled.");
  await sleep(800);
  check("a cancelled download closes the dialog", !prog.open);
  check("and says so", await waitFor(() => /was cancelled/.test(el("live-region").textContent),
    2000), el("live-region").textContent);

  // ---- Ready: nothing to press, the sentence, then proceed ---------------------------------
  job = jobAt("downloading", OFFER.downloading, 90);
  await probeClear();
  openUpdateDialog(OFFER);
  el("update-install").click();
  await sleep(500);
  job = jobAt("ready", "AccessCaster will now close. Please wait.", 100);
  await sleep(1300);
  check("at the ready point there is nothing to press", btn.hidden);
  check("and focus is on the heading", document.activeElement === el("update-progress-title"));
  checkEqual("the closing sentence is said", flatText(el("update-progress-step")),
    "AccessCaster will now close. Please wait.");
  check("proceed waits for it to be read", !(await posted("/api/update/proceed")));
  check("then the installer is asked for",
    await waitFor(() => posted("/api/update/proceed"), 6000));
  await sleep(200);
  check("the page knows the outage that follows is the update", updateState.handedOff);
  const lost = el("server-lost-dialog");
  const before = { seen: serverWatch.seen, misses: serverWatch.misses };
  noteServer(null); noteServer(null); noteServer(null);
  check("so the server-lost dialog does not open over it", !lost.open);
  serverWatch.misses = before.misses;
  window.clearTimeout(updateState.backTimer);
  updateState.handedOff = false;
  prog.close();

  await probeDone();
}

probeRun(run);
