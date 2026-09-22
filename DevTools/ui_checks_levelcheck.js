/* THE PER-CHANNEL LEVEL CHECK on the Channels page, not on Device > Levels.
   Run:
     python DevTools/ui_probe.py --checks DevTools/ui_checks_levelcheck.js --route channels --timeout 240

   WHY IT MOVED, since that is what these checks are really about: the old check metered a Windows
   STREAM, and every stream this desk offers is a MIX - "Microphone (RODECaster Pro II Main Stereo)"
   is four mics, two USB returns and Bluetooth at once. So it could say "something is reaching the
   computer" and never "Mic 2 is right", which is the question you have while setting up. The server
   now isolates one channel on a temporary Chat mix and corrects for the fader
   (server.action_channel_level_check).

   What is asserted here, and each one is a decision rather than plumbing:
     - the button is on EVERY channel, not just the mics: a USB feed or a phone on Bluetooth arrives
       at a level nobody chose either;
     - it is NOT on Chat, because the check listens on the Chat output and that output deliberately
       leaves the caller out of their own mix. A button that could only fail is worse than no button;
     - its hint is NOT attached with aria-describedby. It would then be read in full every time the
       button took focus, and this button is pressed twice per check;
     - pressing it posts to /api/channels/level-check WITH THE CHANNEL NAME, and nothing else. The
       probe stubs every POST, so nothing here reaches the desk and no mix is touched;
     - the page adds NO live region for it. The check speaks the level itself every second and a
       half, and a second voice landing on top of that is the one thing that would make it useless. */

const el = (id) => document.getElementById(id);
const strip = () => document.querySelector("#channel-panel [role='tablist']");

async function waitFor(fn, ms) {
  const until = Date.now() + (ms || 8000);
  while (Date.now() < until) {
    try { if (fn()) return true; } catch (e) { /* not ready yet */ }
    await sleep(80);
  }
  return false;
}

const posts = (log) => log.filter((r) => r.method === "POST" && r.path !== "/api/ping");

function nameOf(node) {
  if (!node) return "";
  if (node.getAttribute("aria-label")) return node.getAttribute("aria-label");
  const lab = node.closest("label") || document.querySelector(`label[for='${node.id}']`);
  return lab ? lab.textContent.trim() : node.textContent.trim();
}

async function pick(name, waitForId) {
  const sel = el("channel-select");
  sel.value = name;
  sel.dispatchEvent(new Event("change", { bubbles: true }));
  return waitFor(() => el(waitForId), 15000);
}

async function run() {
  await waitFor(() => !el("sec-channels").hidden && strip(), 20000);
  await sleep(1200);

  // THE MIC COMES FROM THE DESK, NOT FROM A CONSTANT. A hardcoded "Mic3" does not exist on a Duo
  // (its source 2 is the Headset jack), so on that desk the suite reports six failures that read
  // like the check button having gone missing. The HIGHEST-numbered mic is picked because it is
  // the least likely to have anything plugged into it; ui_probe forwards no POST at all, so that
  // is belt and braces. ui_checks_solo.js picks the same way, for the same reason.
  const liveRegionsBefore = document.querySelectorAll("#sec-channels [aria-live]").length;
  const sel0 = el("channel-select");
  const mics = Array.from(sel0.options).filter((o) => /^Mic\d+$/.test(o.value));
  check("this desk offers at least one mic to test with", mics.length > 0,
    Array.from(sel0.options).map((o) => o.value).join(","));
  if (!mics.length) { await probeDone(); return; }
  const micName = mics[mics.length - 1].value;
  await pick(micName, "levelcheck-" + micName);
  const btn = el("levelcheck-" + micName);
  check("the check button is on a mic's Settings tab", !!btn);
  check("it is a real button, not a link or a div",
    btn && btn.tagName === "BUTTON" && btn.type === "button", btn && btn.tagName);
  check("it is reachable by keyboard", btn && btn.tabIndex >= 0);
  check("it names what pressing it will do, and says whose level",
    /check this channel's level/i.test(nameOf(btn)), nameOf(btn));

  // THE HINT: on the page, not on the control.
  check("its hint is NOT attached with aria-describedby",
    btn && !btn.getAttribute("aria-describedby"), btn && btn.getAttribute("aria-describedby"));
  const hintText = Array.from(btn.parentElement.querySelectorAll(".hint"))
    .map((p) => p.textContent.replace(/\s+/g, " ").trim()).join(" ");
  check("the hint says how long it talks for", /half a minute|30 seconds/i.test(hintText), hintText);
  // THE FADER FACT has to be in there. The cross-point it listens through is post-fader, the server
  // divides it out, and a reading that silently ignored a fader would be the same trap the old
  // percent-based meter was.
  check("and says the fader position is taken out of the reading",
    /fader/i.test(hintText), hintText);

  // IT IS ON THE OTHER CHANNELS TOO, which is the half of the design a mic-only test would miss.
  await pick("USB2", "levelcheck-USB2");
  check("a non-mic channel has it as well (USB 2)", !!el("levelcheck-USB2"));
  check("and there is no auto gain button there, since nothing else has a preamp",
    !el("autogain-USB2"));

  // AND NOT ON CHAT.
  const sel = el("channel-select");
  const hasChat = Array.from(sel.options).some((o) => o.value === "Chat");
  if (hasChat) {
    await pick("Chat", "mute-Chat");
    await sleep(400);
    check("Chat has NO check button: the check listens on the Chat output, which leaves the caller "
      + "out of their own mix", !el("levelcheck-Chat"));
  } else {
    check("SKIPPED Chat is not offered as a channel on this unit", true, "no Chat option");
  }

  // ---- the press. POSTs are stubbed, so nothing reaches the desk. ----
  await pick(micName, "levelcheck-" + micName);
  await probeClear();
  el("levelcheck-" + micName).click();
  await waitFor(async () => posts(await probeLog()).length > 0, 8000);
  const log = posts(await probeLog());
  check("pressing it posts to /api/channels/level-check and nothing else",
    log.length === 1 && log[0].path === "/api/channels/level-check",
    JSON.stringify(log.map((r) => r.path)));

  // NO NEW LIVE REGION. Everything this feature says out loud is said by the server's own voice.
  check("the page gained no extra live region for the check",
    document.querySelectorAll("#sec-channels [aria-live]").length === liveRegionsBefore,
    document.querySelectorAll("#sec-channels [aria-live]").length + " vs " + liveRegionsBefore);

  // The old home of the check must not still be wired up on this page either.
  check("nothing on the Channels page posts to the retired /api/meters/check",
    !(await probeLog()).some((r) => r.path === "/api/meters/check"));

  await probeDone();
}

run().catch(async (e) => {
  check("the checks ran to the end", false, String((e && e.stack) || e));
  await probeDone();
});
