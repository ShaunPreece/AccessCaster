/* The SMART pads bus on the Channels page: it is a channel, and it must not offer controls it does
   not have. It has MUTE and SOLO and nothing else. NO level (that is its fader), no mic voicing, no
   preamp, and - tested by ear on both desks - NO PROCESSING either. The strip PUBLISHES a whole
   chain and accepts every write to it, which is what tempts an app into offering an equaliser here;
   RODE's own guide and their app both say the SMART pads take no processing, and the ears agree. A
   dead control is worse than a missing one for a screen reader: it is read out, focused, operated,
   and nothing happens.
   Run: python DevTools/ui_probe.py --checks DevTools/ui_checks_padsbus.js --route channels */

const el = (id) => document.getElementById(id);
const strip = () => document.querySelector("#channel-panel [role='tablist']");
const buttons = () => Array.from(strip().querySelectorAll("[role='tab']"));
const titles = () => buttons().map((b) => b.textContent.trim());

async function waitFor(fn, ms) {
  const until = Date.now() + (ms || 8000);
  while (Date.now() < until) {
    try { if (fn()) return true; } catch (e) { /* not ready */ }
    await sleep(80);
  }
  return false;
}

async function pick(name) {
  const sel = el("channel-select");
  sel.value = name;
  sel.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(700);
  return sel.value;
}

async function run() {
  await waitFor(() => !el("sec-channels").hidden && strip(), 20000);
  await sleep(1200);

  const opts = Array.from(el("channel-select").options).map((o) => o.value);
  check("the SMART pads bus is offered as a channel", opts.includes("SMARTpads"),
    opts.join(","));
  const label = (Array.from(el("channel-select").options)
    .find((o) => o.value === "SMARTpads") || {}).textContent;
  check("and it is named in words, not as a key", /SMART pads/i.test(label || ""), label);

  check("selected it", (await pick("SMARTpads")) === "SMARTpads");
  check("it has a tab strip like any other channel", !!strip(), titles().join(" / "));
  check("no Presets tab (mic voicings are a mic thing)",
    !titles().some((t) => /preset/i.test(t)), titles().join(" / "));
  check("no Effects tab (echo and reverb are mic-only today)",
    !titles().some((t) => /effect/i.test(t)), titles().join(" / "));
  check("no Processing tab: the unit applies none to the pads",
    !titles().some((t) => /processing/i.test(t)), titles().join(" / "));
  check("no Equaliser tab", !titles().some((t) => /equalis/i.test(t)), titles().join(" / "));
  check("no Dynamics tab", !titles().some((t) => /dynamics/i.test(t)), titles().join(" / "));
  check("no Tone and pan tab", !titles().some((t) => /tone/i.test(t)), titles().join(" / "));
  // ONE tab is not a tab strip worth arrowing along, and that is the point: everything the bus has
  // is on the first pane.
  check("just the one tab, Channel settings", titles().length === 1, titles().join(" / "));

  // The Channel settings tab: mute and solo yes, level no.
  buttons()[0].click();
  await sleep(400);
  check("mute is there", !!el("mute-SMARTpads"));
  check("solo is there", !!el("solo-SMARTpads"));
  check("NO level slider, because a bus has no input gain", !el("level-SMARTpads"));
  check("no phantom power switch", !el("p48-SMARTpads"));
  check("no polarity switch", !el("pol-SMARTpads"));
  check("no mic type list", !el("mictype-SMARTpads"));

  // Every control on the panel must be reachable: nothing left disabled and unexplained.
  const dead = Array.from(document.querySelectorAll("#channel-panel [disabled]"))
    .map((n) => n.id || n.tagName);
  check("nothing on the panel is left in the raw disabled state", dead.length === 0,
    dead.join(","));

  // NOTHING from the chain is anywhere on the panel, not even switched off. The whole point is that
  // it is absent, so a check for "present but disabled" would pass the wrong thing.
  check("no master processing switch", !el("proc-on-SMARTpads"));
  const sliders = document.querySelectorAll(
    "#channel-panel input[type='range']:not([hidden])");
  check("no parameter sliders at all", sliders.length === 0, sliders.length);

  // A mic still looks like a mic: taking the chain off the BUS must not have taken it off anything
  // else, which is the regression that would matter most here.
  check("back on Mic 1", (await pick("Mic1")) === "Mic1");
  check("a mic still has its level slider", !!el("level-Mic1"));
  check("a mic still has its voicing list", !!el("mictype-Mic1"));
  check("a mic STILL has its Processing tab", titles().some((t) => /processing/i.test(t)),
    titles().join(" / "));
  check("and its Equaliser", titles().some((t) => /equalis/i.test(t)), titles().join(" / "));
  const eqBtn = buttons().find((b) => /equaliser/i.test(b.textContent));
  if (eqBtn) {
    eqBtn.click();
    await sleep(500);
    check("with real parameter sliders on it",
      document.querySelectorAll("#tabpanel-channels input[type='range']:not([hidden])").length > 0);
  }

  await probeDone();
}

run().catch(async (e) => {
  check("the checks ran to the end", false, String((e && e.stack) || e));
  await probeDone();
});
