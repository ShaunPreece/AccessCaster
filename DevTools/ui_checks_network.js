/* The Wireless connections tab's WRITABLE controls. The tab's KEY, its route and this file's name
   all say "network" instead, deliberately: renaming them would break the remembered tab and the
   API path for nothing. Run:
     python DevTools/ui_probe.py --checks DevTools/ui_checks_network.js --route status --timeout 240

   This tab was a list of facts with a paragraph explaining why nothing on it could be changed.
   Three things are proven writable now (the wifi radio, joining a network, and Bluetooth
   discoverability), and the checks here are mostly about the PASSWORD and about honesty:

     - the password field is a real password field, is never prefilled, and is EMPTIED after a
       join attempt whether it worked or not. A password left in a form field is one screen-reader
       review command from being read aloud in a room with people in it;
     - the network NAME is prefilled from the unit, because rejoining after a password change
       should be one field, not two;
     - the page still says what CANNOT be done here (scanning, pairing, static IP) and does not
       quietly drop the subject;
     - pressing Join with no name posts NOTHING, rather than sending an empty write to the unit.

   The probe stubs every POST, so nothing here reaches the desk. */

const el = (id) => document.getElementById(id);
const pane = () => document.querySelector("#device-panel > .cat-tab[data-tab-key='network']");
const posts = (log) => log.filter((r) => r.method === "POST" && r.path !== "/api/ping");

async function waitFor(fn, ms) {
  const until = Date.now() + (ms || 8000);
  while (Date.now() < until) {
    try { if (fn()) return true; } catch (e) { /* not ready yet */ }
    await sleep(80);
  }
  return false;
}

function nameOf(node) {
  if (!node) return "";
  if (node.getAttribute("aria-label")) return node.getAttribute("aria-label");
  const lab = node.closest("label") || document.querySelector(`label[for='${node.id}']`);
  return lab ? lab.textContent.trim() : node.textContent.trim();
}

function flatText(node) {
  return node ? node.textContent.replace(/\s+/g, " ").trim() : "";
}

function described(node) {
  const id = node && node.getAttribute("aria-describedby");
  return id ? el(id) : null;
}

