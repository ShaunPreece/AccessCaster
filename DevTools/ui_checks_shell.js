/* THE APP SHELL: the header heading, where focus starts, and the desk line beneath it.
   Run with: python DevTools/ui_probe.py --checks DevTools/ui_checks_shell.js --route status

   WHAT IT PINS, and why each one is worth a check rather than a comment:

   - FOCUS LANDS ON THE APP'S OWN HEADING WHEN THE WINDOW OPENS, not on the first page's heading
     and not inside the nav list. Nothing else in the app asserts where a fresh window puts you, so
     a later tidy-up of render() could quietly move it and nobody would find out except by opening
     the app.
   - THE VERSION IS IN THE HEADING TEXT BEFORE FOCUS ARRIVES. A heading whose text changes is not
     re-read by a screen reader, so filling it in after the fact means the number is never heard.
     That is a timing bug no static reading of the code would catch.
   - THE DESK LINE IS A LIVE REGION AND NOT PART OF THE HEADING. Finding the unit is a USB
     enumeration, so its answer always arrives late; a live region is read when it changes and a
     heading is not. */

const el = (id) => document.getElementById(id);

async function waitFor(fn, ms) {
  const until = Date.now() + (ms || 15000);
  while (Date.now() < until) {
    try { if (fn()) return true; } catch (e) { /* not ready */ }
    await sleep(80);
  }
  return false;
}

async function run() {
  await waitFor(() => el("app-title"), 20000);

  // ---- the heading itself ------------------------------------------------
  const title = el("app-title");
  check("the app has its own heading in the header", !!title && title.tagName === "H1",
    title && title.tagName);
  check("it is OUTSIDE main, so it is chrome rather than page content",
    !!title && !title.closest("main"));
  check("it is focusable programmatically but NOT a tab stop",
    title.getAttribute("tabindex") === "-1", title.getAttribute("tabindex"));
  check("it is not hidden from screen readers",
    title.getAttribute("aria-hidden") === null, title.getAttribute("aria-hidden"));

  // The version has to be THERE, not on its way. See the header comment.
  await waitFor(() => /version \d/.test(el("app-title").textContent), 5000);
  // "version 2.50" SPELLED OUT, never "v2.50": a screen reader reads that v as "volt".
  check("the heading names the app and carries the version, spelled out",
    /^AccessCaster version \d+\.\d+$/.test(title.textContent.trim()), title.textContent.trim());

  // ---- where focus starts ------------------------------------------------
  check("a fresh window puts focus on that heading",
    document.activeElement === title,
    document.activeElement && (document.activeElement.id || document.activeElement.tagName));

  // ---- the desk line -----------------------------------------------------
  const dev = el("app-device");
  check("the desk line exists and is a live region", !!dev && dev.getAttribute("role") === "status",
    dev && dev.getAttribute("role"));
  check("it sits after the heading, so it is read in that order",
    !!(title.compareDocumentPosition(dev) & Node.DOCUMENT_POSITION_FOLLOWING));
  check("the desk is NOT part of the heading text, which would never be re-read",
    !/RODECaster/i.test(title.textContent), title.textContent.trim());
  await waitFor(() => !/Looking for/i.test(el("app-device").textContent), 20000);
  check("it ends up naming the desk rather than the placeholder",
    /connected|found|unreachable/i.test(dev.textContent), dev.textContent.trim());
  check("and it is the SHORT form, not the Device page's sentence",
    !/serial|hardware id/i.test(dev.textContent), dev.textContent.trim());

  // ---- the nav is still one step away ------------------------------------
  const firstNav = document.querySelector('nav[aria-label="Main"] .navlink');
  check("the nav follows the heading in document order, so Tab reaches it next",
    !!(title.compareDocumentPosition(firstNav) & Node.DOCUMENT_POSITION_FOLLOWING));
  check("the page's own h1 is still there underneath, unchanged",
    !!document.querySelector("#sec-status > h1[tabindex='-1']"));

  // ---- the spoken startup summary ----------------------------------------
  // WORDING, PINNED. It is assembled from parts that arrive in different shapes (some ending in a
  // full stop, some not, some lower case) and two faults got through a read-through by eye:
  // a doubled full stop and a lower-case sentence in the middle. Both are inaudible in code review
  // and obvious in the ear, which is exactly what a check is for.
  const clear = summariseOverview(
    {ok: true, muted: [], soloed: [], routing: {ok: true, notes: []}, bank: 1});
  check("a clean desk says so in one sentence, and names the bank",
    clear === "Nothing is muted or soloed and every output is on its usual mix. SMART pad bank 1.",
    clear);
  const mutes = summariseOverview({ok: true, muted: [{label: "Mic 3"}, {label: "Mic 4"}],
                                   soloed: [], routing: {ok: true, notes: []}, bank: 1});
  // The tail is the point: told only what is wrong, you cannot tell a clean solo bus from a summary
  // that stopped at the first problem.
  check("with something muted it still says solo and routing are clear",
    mutes === "Muted: Mic 3, Mic 4. Nothing is soloed and every output is on its usual mix. "
            + "SMART pad bank 1.", mutes);
  const busy = summariseOverview({ok: true, muted: [{label: "Mic 2"}],
                                  routing: {ok: true, notes: ["Chat is on a custom mix."]}, bank: 3});
  check("a routing note is one sentence, not two full stops",
    busy.indexOf("..") < 0 && busy.indexOf("Chat is on a custom mix.") >= 0, busy);
  check("nothing is said at all when there is no desk",
    summariseOverview({ok: false}) === "", summariseOverview({ok: false}));

  await probeDone();
}

run().catch(async (e) => {
  check("the checks ran to the end", false, String((e && e.stack) || e));
  await probeDone();
});
