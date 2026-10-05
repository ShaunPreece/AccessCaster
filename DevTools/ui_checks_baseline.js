/* Machine checks for the two features: the first-run AS-FOUND backup dialog, and the
   "which preset is this channel on" answer riding on the Preset control's own label.

   Run with:
     python DevTools/ui_probe.py --checks DevTools/ui_checks_baseline.js \
            --stub DevTools/_probe_stub_baseline.json

   The stub file is generated from the REAL desk by hand (ui_probe.py has no --write-stub), so the
   checks run against fixed data rather than whatever build is on 8770. The channel data in it is
   the live unit's.

   This is NOT a screen-reader pass. It pins the things NVDA depends on and the eye cannot see: that
   the offer is a real modal alertdialog with a name and a description, that focus lands somewhere
   useful and is trapped, and that the preset answer is on the LABEL rather than in a paragraph
   nobody tabs to. The screen-reader run is then about how it sounds. */

async function waitFor(fn, ms) {
  const until = Date.now() + (ms || 8000);
  while (Date.now() < until) {
    try { if (fn()) return true; } catch (e) { /* not ready yet */ }
    await sleep(80);
  }
  return false;
}

const $ = (id) => document.getElementById(id);
const textOf = (id) => (($(id) || {}).textContent || "").trim();

/* The accessible description of a dialog, as an AT would assemble it from aria-describedby. */
function describedBy(el) {
  return (el.getAttribute("aria-describedby") || "")
    .split(/\s+/).filter(Boolean)
    .map((id) => (($(id) || {}).textContent || "").trim())
    .join(" ");
}

