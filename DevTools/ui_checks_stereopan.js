/* Pan on the Channels page, mono and stereo. Run:
     python DevTools/ui_probe.py --checks DevTools/ui_checks_stereopan.js --route channels --timeout 240

   A stereo channel pans with channelPanL / channelPanR, one per leg; channelPan only pans a mono
   one and does nothing on a stereo source. So:
     - a mic has ONE Pan slider, spoken as a direction ("centre"), never as a bare percent;
     - USB 1 has TWO leg sliders and no channelPan slider, plus a Link switch;
     - linked, one Pan slider writes BOTH legs, shifted together from where they were;
     - the probe stubs POSTs, so nothing reaches the desk. */

const el = (id) => document.getElementById(id);
const strip = () => document.querySelector("#channel-panel [role='tablist']");
const posts = (log) => log.filter((r) => r.method === "POST" && r.path.startsWith("/api/"));

async function waitFor(fn, ms) {
  const until = Date.now() + (ms || 8000);
  while (Date.now() < until) {
    try { if (await fn()) return true; } catch (e) { /* not ready yet */ }
    await sleep(80);
  }
  return false;
}

async function openChannelTab(name, tabText) {
  const sel = el("channel-select");
  sel.value = name;
  sel.dispatchEvent(new Event("change", { bubbles: true }));
  await waitFor(() => el("mute-" + name), 15000);
  const tab = Array.from(strip().querySelectorAll("[role='tab']"))
    .find((t) => t.textContent.trim() === tabText);
  if (tab) tab.click();
  await sleep(300);
  return !!tab;
}

async function run() {
  await waitFor(() => !el("sec-channels").hidden && strip(), 20000);
  await sleep(1200);
  window.localStorage.removeItem("accesscaster.stereoPanLink");

  // ---- mono: a mic ----
  check("Mic 1 has a Tone and pan tab", await openChannelTab("Mic1", "Tone and pan"));
  const mono = el("param-channelPan-Mic1");
  check("a mic has the single Pan slider", !!mono);
  check("a mic has no leg sliders",
    !el("param-channelPanL-Mic1") && !el("param-channelPanR-Mic1"));
  check("the mic's pan is spoken as a direction, not a percent",
    mono && /centre|left|right/.test(mono.getAttribute("aria-valuetext")),
    mono && mono.getAttribute("aria-valuetext"));

  // ---- stereo: USB 1 ----
  check("USB 1 has a Tone and pan tab", await openChannelTab("USB1", "Tone and pan"));
  check("USB 1 has no channelPan slider, which would do nothing", !el("param-channelPan-USB1"));
  const left = el("param-channelPanL-USB1");
  const right = el("param-channelPanR-USB1");
  check("USB 1 has a left side slider", !!left);
  check("USB 1 has a right side slider", !!right);
  check("the legs are spoken as directions",
    left && right && /left|right|centre/.test(left.getAttribute("aria-valuetext"))
      && /left|right|centre/.test(right.getAttribute("aria-valuetext")),
    left && right && left.getAttribute("aria-valuetext") + " / " + right.getAttribute("aria-valuetext"));
  const link = el("panlink-USB1");
  check("a Link switch is offered", !!link && link.type === "checkbox");

  // ---- linking ----
  const l0 = parseInt(left.value, 10), r0 = parseInt(right.value, 10);
  link.click();
  await waitFor(() => el("param-panlinked-USB1"), 4000);
  const one = el("param-panlinked-USB1");
  check("linked, the legs give way to one Pan slider",
    !!one && !el("param-channelPanL-USB1") && !el("param-channelPanR-USB1"));
  check("focus stays on the Link switch after it rebuilds the section",
    document.activeElement && document.activeElement.id === "panlink-USB1",
    document.activeElement && document.activeElement.id);
  check("the linked slider starts at centre",
    one && one.value === "0" && one.getAttribute("aria-valuetext") === "centre",
    one && one.value + " " + one.getAttribute("aria-valuetext"));

  await probeClear();
  one.value = "-30";
  one.dispatchEvent(new Event("input", { bubbles: true }));
  one.dispatchEvent(new Event("change", { bubbles: true }));
  await waitFor(async () => posts(await probeLog()).length >= 2, 8000);
  const log = posts(await probeLog());
  const sent = Object.fromEntries(log.map((r) => [r.body && r.body.prop, r.body && r.body.value]));
  check("moving it writes both legs and nothing else",
    log.length === 2 && "channelPanL" in sent && "channelPanR" in sent, JSON.stringify(log));
  const want = (v) => Math.max(0, Math.min(100, v - 30)) / 100;
  check("each leg moves by the same amount from where it was",
    Math.abs(sent.channelPanL - want(l0)) < 1e-9 && Math.abs(sent.channelPanR - want(r0)) < 1e-9,
    JSON.stringify(sent) + " from " + l0 + "/" + r0);
  check("the slider says which way it went", one.getAttribute("aria-valuetext") === "30 percent left",
    one.getAttribute("aria-valuetext"));

  // ---- unlinking ----
  el("panlink-USB1").click();
  await waitFor(() => el("param-channelPanL-USB1"), 4000);
  check("unlinked, the two leg sliders come back", !!el("param-channelPanL-USB1")
    && !!el("param-channelPanR-USB1") && !el("param-panlinked-USB1"));
  window.localStorage.removeItem("accesscaster.stereoPanLink");

  await probeDone();
}

run().catch(async (e) => {
  check("the checks ran to the end", false, String((e && e.stack) || e));
  await probeDone();
});
