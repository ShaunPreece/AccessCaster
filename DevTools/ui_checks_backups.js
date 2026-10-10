/* AccessCaster - browser checks for the BACKUPS page (Alt+5).
 *
 * ONE page, ONE list. The shape this replaces - two headings, two lists and two "back up" buttons,
 * one set for profiles on this computer and one for the unit's own shows - put the two mechanisms
 * in the user's face and still could not answer which of them would actually put the desk back.
 *
 * WHAT IS PINNED HERE IS THE PROMISE, not the plumbing: one list, one save, one restore, one
 * delete, and a page that never makes the user learn the word "show". Run with a stub for
 * /api/backups when the attached build predates the endpoint:
 *
 *   python DevTools/ui_probe.py --checks DevTools/ui_checks_backups.js --route profiles \
 *       --stub <a file mapping /api/backups to a payload>
 */
(async function () {
  const el = (id) => document.getElementById(id);
  const textOf = (id) => (el(id) ? el(id).textContent.trim() : "");
  const labelFor = (node) => {
    const l = node && document.querySelector(`label[for='${node.id}']`);
    return l ? l.textContent.trim() : "";
  };
  const waitFor = async (fn, ms) => {
    const end = Date.now() + (ms || 5000);
    while (Date.now() < end) {
      try { if (fn()) return true; } catch (e) { /* not built yet */ }
      await sleep(80);
    }
    return false;
  };

  await waitFor(() => el("profile-select"), 30000);
  await waitFor(() => el("profile-select").options.length > 0, 8000);
  await waitFor(() => textOf("backup-state") !== "Checking.", 8000);

  // ---- one of everything -------------------------------------------------
  check("there is ONE backups list, not one per mechanism",
    document.querySelectorAll("#sec-profiles select").length === 1,
    document.querySelectorAll("#sec-profiles select").length + " selects in the section");
  check("the old separate Shows list is gone", !el("show-select"));
  check("and so is its own back-up button", !el("show-export-btn"));
  check("the list has a real label", labelFor(el("profile-select")).length > 0,
    labelFor(el("profile-select")));
  check("one button backs the unit up", !!el("profile-save-open"),
    el("profile-save-open") && el("profile-save-open").textContent.trim());
  ["profile-restore-open", "profile-rename-open", "profile-delete-open"].forEach((id) => {
    check(`${id} exists and says what it acts on`,
      !!el(id) && /backup/i.test(el(id).textContent), el(id) && el(id).textContent.trim());
  });

  // ---- the page never says "profile" or "show" at the user ---------------
  // The mechanisms are the page's business. The words are allowed in the HELP, where the trade is
  // explained, but not in the controls somebody has to operate.
  const controlText = Array.from(
    document.querySelectorAll("#sec-profiles button, #sec-profiles label, #sec-profiles h1, "
                              + "#sec-profiles h2"))
    .map((n) => n.textContent).join(" ");
  check("no control on the page calls a backup a profile or a show",
    !/\bprofiles?\b/i.test(controlText) && !/\bshows?\b/i.test(controlText),
    controlText.replace(/\s+/g, " ").slice(0, 200));

  // ---- the kinds, and what the page says about each ----------------------
  const opts = Array.from(el("profile-select").options);
  check("every entry in the list is selectable by a stable key",
    opts.every((o) => /^(profile:|show:)/.test(o.value)),
    opts.map((o) => o.value).join(","));
  const unitOpt = opts.find((o) => /^show:/.test(o.value));
  if (unitOpt) {
    check("a backup the unit made says so in its own line",
      /made on the unit/i.test(unitOpt.text), unitOpt.text);
    el("profile-select").value = unitOpt.value;
    el("profile-select").dispatchEvent(new Event("change", { bubbles: true }));
    await sleep(150);
    check("choosing it explains what it can and cannot do",
      /seconds/i.test(textOf("backup-detail")) && /cannot be renamed/i.test(textOf("backup-detail")),
      textOf("backup-detail"));
    el("profile-rename-open").click();
    await sleep(150);
    check("and renaming it is refused rather than half-working",
      !document.querySelector("#rename-dialog[open]"));
  } else {
    check("a unit-made backup was in the list to test", false, "none in the stub");
  }

  const profOpt = opts.find((o) => /^profile:/.test(o.value));
  if (profOpt) {
    el("profile-select").value = profOpt.value;
    el("profile-select").dispatchEvent(new Event("change", { bubbles: true }));
    await sleep(150);
    check("choosing one held on this computer says where it is held",
      /this computer/i.test(textOf("backup-detail")), textOf("backup-detail"));
  }

  // ---- the state line is about the half the user can change --------------
  check("the page says whether the unit is keeping its own copy",
    /this computer/i.test(textOf("backup-state")), textOf("backup-state"));

  // ---- the factory reset kept its place ----------------------------------
  check("the factory reset is still here and still separate", !!el("show-reset-btn"));
  check("and it still says whether it can be undone",
    textOf("reset-undo-note").length > 0, textOf("reset-undo-note"));

  // ---- nothing was written to the device by any of this ------------------
  const log = await probeLog();
  const writes = log.filter((r) => r.method === "POST" && !/\/api\/(ping|backup\/plan)$/.test(r.path));
  check("reading the page writes nothing to the unit", writes.length === 0,
    writes.map((w) => w.path).join(","));

  probeDone();
})();
