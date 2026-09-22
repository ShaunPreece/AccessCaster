/* THE WIRELESS MIC'S OWN MUTE on the Channels page (channelWirelessMute). Two controls a keystroke
   apart are both called mute and do different things - one silences the strip inside the desk, the
   other tells a paired RODE transmitter to stop sending - so the thing worth checking by machine is
   that they are TOLD APART: different ids, different endpoints, different spoken names, and a
   description on each saying which is which.

   It is also the check that the control appears only where it can do something. A line channel and
   the SMART pads bus cannot have a transmitter paired to them, so offering the switch there would be
   a control that is read out, focused, operated and does nothing.

   The probe never forwards a POST, so the endpoint and body are asserted from its log without a byte
   reaching the desk - which matters here more than usual, because nobody on the project owns a
   wireless mic to hear the result.

   Run: python DevTools/ui_probe.py --checks DevTools/ui_checks_wireless.js --route channels */

const el = (id) => document.getElementById(id);
const wireless = (name) => el("wireless-mute-" + name);

async function waitFor(fn, ms) {
  const until = Date.now() + (ms || 10000);
  while (Date.now() < until) {
    try { if (fn()) return true; } catch (e) { /* not ready yet */ }
    await sleep(80);
  }
  return false;
}

async function pick(name) {
  const sel = el("channel-select");
  sel.value = name;
  sel.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(800);
  return sel.value;
}

/* The text a screen reader reads for a control: its label plus whatever aria-describedby points at.
   Checking the two separately is what would let "Mute Mic 1" and "Mute Mic 1" both pass. */
function spoken(input) {
  if (!input) return "";
  const lab = document.querySelector("label[for='" + input.id + "']")
    || (input.closest("label") || null);
  const desc = input.getAttribute("aria-describedby");
  const d = desc && el(desc);
  return ((lab ? lab.textContent : "") + " " + (d ? d.textContent : "")).trim();
}

async function run() {
  await waitFor(() => !el("sec-channels").hidden && el("channel-select"), 20000);
  await sleep(1200);

  const names = Array.from(el("channel-select").options).map((o) => o.value);

  // ---- It is there on a mic, and it is a real switch --------------------------------------
  check("selected Mic 1", (await pick("Mic1")) === "Mic1");
  const w = wireless("Mic1");
  check("a mic offers the wireless mic mute", !!w);
  check("it is a switch, not a button", !!w && w.type === "checkbox", w && w.type);
  check("it is reachable and not natively disabled", !!w && !w.disabled
    && w.getAttribute("aria-disabled") !== "true");

  // ---- And it cannot be mistaken for the channel mute -------------------------------------
  const chanMute = el("mute-Mic1");
  check("the channel mute is still there beside it", !!chanMute);
  const wSaid = spoken(w);
  const mSaid = spoken(chanMute);
  check("the two mutes do not read out the same", wSaid !== mSaid, wSaid + " || " + mSaid);
  check("the wireless one names the wireless mic", /wireless/i.test(wSaid), wSaid);
  check("and its description says it is not the channel mute",
    /wireless/i.test(wSaid) && /not the channel/i.test(wSaid), wSaid);
  check("the channel mute still says it silences everything",
    /everywhere|every mix/i.test(mSaid), mSaid);

  // ---- It writes to its OWN endpoint, with the channel it is on ---------------------------
  await probeClear();
  w.click();
  await sleep(900);
  const posts = (await probeLog()).filter((e) => e.method === "POST"
                                                 && e.path.indexOf("/api/ping") < 0);
  const paths = posts.map((p) => p.path);
  check("pressing it posts to the wireless-mute endpoint",
    paths.indexOf("/api/channels/wireless-mute") >= 0, paths.join(","));
  check("and NOT to the channel mute", paths.indexOf("/api/channels/mute") < 0, paths.join(","));
  const body = (posts.find((p) => p.path === "/api/channels/wireless-mute") || {}).body;
  const sent = body ? (typeof body === "string" ? JSON.parse(body) : body) : {};
  check("it names the channel it is on", sent.name === "Mic1", JSON.stringify(sent));
  check("and sends a boolean", typeof sent.on === "boolean", JSON.stringify(sent));

  // ---- It is absent everywhere a transmitter cannot be paired -----------------------------
  // DERIVED, not a written-out list. A Pro II has Mic 3 and Mic 4, which DO get the switch, so
  // "everything except Mic1 and Mic2" would have failed there while passing on the Duo it was
  // written against.
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

run().catch(async (e) => {
  check("the checks ran to the end", false, String((e && e.stack) || e));
  await probeDone();
});
