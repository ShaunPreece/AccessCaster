// probe: route=channels timeout=240
/* Pan on the Channels page, mono and stereo. Run:
     python DevTools/run_ui_probes.py stereopan

   A stereo channel pans with channelPanL / channelPanR, one per leg; channelPan only pans a mono
   one and does nothing on a stereo source. So:
     - a mic has ONE Pan slider, spoken as a direction ("centre"), never as a bare percent;
     - USB 1 has TWO leg sliders and no channelPan slider, plus a Link switch;
     - linked, one Pan slider sends one move, which the server applies to BOTH legs from where
       they were (server.action_set_linked_pan);
     - the probe stubs POSTs, so nothing reaches the desk. */

const el = (id) => document.getElementById(id);
const strip = () => document.querySelector("#channel-panel [role='tablist']");

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

  // ---- mono: a mic ----
  check("Mic 1 has a Tone and pan tab", await openChannelTab("Mic1", "Tone and pan"));
  const mono = el("param-channelPan-Mic1");
  check("a mic has the single Pan slider", !!mono);
  check("a mic has no leg sliders",
    !el("param-channelPanL-Mic1") && !el("param-channelPanR-Mic1"));
  check("the mic's pan is spoken as a direction, not a percent",
    mono && /centre|left|right/.test(mono.getAttribute("aria-valuetext")),
    mono && mono.getAttribute("aria-valuetext"));

  // ---- stereo: USB 1, starting unlinked whatever this computer's settings hold ----
  channelsData.pan_links = {};
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
  await probeClear();
  link.click();
  await waitFor(() => el("param-panlinked-USB1"), 4000);
  const one = el("param-panlinked-USB1");
  check("linked, the legs give way to one Pan slider",
    !!one && !el("param-channelPanL-USB1") && !el("param-channelPanR-USB1"));
  const linkPost = pagePosts(await probeLog())[0];
  check("linking sends where the legs are now",
    !!linkPost && linkPost.path === "/api/channels/pan-link" && linkPost.body.on === true
      && linkPost.body.left === l0 && linkPost.body.right === r0, JSON.stringify(linkPost));
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
  await waitFor(async () => pagePosts(await probeLog()).length >= 1, 8000);
  const log = pagePosts(await probeLog());
  const move = log[0];
  check("moving it sends one linked move and nothing else",
    log.length === 1 && move.path === "/api/channels/pan-linked", JSON.stringify(log));
  check("the move carries the offset and where the legs are",
    !!move && move.body.offset === -30 && move.body.left === l0 && move.body.right === r0,
    JSON.stringify(move && move.body) + " from " + l0 + "/" + r0);
  check("the slider says which way it went", one.getAttribute("aria-valuetext") === "30 percent left",
    one.getAttribute("aria-valuetext"));

  // ---- unlinking ----
  el("panlink-USB1").click();
  await waitFor(() => el("param-channelPanL-USB1"), 4000);
  check("unlinked, the two leg sliders come back", !!el("param-channelPanL-USB1")
    && !!el("param-channelPanR-USB1") && !el("param-panlinked-USB1"));

  await probeDone();
}

probeRun(run);
