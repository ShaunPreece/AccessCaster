/* The CallMe page. Run:
     python DevTools/ui_probe.py --checks DevTools/ui_checks_callme.js --route callme --timeout 240

   IT NEEDS A SERVER THAT HAS /api/callme, so start DevTools/run_dev_server.py first.

   WHAT IS WORTH ASSERTING:
     1. ONE live region. A ringing call is spoken by the announcer; the page's status line speaks
        what its own buttons did. Two regions would talk over each other.
     2. The incoming call comes FIRST, ahead of the settings: it is the one thing with a deadline.
     3. Answer and Decline never leave the tab order. With nothing ringing they are aria-disabled
        and say why when pressed, and they post NOTHING.
     4. Entering the page reads the unit once, and after that only the free live poll runs.
   The probe stubs every POST, so nothing here reaches the desk. */

const el = (id) => document.getElementById(id);
const sec = () => el("sec-callme");

async function waitFor(fn, ms) {
  const until = Date.now() + (ms || 8000);
  while (Date.now() < until) {
    try { if (fn()) return true; } catch (e) { /* not ready yet */ }
    await sleep(80);
  }
  return false;
}

const posts = (log) => log.filter((r) => r.method === "POST" && r.path !== "/api/ping");
const gets = (log, path) => log.filter((r) => r.method === "GET" && r.path.split("?")[0] === path);

function nameOf(node) {
  if (!node) return "";
  if (node.getAttribute("aria-label")) return node.getAttribute("aria-label");
  const lab = node.closest("label") || document.querySelector(`label[for='${node.id}']`);
  return lab ? lab.textContent.trim() : node.textContent.trim();
}

async function run() {
  await waitFor(() => sec() && !sec().hidden, 20000);
  await waitFor(() => !/Reading the unit/i.test(el("callme-state").textContent), 25000);
  await sleep(600);
  const ringingNow = / is calling\./.test(el("callme-now").textContent);

  // ---- the nav --------------------------------------------------------------
  const link = document.querySelector("a[data-route='callme']");
  // The link also carries a hidden "5 of 8" position (applyItemPositions), so match the start.
  check("CallMe is its own main menu item", !!link && /^CallMe\b/.test(link.textContent.trim()),
    link && link.textContent);
  check("on Alt+5", link && link.getAttribute("aria-keyshortcuts") === "Alt+5");
  const order = Array.from(document.querySelectorAll("a[data-route]")).map((a) => a.dataset.route);
  check("between Recording and Backups",
    order.indexOf("callme") === order.indexOf("recording") + 1
      && order.indexOf("profiles") === order.indexOf("callme") + 1, order.join(","));
  const keys = Array.from(document.querySelectorAll("a[data-route]"))
    .map((a) => a.getAttribute("aria-keyshortcuts"));
  check("the shortcuts still follow the list, Alt+1 to Alt+8",
    keys.join(",") === "Alt+1,Alt+2,Alt+3,Alt+4,Alt+5,Alt+6,Alt+7,Alt+8", keys.join(","));

  // ---- shape ----------------------------------------------------------------
  const h1 = sec().querySelector("h1");
  check("one h1, focusable so the router can land on it",
    h1 && h1.getAttribute("tabindex") === "-1" && h1.textContent.trim() === "CallMe");
  const live = sec().querySelectorAll("[aria-live]");
  check("ONE live region on the page", live.length === 1 && live[0].id === "callme-state",
    Array.from(live).map((n) => n.id).join(","));
  const headings = Array.from(sec().querySelectorAll("h2")).map((h) => h.textContent.trim());
  check("the call comes first", /^call$/i.test(headings[0] || ""), headings.join(" | "));

  // ---- controls -------------------------------------------------------------
  const answer = el("callme-answer");
  const end = el("callme-end");
  check("Answer is a native button", answer && answer.tagName === "BUTTON");
  check("the second button is a native button", end && end.tagName === "BUTTON");
  check("TWO buttons in the call area, no more",
    sec().querySelector("#callme-h-call + div").querySelectorAll("button").length === 2);
  check("the second button is named for what it will do",
    end.textContent.trim() === (ringingNow ? "Decline" : "Hang up"), end.textContent);
  check("neither uses the disabled attribute, so neither leaves the tab order",
    !answer.disabled && !end.disabled);
  check("the details list is hidden with no call up, or filled with one",
    el("callme-details").hidden === (el("callme-details").children.length === 0));
  const host = el("callme-hosting");
  check("hosting is a labelled checkbox",
    host && host.type === "checkbox" && /accept incoming calls/i.test(nameOf(host)), nameOf(host));
  check("hosting says what switching it costs",
    /new code/i.test((el(host.getAttribute("aria-describedby")) || {}).textContent || ""));
  for (const id of ["callme-code", "callme-link"]) {
    const box = el(id);
    check(`${id} is labelled and read-only`, box && box.readOnly && nameOf(box).length > 0,
      nameOf(box));
  }
  check("the plan line is filled in", /CallMe/.test(el("callme-plan").textContent),
    el("callme-plan").textContent);
  check("the plan is one fact to a line", el("callme-plan").children.length >= 2,
    el("callme-plan").children.length);
  const code = el("callme-dial-code");
  check("the code to call is a labelled text box",
    code && code.type === "text" && /code/i.test(nameOf(code)), nameOf(code));
  const call = el("callme-dial");
  check("Call is the form's submit button, so Enter places the call",
    call && call.type === "submit" && call.form && call.form.id === "callme-dial-form");

  // ---- with nothing ringing -------------------------------------------------
  if (!ringingNow) {
    check("Answer is marked unavailable", answer.getAttribute("aria-disabled") === "true");
    await probeClear();
    answer.click();
    await sleep(700);
    let log = posts(await probeLog());
    check("pressing it posts nothing", log.length === 0, JSON.stringify(log.map((r) => r.path)));
    await waitFor(() => /no call is ringing/i.test(el("callme-state").textContent), 2000);
    check("and says why, in the page's own live region",
      /no call is ringing/i.test(el("callme-state").textContent), el("callme-state").textContent);
  }

  // ---- reads ----------------------------------------------------------------
  await probeClear();
  await sleep(2500);
  const log = await probeLog();
  check("staying on the page only polls the free live read",
    gets(log, "/api/callme").length === 0 && gets(log, "/api/callme/live").length >= 1,
    JSON.stringify(log.map((r) => r.path)));

  await probeDone();
}

run().catch(async (e) => {
  check("the checks ran to the end", false, String((e && e.stack) || e));
  await probeDone();
});
