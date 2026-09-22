/* Machine checks for THE END-OF-PAGE HARD STOP (app.js bindPageBoundary).
   Run with: python DevTools/ui_probe.py --checks DevTools/ui_checks_boundary.js --route about

   WHAT IT IS. Tab on the last control of a page stays put and plays a short tone. Wrapping round
   to the top instead tells you nothing: you quietly find yourself somewhere else. Nothing is made
   unreachable by the stop, because headings, landmarks, the nav links, Alt with a digit and Home
   all still get you back up the page.

   WHAT THIS SUITE CANNOT PROVE, said plainly. The probe has no CDP, so every key here is a
   SYNTHETIC KeyboardEvent, and a browser never performs the default action for one - focus would
   not have moved even without our handler. So "focus did not move" is worthless here and is not
   asserted. What IS meaningful is whether the handler called preventDefault, which is exactly what
   a real Tab press obeys, so that is what every check below looks at. The stop actually being felt,
   and the tone actually being heard, is Shaun's NVDA pass.

   The route is About because it is the simplest page (prose only, reads nothing from the unit), but
   the handler is global, so this is about the mechanism rather than that page. */

const key = (target, opts) => {
  const ev = new KeyboardEvent("keydown", Object.assign(
    { key: "Tab", bubbles: true, cancelable: true }, opts || {}));
  target.dispatchEvent(ev);
  return ev;
};

async function waitFor(fn, ms) {
  const until = Date.now() + (ms || 8000);
  while (Date.now() < until) {
    try { if (fn()) return true; } catch (e) { /* not ready */ }
    await sleep(80);
  }
  return false;
}

