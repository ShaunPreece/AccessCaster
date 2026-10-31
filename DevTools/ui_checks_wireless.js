// probe: route=channels timeout=90
/* THE WIRELESS MIC'S OWN MUTE on the Channels page (channelWirelessMute). Two controls a keystroke
   apart are both called mute and do different things - one silences the strip inside the desk, the
   other tells a paired RODE transmitter to stop sending - so the thing worth checking by machine is
   that they are TOLD APART: different ids, different endpoints, different spoken names, and a
   description on each saying which is which.

   It is also the check that the control appears only where it can do something. A line channel and
   the SMART pads bus cannot have a transmitter paired to them, so offering the switch there would be
   a control that is read out, focused, operated and does nothing.

   The probe never forwards a POST, so the endpoint and body are asserted from its log without a byte
   reaching the desk - which matters here more than usual, because the write is untested with a
   real transmitter.

   Run: python DevTools/run_ui_probes.py wireless */

const el = (id) => document.getElementById(id);
const wireless = (name) => el("wireless-mute-" + name);

async function pick(name) {
  const sel = el("channel-select");
  sel.value = name;
  sel.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(800);
  return sel.value;
}

/* No transmitter connected: it says so, a press is refused aloud, and nothing is sent. */
async function checkUnavailable() {
  const w = wireless("Mic1");
  check("[none paired] it is marked unavailable", !!w && w.getAttribute("aria-disabled") === "true");
  check("[none paired] and says why", /no wireless mics detected/i.test(describedText(w)),
    describedText(w));
  await probeClear();
  const was = w.checked;
  w.click();
  await sleep(900);
  check("[none paired] pressing it sends nothing", pagePosts(await probeLog()).length === 0,
    pagePosts(await probeLog()).map((p) => p.path).join(","));
  check("[none paired] and the switch does not move", w.checked === was);
}

/* A transmitter connected: it works, and cannot be mistaken for the channel mute. */
async function checkAvailable() {
  const w = wireless("Mic1");
  check("[paired] it is available", !!w && w.getAttribute("aria-disabled") !== "true");

  const chanMute = el("mute-Mic1");
  check("[paired] the channel mute is still there beside it", !!chanMute);
  const wSaid = accessibleName(w) + " " + describedText(w);
  const mSaid = accessibleName(chanMute) + " " + describedText(chanMute);
  check("[paired] the two mutes do not read out the same", wSaid !== mSaid, wSaid + " || " + mSaid);
  check("[paired] the wireless one names the wireless mic", /wireless/i.test(wSaid), wSaid);
  check("[paired] and its description says it is not the channel mute",
    /not the channel/i.test(wSaid), wSaid);
  check("[paired] the channel mute still says it silences everything",
    /everywhere|every mix/i.test(mSaid), mSaid);

  // It writes to its OWN endpoint, with the channel it is on.
  await probeClear();
  w.click();
  await sleep(900);
  const posts = pagePosts(await probeLog());
  const paths = posts.map((p) => p.path);
  check("[paired] pressing it posts to the wireless-mute endpoint",
    paths.indexOf("/api/channels/wireless-mute") >= 0, paths.join(","));
  check("[paired] and NOT to the channel mute", paths.indexOf("/api/channels/mute") < 0,
    paths.join(","));
  const body = (posts.find((p) => p.path === "/api/channels/wireless-mute") || {}).body;
  const sent = body ? (typeof body === "string" ? JSON.parse(body) : body) : {};
  check("[paired] it names the channel it is on", sent.name === "Mic1", JSON.stringify(sent));
  check("[paired] and sends a boolean", typeof sent.on === "boolean", JSON.stringify(sent));
}

async function run() {
  await waitFor(() => !el("sec-channels").hidden && el("channel-select"), 20000);
  await sleep(1200);

  const names = Array.from(el("channel-select").options).map((o) => o.value);

  // ---- It is there on a mic, and it is a real switch --------------------------------------
  check("selected Mic 1", (await pick("Mic1")) === "Mic1");
  check("a mic offers the wireless mic mute", !!wireless("Mic1"));
  check("it is a switch, not a button", !!wireless("Mic1") && wireless("Mic1").type === "checkbox",
    wireless("Mic1") && wireless("Mic1").type);
  check("it stays in the tab order: never natively disabled", !!wireless("Mic1")
    && !wireless("Mic1").disabled && !wireless("Mic1").hasAttribute("disabled"));

  // ---- Both states, whatever this desk has paired -----------------------------------------
  // The switch is unavailable while the unit reports transmitter slots and none connected. The
  // desk's own state is checked first, then channelsData is flipped and Mic 1 rebuilt, so one run
  // covers both whether or not a transmitter is paired. No POST is forwarded either way.
  const wm = channelsData && channelsData.wireless_mics;
  const deskSaysNone = !!(wm && wm.present && !wm.any_connected);
  for (const none of [deskSaysNone, !deskSaysNone]) {
    channelsData.wireless_mics = Object.assign({}, wm || {}, { present: true, any_connected: !none });
    await pick(names.find((n) => n !== "Mic1"));
    await pick("Mic1");
    if (none) await checkUnavailable();
    else await checkAvailable();
  }
  channelsData.wireless_mics = wm;

  // ---- It is absent everywhere a transmitter cannot be paired -----------------------------
  // DERIVED, not a written-out list. A Pro II has Mic 3 and Mic 4, which DO get the switch, so
  // "everything except Mic1 and Mic2" would pass on a Duo and fail on a Pro II.
  const shouldNotHaveIt = names.filter((n) => !/^Mic\d+$/.test(n));
  for (const n of shouldNotHaveIt) {
    await pick(n);
    check(n + " has no wireless mic mute, because nothing can pair to it", !wireless(n));
  }
  // The second mic DOES get one.
  const otherMics = names.filter((n) => /^Mic\d+$/.test(n) && n !== "Mic1");
  for (const n of otherMics) {
    await pick(n);
    check(n + " has one too, with its own id", !!wireless(n) && !wireless("Mic1"));
  }

  await probeDone();
}

probeRun(run);
