// probe: route=profiles timeout=90
/* THIS APP's OWN SETTINGS, the export/import section on the Profiles page (built).

   It sits on the Profiles page and NOT inside a profile, and the first check here is that the two
   are told apart: a profile is the DESK, this is the APP, and someone arriving to solve "I am moving
   to another computer" has to be able to tell which button they want. Everything else is the usual
   pair of questions - can a screen reader reach and understand it, and does pressing something
   dangerous ask first.

   POSTs are stubbed by the probe, so nothing here writes a file or touches settings.

   Run: python DevTools/run_ui_probes.py appsettings */

const el = (id) => document.getElementById(id);

async function run() {
  await waitFor(() => el("sec-profiles") && !el("sec-profiles").hidden, 20000);
  await waitFor(() => el("appsettings-export-btn"), 15000);
  await sleep(600);

  // ---- It is its own section, and it says it is about the APP -----------------------------
  const heading = el("h-appsettings");
  check("it has a heading of its own", !!heading);
  check("the heading is level 2, like every other on the page",
    heading && heading.tagName === "H2", heading && heading.tagName);
  // The section's own note: the first role="note" paragraph after the heading (the start-at-sign-in
  // switch sits between them).
  let noteEl = heading && heading.nextElementSibling;
  while (noteEl && !(noteEl.tagName === "P" && noteEl.getAttribute("role") === "note")) {
    noteEl = noteEl.nextElementSibling;
  }
  const note = flatText(noteEl);
  check("the note says these are the APP's settings, not the desk's",
    /no profile carries them|belong to AccessCaster/i.test(note), note);
  check("and it says outright that no desk settings are in the file",
    /nothing about the desk/i.test(note), note);
  // The reason a user can trust emailing it to themselves has to be ON the page, not just in a
  // reply they may never hear.
  check("and that it holds no remote-access token", /no remote-access token/i.test(note), note);

  // ---- Everything is reachable and named --------------------------------------------------
  const ids = ["appsettings-export-btn", "appsettings-open-btn", "appsettings-import-btn"];
  ids.forEach((id) => {
    const b = el(id);
    check(id + " is a real button", !!b && b.tagName === "BUTTON" && b.type === "button",
      b && b.tagName);
    check(id + " is not natively disabled", !!b && !b.disabled && !b.hasAttribute("disabled"));
    check(id + " has a name that says what it does", flatText(b).length > 6, flatText(b));
  });
  check("the export button says WHAT it saves, not just 'Export'",
    /settings/i.test(flatText(el("appsettings-export-btn"))), flatText(el("appsettings-export-btn")));

  const picker = el("appsettings-file");
  check("the file picker exists", !!picker && picker.type === "file");
  check("and it has a real label", accessibleName(picker).length > 0, accessibleName(picker));
  const described = picker && el(picker.getAttribute("aria-describedby"));
  check("and an explanation attached with aria-describedby", !!described,
    picker && picker.getAttribute("aria-describedby"));
  check("which warns that an unavailable device is left alone",
    /not got|left alone/i.test(flatText(described)), flatText(described));

  // ---- The result line is READ, and announce() is the one voice ---------------------------
  // A live region here as well as announce() would speak every result twice (index.html says why).
  const state = el("appsettings-state");
  check("there is a line for the result", !!state);
  check("and it is NOT a live region: announce() speaks the result",
    !!state && !state.hasAttribute("aria-live") && !state.getAttribute("role"),
    state && (state.getAttribute("aria-live") || state.getAttribute("role")));

  // ---- Pressing Restore with nothing chosen says so, and writes nothing --------------------
  await probeClear();
  el("appsettings-import-btn").click();
  await sleep(700);
  check("restoring with no file chosen writes nothing", pagePosts(await probeLog()).length === 0);
  check("and it says to choose a file first", /choose a settings file/i.test(flatText(state)),
    flatText(state));
  check("and focus goes to the picker so you can", document.activeElement === picker,
    document.activeElement && document.activeElement.id);

  // ---- Export is a plain action; it only ADDS a file, so it must NOT confirm ---------------
  await probeClear();
  el("appsettings-export-btn").click();
  await sleep(900);
  const dlg = document.querySelector("dialog[open]");
  check("saving does not stop to confirm, because it only writes a file", !dlg,
    dlg && dlg.id);
  const posted = pagePosts(await probeLog()).map((p) => p.path);
  check("and it posts to the export endpoint",
    posted.indexOf("/api/appsettings/export") >= 0, posted.join(","));

  await probeDone();
}

probeRun(run);