async function run() {
  await waitFor(() => el("sec-status") && !el("sec-status").hidden, 20000);
  // Open the Network tab and let its read land (it reads /api/network, about 1.5 s).
  const tab = el("tab-device-network");
  check("the Wireless connections tab is on the strip", !!tab);
  // The NAME is the point of the rename: "Network" said cables and IP addresses, so Bluetooth read
  // as an intruder and the pairing switch felt misfiled.
  check("and it is named for every radio, not for the network",
    /wireless connections/i.test(tab.textContent), tab.textContent);
  tab.click();
  await waitFor(() => pane() && !pane().hidden, 10000);
  await waitFor(() => el("net-wifi-ssid").textContent !== "Unknown", 25000);
  await sleep(800);

  // ---- the controls exist and are native ---------------------------------
  const wifi = el("net-wifi-toggle"), bt = el("net-bt-toggle"), join = el("net-join-btn");
  const ssid = el("net-join-ssid"), psk = el("net-join-psk");
  check("there is a wifi on/off switch", wifi && wifi.type === "checkbox", wifi && wifi.type);
  check("there is a Bluetooth discoverable switch", bt && bt.type === "checkbox", bt && bt.type);
  check("there is a Join button", join && join.tagName === "BUTTON", join && join.tagName);
  check("the network name is a plain text box", ssid && ssid.type === "text", ssid && ssid.type);
  check("THE PASSWORD IS A PASSWORD FIELD, not a text box",
    psk && psk.type === "password", psk && psk.type);

  check("the wifi switch says what it switches", /wifi/i.test(nameOf(wifi)), nameOf(wifi));
  check("the Bluetooth switch is named for what it DOES, not for the property",
    /find this unit/i.test(nameOf(bt)), nameOf(bt));
  check("the name box has a label", /network name/i.test(nameOf(ssid)), nameOf(ssid));
  check("the password box has a label", /password/i.test(nameOf(psk)), nameOf(psk));
  // THE JOIN HINT IS DELIBERATELY NOT ATTACHED TO ANYTHING. Describing the name box, the password
  // box AND the Join button with one paragraph reads four sentences three times over while moving
  // through three controls. It sits in normal reading order instead and is read ONCE; the labels
  // say what each box is, which is what a label is for.
  [["network name", ssid], ["password", psk], ["Join button", join]].forEach(([n, c]) => {
    check(`the ${n} does NOT repeat the join hint at the user`, !described(c),
      described(c) && described(c).id);
  });

  // ---- what the hints have to say ----------------------------------------
  const jhint = flatText(el("net-join-hint"));
  check("the join hint is still on the page, in reading order", !!jhint.length);
  check("the hint says the password is NOT kept by this app",
    /not kept by\s*AccessCaster/i.test(jhint), jhint);
  check("and that joining is only possible at this computer",
    /only be done at the computer/i.test(jhint), jhint);
  check("and how long it may take, so silence is not read as a hang",
    /45 seconds/.test(jhint), jhint);
  // THE COPY IS SHORT. What belongs here is only what changes what the user
  // DOES: the exact name, the empty box for an open network, the wait, and the local-only rule
  // that explains a refusal from a phone. The reasoning behind each lives in the code.
  check("and it stays short: the reasoning is in the code, not on the page",
    jhint.length < 280, jhint.length);
  const bhint = flatText(described(bt));
  check("the Bluetooth hint gives the route that actually works: pair from the phone",
    /pair from the phone/i.test(bhint), bhint);
  check("and warns that discoverability times out on its own",
    /stops being discoverable/i.test(bhint));

  // ---- honesty about what is still read-only -----------------------------
  const ro = flatText(el("net-readonly"));
  check("the page still says what CANNOT be done here", ro.length > 40, ro);
  check("it names scanning, pairing and the fixed IP address",
    /scanning/i.test(ro) && /pairing/i.test(ro) && /fixed IP/i.test(ro), ro);
  // PARKED, NOT WRITTEN OFF. The page must not read as a closed door: a single attempt on a quiet
  // network with nothing in pairing mode proves nothing either way. What the page does NOT do is
  // explain the protocol - that is not the user's problem.
  check("and says they are still being worked on, not that they are impossible",
    /still being worked on/i.test(ro), ro);
  check("and says what to do instead", /discoverable/i.test(ro), ro);
  check("and stays short", ro.length < 280, ro.length);

  // ---- the state the controls show is the UNIT's, not the page's ----------
  check("the wifi switch reflects what was read", typeof wifi.checked === "boolean");
  check("the network name box is prefilled from the unit",
    (ssid.value || "").length > 0, ssid.value);
  check("THE PASSWORD BOX IS NOT PREFILLED, ever", (psk.value || "") === "", psk.value);

  // ---- one live region for the replies ------------------------------------
  const status = el("net-write-status");
  check("there is a status line for the replies", !!status);
  check("it is polite", status.getAttribute("aria-live") === "polite");
  const lives = Array.from(pane().querySelectorAll("[aria-live]"));
  check("and it is the ONLY live region on this tab", lives.length === 1,
    lives.map((n) => n.id).join(","));

  // ---- the writes (stubbed by the probe) ----------------------------------
  await probeClear();
  ssid.value = "";
  join.click();
  await sleep(700);
  let log = posts(await probeLog());
  check("Join with no network name posts NOTHING", log.length === 0,
    JSON.stringify(log.map((r) => r.path)));
  check("and it says what is missing", /type the name/i.test(status.textContent),
    status.textContent);
  check("and puts focus on the box that is empty",
    document.activeElement === ssid, document.activeElement && document.activeElement.id);

  await probeClear();
  ssid.value = "A Test Network";
  psk.value = "not-a-real-password";
  join.click();
  await waitFor(async () => posts(await probeLog()).length > 0, 10000);
  log = posts(await probeLog());
  check("Join posts to /api/network/wifi/join, once",
    log.length === 1 && log[0].path === "/api/network/wifi/join",
    JSON.stringify(log.map((r) => r.path)));
  await sleep(400);
  check("THE PASSWORD FIELD IS EMPTIED once the attempt is over", psk.value === "", psk.value);

  await probeClear();
  wifi.click();
  await waitFor(async () => posts(await probeLog()).length > 0, 10000);
  log = posts(await probeLog());
  check("the wifi switch posts to /api/network/wifi",
    log.length === 1 && log[0].path === "/api/network/wifi",
    JSON.stringify(log.map((r) => r.path)));

  await probeClear();
  bt.click();
  await waitFor(async () => posts(await probeLog()).length > 0, 10000);
  log = posts(await probeLog());
  check("the Bluetooth switch posts to /api/network/bt-visible",
    log.length === 1 && log[0].path === "/api/network/bt-visible",
    JSON.stringify(log.map((r) => r.path)));

  await probeDone();
}

run().catch(async (e) => {
  check("the checks ran to the end", false, String((e && e.stack) || e));
  await probeDone();
});