async function run() {
  await waitFor(() => !document.getElementById("sec-about").hidden, 20000);
  await sleep(500);

  // ---- the list the stop is computed from -------------------------------
  check("the app exposes a tabbable-element list", typeof tabbableElements === "function");
  const list = tabbableElements();
  check("the page has tabbable controls to bound", list.length > 1, list.length);

  // Document order is tab order ONLY while no positive tabindex exists. If one is ever added this
  // whole feature points at the wrong element, so it is pinned here rather than left as a comment.
  const positive = Array.from(document.querySelectorAll("[tabindex]"))
    .filter((el) => Number(el.getAttribute("tabindex")) > 0);
  check("nothing uses a positive tabindex, so document order IS tab order",
    positive.length === 0, positive.map((el) => el.tagName + "#" + el.id).join(" "));

  check("nothing hidden is counted as tabbable",
    list.every((el) => !el.closest("[hidden]")));
  check("nothing disabled is counted as tabbable",
    list.every((el) => !el.disabled));
  check("nothing with a negative tabindex is counted as tabbable",
    list.every((el) => {
      const ti = el.getAttribute("tabindex");
      return ti === null || Number(ti) >= 0;
    }));

  // ---- the stop itself ---------------------------------------------------
  const last = list[list.length - 1];
  last.focus();
  check("the last control can take focus", document.activeElement === last,
    document.activeElement && document.activeElement.tagName);

  const atEnd = key(last);
  check("Tab on the LAST control is stopped", atEnd.defaultPrevented);

  // Shift+Tab is deliberately untouched: only the end of the page is bounded, so there is always a
  // way back through the controls one at a time.
  last.focus();
  const back = key(last, { shiftKey: true });
  check("Shift+Tab on the last control is NOT stopped", !back.defaultPrevented);

  // A modified Tab belongs to the browser (Ctrl+Tab changes browser tab), so we must not eat it.
  last.focus();
  check("Ctrl+Tab is left alone", !key(last, { ctrlKey: true }).defaultPrevented);
  check("Alt+Tab is left alone", !key(last, { altKey: true }).defaultPrevented);

  const first = list[0];
  first.focus();
  check("Tab anywhere ELSE is not stopped", !key(first).defaultPrevented);

  const middle = list[Math.floor(list.length / 2)];
  middle.focus();
  check("Tab in the middle of the page is not stopped", !key(middle).defaultPrevented);

  // ---- a modal's Tab cycle is CLOSED BY US, not by the browser -----------
  // THE WRAP IS OURS TO DO. showModal() does NOT keep Tab inside the dialog: it makes the rest of
  // the document inert, and tabbing past the dialog's last control leaves the document for the
  // browser's toolbar. Assuming otherwise is how a modal lets you tab straight out into that
  // toolbar, so these two pin the wrap rather than its absence.
  const dlg = document.createElement("dialog");
  const btnA = document.createElement("button");
  const btnB = document.createElement("button");
  btnA.textContent = "Boundary check A";
  btnB.textContent = "Boundary check B";
  dlg.appendChild(btnA);
  dlg.appendChild(btnB);
  document.body.appendChild(dlg);
  dlg.showModal();

  // THESE ARE THE ONE PLACE IN THIS SUITE WHERE activeElement IS WORTH ASSERTING, and the header's
  // warning does not apply. It says focus checks are worthless because a browser performs no default
  // action for a synthetic event - true for the STOP, which only calls preventDefault. The wrap is
  // different: it moves focus with an explicit .focus() call, which works synthetic or not. So where
  // focus lands is our behaviour rather than the browser's, and it is exactly what needs proving.
  btnB.focus();
  const fwd = key(btnB);
  check("Tab on a modal's LAST control is intercepted", fwd.defaultPrevented);
  check("...and wraps to its first control, not out to the browser",
    document.activeElement === btnA, document.activeElement && document.activeElement.textContent);

  btnA.focus();
  // NOT `back`: that name is taken by the page-end Shift+Tab check above, and a second `const back`
  // in the same scope is a SyntaxError, so the whole suite failed to parse and timed out without
  // running a single check (found, introduced with these modal checks on the
  // 3rd). A suite that cannot parse looks exactly like a hung page, so check the console first.
  const backInModal = key(btnA, { shiftKey: true });
  check("Shift+Tab on a modal's FIRST control is intercepted", backInModal.defaultPrevented);
  check("...and wraps to its last control",
    document.activeElement === btnB, document.activeElement && document.activeElement.textContent);

  // In the MIDDLE of a dialog the browser's own handling is correct and must be left alone.
  const mid = document.createElement("button");
  mid.textContent = "Boundary check mid";
  dlg.insertBefore(mid, btnB);
  mid.focus();
  check("Tab in the middle of a modal is left to the browser", !key(mid).defaultPrevented);
  check("and Shift+Tab in the middle likewise", !key(mid, { shiftKey: true }).defaultPrevented);
  mid.remove();

  // THE RECOVERY PATH, and the reason the wrap keys on "is focus inside the dialog" rather than
  // just "is it the last control". If focus has already escaped the dialog, by a click on the
  // browser toolbar or by tabbing out of it, the next Tab must pull it back IN rather than fire the
  // page-end stop, which would be a lie: the page behind a modal is inert, so its last control is
  // not the boundary that matters.
  last.focus();
  const recover = key(last);
  check("with a modal open, Tab from OUTSIDE it is intercepted", recover.defaultPrevented);
  check("...and pulls focus back into the dialog",
    dlg.contains(document.activeElement),
    document.activeElement && document.activeElement.textContent);
  dlg.close();
  dlg.remove();

  // Back to normal once the modal has gone.
  last.focus();
  check("the stop is back after the modal closes", key(last).defaultPrevented);

  // ---- the cue -----------------------------------------------------------
  // Headless has no audio device. All that can be checked here is that the cue is reachable and
  // cannot throw into the keydown handler; whether it SOUNDS right is an ear question.
  let threw = null;
  try { playBoundaryCue(); } catch (e) { threw = String(e); }
  check("the boundary cue runs without throwing", threw === null, threw);

  // ---- it follows the page, it is not baked in ---------------------------
  // The last control differs per page, so a stop computed once at startup would be wrong the moment
  // you navigate. Prove it recomputes by moving to a page with a different set of controls.
  location.hash = "#/routing";
  await waitFor(() => !document.getElementById("sec-routing").hidden, 20000);
  await sleep(700);
  const list2 = tabbableElements();
  check("the tabbable list is recomputed on another page", list2.length > 0, list2.length);
  const last2 = list2[list2.length - 1];
  last2.focus();
  check("the stop lands on the new page's own last control",
    document.activeElement === last2 && key(last2).defaultPrevented);
  const notLast2 = list2[0];
  notLast2.focus();
  check("the About page's last control is not still being treated as the end",
    !key(notLast2).defaultPrevented);

  await probeDone();
}

run().catch(async (e) => {
  check("the checks ran to the end", false, String((e && e.stack) || e));
  await probeDone();
});