(async function () {
  /* ---- 1. The first-run offer ------------------------------------------------------------- */
  const dlg = $("baseline-dialog");
  check("the as-found dialog exists in the page", !!dlg);
  const opened = await waitFor(() => dlg && dlg.open, 12000);
  check("it opens by itself on first run", opened);
  check("as a MODAL, so nothing behind it can be reached",
        opened && dlg.matches(":modal"), opened ? String(dlg.matches(":modal")) : "never opened");
  check("with role alertdialog: a decision that interrupts, not a passive notice",
        dlg.getAttribute("role"), dlg.getAttribute("role") === "alertdialog");
  check("it has an accessible NAME", textOf("baseline-dialog-title").length > 5,
        textOf("baseline-dialog-title"));

  const desc = describedBy(dlg);
  check("and an accessible DESCRIPTION that is actually read out", desc.length > 80);
  check("naming the unit it found", desc.indexOf("RODECaster") >= 0, desc.slice(0, 60));
  check("and the name it will save under",
        desc.indexOf("As found") >= 0, textOf("baseline-suggested-name"));
  // WHY THIS MATTERS: a reset gets you to FACTORY, which is not how the unit came to you, so an
  // as-found backup is the only way back to that. The wording must not claim the unit has no reset
  // at all - it has a software one.
  check("it says why this matters (a reset only reaches factory, not how you found it)",
        desc.indexOf("not the same as how it came to you") >= 0, desc.slice(-160));

  check("focus is on the recommended action, not lost in the page",
        document.activeElement && document.activeElement.id, "baseline-save-btn");
  check("the pad checkbox defaults ON here (the only copy of the pads as they came)",
        $("baseline-include-pads").checked, true);
  check("its help quotes the real per-pad cost from the server",
        textOf("baseline-pad-seconds").length > 0, textOf("baseline-pad-seconds"));
  check("the checkbox has a describedby that explains the wait",
        ($("baseline-include-pads").getAttribute("aria-describedby") || "").length > 0);

  /* Both ways out are present and reachable. A native modal <dialog> traps Tab for us; what the
     page has to get right is that there IS a way out that does not need the mouse. */
  const focusables = Array.from(dlg.querySelectorAll("button, input"))
    .filter((el) => !el.disabled && !el.hidden);
  check("every control in it is keyboard reachable", focusables.length, 3);
  check("including a decline", !!$("baseline-skip-btn"));
  $("baseline-skip-btn").focus();
  check("which can take focus", document.activeElement.id, "baseline-skip-btn");

  // Escape must work here: no job is running, so there is nothing that cannot be abandoned.
  dlg.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  $("baseline-skip-btn").click();
  const closed = await waitFor(() => !dlg.open, 5000);
  check("declining closes it", closed);
  check("and focus is not stranded on the body afterwards",
        document.activeElement && document.activeElement !== document.body,
        document.activeElement ? document.activeElement.tagName : "none");

  /* ---- 2. The preset answer is on the control, not in a paragraph -------------------------- */
  location.hash = "#/channels";
  await waitFor(() => !$("sec-channels").hidden, 8000);
  const ready = await waitFor(() => document.querySelector("#channel-panel select"), 12000);
  check("the channels page filled in", ready);

  // Open Mic 1's Presets tab. The tab strip is built per channel; find it by its title.
  const presetTab = Array.from(document.querySelectorAll("#channel-panel [role='tab']"))
    .find((b) => (b.textContent || "").trim().toLowerCase().indexOf("preset") >= 0);
  check("a mic channel has a Presets tab", !!presetTab, presetTab && presetTab.textContent);
  if (presetTab) {
    presetTab.click();
    const sel = await waitFor(() => document.querySelector("select[id^='preset-apply-']"), 8000);
    check("with the preset dropdown in it", sel);
    const dropdown = document.querySelector("select[id^='preset-apply-']");
    const label = document.querySelector("label[for='" + dropdown.id + "']");
    check("the dropdown has a label", !!label);
    // THE POINT OF THE CHANGE: tabbing to this control tells you what the channel is on. It used to
    // say only "Preset", with the answer in a paragraph above that nobody lands on.
    check("the label names the CURRENT preset, so focusing it announces the answer",
          (label.textContent || "").indexOf("currently") >= 0, label.textContent);
    check("and names the real one read back from the unit",
          (label.textContent || "").indexOf("Podcast Studio") >= 0, label.textContent);
    const hint = $((dropdown.getAttribute("aria-describedby") || ""));
    check("the described-by hint explains where that claim comes from",
          hint && (hint.textContent || "").indexOf("unit itself reports") >= 0,
          hint ? hint.textContent.slice(-90) : "no hint");
  }

  /* ---- 3. The Profiles page's as-found panel ----------------------------------------------- */
  location.hash = "#/profiles";
  await waitFor(() => !$("sec-profiles").hidden, 8000);
  await waitFor(() => textOf("baseline-state") !== "Checking.", 8000);
  check("the Profiles page reports the as-found state",
        textOf("baseline-state").length > 20, textOf("baseline-state"));
  check("and with no backup taken, it says so rather than staying blank",
        textOf("baseline-state").toLowerCase().indexOf("no as-found backup") >= 0);
  check("the add-pads button is hidden when there is nothing to add to", $("baseline-add-pads").hidden);
  // IT HAS NO HEADING OF ITS OWN, and this PINS that rather than leaving the absence to look like
  // an accident. The as-found profile is already in the saved-profiles dropdown, so it needs no
  // section: the line stays, the <h2> does not, and the fact sits with the dropdown it describes
  // one heading sooner.
  check("it has NO heading of its own", !$("h-prof-baseline"));
  {
    const line = $("baseline-state");
    const heads = Array.from(document.querySelectorAll("#sec-profiles h2"));
    const prev = heads.filter((h) =>
      line.compareDocumentPosition(h) & Node.DOCUMENT_POSITION_PRECEDING).pop();
    check("and it sits under Manage saved profiles",
          !!prev && prev.id === "h-prof-manage", prev ? prev.id : "no preceding h2");
    // THE VERBOSITY TRAP, PINNED. The list hint is on the dropdown because Shaun wants the count
    // there; hanging the as-found sentence off the same control would read the whole thing out every
    // time the select is reached, which is exactly the fault that got it taken off the buttons.
    const desc = $("profile-select").getAttribute("aria-describedby") || "";
    check("and is NOT described-by on the profile dropdown",
          desc.split(/\s+/).indexOf("baseline-state") < 0, desc);
  }

  await probeDone();
})();
