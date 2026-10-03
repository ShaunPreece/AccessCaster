/* AccessCaster front end.
   Keeps the UI honest: it reflects exactly what the backend can do today. The
   unit's current settings CAN be read back over USB (Read from device), and any
   value you change is sent to the unit immediately. Every result is announced
   through an ARIA live region so a screen-reader user hears it without hunting
   around the page. */

"use strict";

/* How long a slider waits after you stop moving before it commits to the device. It exists only
   to stop a drag or a held arrow key firing one USB write per step; it is NOT the device being
   slow. A settings write measures about 10 ms end to end, so 150 ms is ample and the old 250-350 ms
   was just added lag on every adjustment. Pad edits have their own, longer value: each one enters
   transfer mode (~0.5 s), so it is worth waiting a little longer to be sure the drag has finished. */
const WRITE_DEBOUNCE_MS = 150;
const PAD_WRITE_DEBOUNCE_MS = 400;

function announce(message, assertive) {
  const region = document.getElementById(assertive ? "alert-region" : "live-region");
  // Clearing first guarantees the change is re-announced even if text repeats.
  region.textContent = "";
  window.setTimeout(() => { region.textContent = message; }, 50);
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

/* WRITE TO A STATUS LINE THAT IS ITSELF A LIVE REGION, and let that line be the only voice.

   NEVER pair `setText` into a `role="status"` element with an `announce()` of the same words: both
   are polite regions changing in the same tick, so a screen reader either says it twice or drops
   one, and the guide forbids it outright.

   The clear-then-set is the half that makes one voice enough. A live region only speaks when its
   text CHANGES, so pressing the same failing control twice would be silent the second time -
   exactly the case where silence reads as a dead keyboard. This is the same trick `announce` uses
   and the reason `announce` cannot simply be dropped in favour of a bare `setText`. */
function setStatus(id, text) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = "";
  window.setTimeout(() => { el.textContent = text; }, 50);
}

/* Re-reading the device rebuilds a whole panel, which DESTROYS the control you were sitting on.
   Focus then falls back to the document, so a screen-reader user is silently dumped at the top of
   the page and has to tab all the way back. Every rebuild that can follow a user action runs inside
   this: it notes the focused control's id (ids are deterministic, e.g. "mod-eq-Mic1"), lets the
   rebuild happen, then puts focus back on the control with the same id in the new DOM.

   It only ever restores focus that was LOST (activeElement fell back to the body): if you moved on
   to another control while the read was in flight, your new position is left alone. */
async function keepingFocus(rebuild) {
  const before = document.activeElement;
  const id = before && before.id;
  const result = await rebuild();
  if (!id) return result;
  const active = document.activeElement;
  const lost = !active || active === document.body || active === document.documentElement;
  if (!lost) return result;                     // focus is somewhere real; do not move it
  const again = document.getElementById(id);
  if (again && !again.disabled) {
    try { again.focus(); } catch (e) { /* element cannot take focus; leave it */ }
  }
  return result;
}

/* Mark a control BUSY while its write is in flight, WITHOUT taking focus off it.
   Setting the real `disabled` attribute on the element that currently has focus blurs it (the
   browser will not keep focus on a disabled control) and focus falls back to the document, so a
   screen-reader user is thrown to the top of the page every time they flip a switch, move a
   dropdown or press an action button. aria-disabled announces the same "unavailable" state, keeps
   focus exactly where it was, and looks identical (see style.css); the data-busy flag is what
   actually stops a second activation while the first is still running. */
function setBusy(el, busy) {
  if (!el) return;
  if (busy) {
    el.setAttribute("aria-disabled", "true");
    el.dataset.busy = "1";
  } else {
    el.removeAttribute("aria-disabled");
    delete el.dataset.busy;
  }
}

function isBusy(el) {
  return !!(el && el.dataset && el.dataset.busy);
}

/* ---- Tab strips: the layout every page uses --------------------------------
   THERE ARE NO COLLAPSIBLE GROUPS, and nothing should reintroduce them: every page uses one of the
   two helpers below. `dropRetiredGroupState()` near init() deletes the old `accesscaster.groups`
   localStorage key once, so a remembered "closed" cannot outlive a design with no closed state.
   A page's categories become one horizontal strip of role="tab"
   buttons with a single panel below, and ONLY the selected panel exists in the DOM.

   Why this beats the groups it replaces:
     - a tab is announced as a tab with its position ("Dynamics, tab, 5 of 7") and is NOT in BUTTON
       quick navigation, so B still lands only on real actions (Apply, Refresh, Reset);
     - heading navigation, form-field navigation and the tab order see one category at a time, so a
       mic's 50-plus controls stop being one long walk;
     - the selected tab is announced, so where you are is never a guess (a remembered "closed"
       group was invisible until you walked it).

   ONE ROW, NEVER WRAPPED: wrapped tab rows are a maze for anyone zoomed in, which is why the
   categories are merged (see the per-page maps in the plan). On a narrow screen the strip scrolls
   sideways rather than wrapping, which changes nothing for a screen reader.
   Left/Right move along the strip and switch the panel as they go, Home/End jump to the ends; up
   and down are deliberately unbound, since a horizontal strip does not promise them. Roving
   tabindex gives the whole strip one tab stop.
   The selected tab is remembered per page by KEY (not index), so adding a tab later cannot silently
   move you, and a page offering fewer tabs than last time (Channels on a Bluetooth channel) falls
   back to the first tab instead of landing on nothing. */
const TAB_STATE_KEY = "accesscaster.tabs";
let _tabState = null;

function tabState() {
  if (_tabState) return _tabState;
  try { _tabState = JSON.parse(window.localStorage.getItem(TAB_STATE_KEY)) || {}; }
  catch (e) { _tabState = {}; }   // storage blocked: fall back to per-session memory
  return _tabState;
}

function rememberTab(pageKey, key) {
  const st = tabState();
  st[pageKey] = key;
  try { window.localStorage.setItem(TAB_STATE_KEY, JSON.stringify(st)); } catch (e) { /* ignore */ }
}

/* Does this pane hold anything the Tab key can reach?
   A tabpanel with NO focusable content has to be focusable ITSELF, or Tab from the strip sails past
   the content and lands on whatever comes next in the document, which reads as being dumped at the
   top of the page. The Device page's Network and Technical tabs are the case: a description list
   and a bullet list with not one control between them. Giving the panel
   tabindex="0" in that case is the ARIA APG rule, and it means Tab moves INTO the panel and the
   screen reader starts reading there. */
function paneHasFocusable(pane) {
  const SEL = "a[href], button, input, select, textarea, [tabindex]";
  return Array.from(pane.querySelectorAll(SEL)).some((el) => {
    if (el.disabled) return false;
    if (el.getAttribute("tabindex") === "-1") return false;
    for (let n = el; n && n !== pane; n = n.parentElement) if (n.hidden) return false;
    return true;
  });
}

/* Apply that rule to a panel showing `pane` (for a JS-built page the panel IS the pane). */
function setPanelTabStop(panel, pane) {
  if (paneHasFocusable(pane)) panel.removeAttribute("tabindex");
  else panel.tabIndex = 0;
}

/* EVERY PANE IS BUILT ONCE AND THEN HIDDEN OR SHOWN. Destroying the panel and rebuilding it for the
   incoming tab, so that only the selected pane exists, breaks the strip for a screen reader:
   arrowing back to Channel settings on a mic announces the MIC TYPE list ("Line-in, 1 of 11")
   instead of the tab, and arrowing on to Presets announces the preset list. Focus is on the right
   tab throughout (DevTools/ui_checks_chantabs.js), so it is not a focus fault: the panel's whole
   subtree is replaced in the same moment the screen reader is told to move, and it reads what lands
   at that position instead of the tab. Same fault as a panel that speaks fragments of its own
   controls, same answer: STRUCTURE ONCE. A tab change touches nothing but the `hidden` attribute
   and focus, which is what buildStaticTabs does.
   A hidden pane is out of the accessibility tree, out of the tab order and out of heading navigation,
   so a screen reader still sees exactly one category at a time. The cost is building a mic's seven
   panes instead of one when the CHANNEL changes, a few milliseconds of DOM per change. */
/* NO LANDMARK ROUND THE TAB STRIP, AND IT HAS BEEN TRIED. Wrapping the strip in a <nav> gives a
   jump target, but focus then lands there WITHOUT entering browse mode, so the arrow keys do
   nothing until the user switches mode by hand: more confusing than helpful. The page's own section
   headings are already landmarks and are enough. Do not re-add it. */

/* THE TAB STRIP ITSELF, and the ONE copy of it. Given a panel and the panes already inside it, this
   builds the button strip and owns every behaviour the two builders below share: the roving
   tabindex, the arrow/Home/End keys, the remembered tab, the "focus the tab before you hide the
   pane it is in" rule, and the panel's own tab stop.

   KEEP IT AS ONE COPY. This is the widget a user moves through most, every rule in it comes from a
   screen-reader pass, and a fix applied to one copy of it would silently not apply to the other.
   `panes` and `keys` come from the caller because that is the ONLY real difference between the two
   builders: one builds its panes, the other finds them in the document.

   `onSelect(key)` fires when the USER changes tab, and deliberately NOT for the first paint: the
   page's own onShow does the arrival read, and firing here would read the device at startup for a
   page nobody has opened yet. */
function wireTabStrip(pageKey, panelId, panel, panes, keys, titles, onSelect) {
  const tabId = (key) => "tab-" + pageKey + "-" + String(key).replace(/[^A-Za-z0-9_-]+/g, "-");

  const strip = document.createElement("div");
  strip.className = "cat-list";
  // NO NAME ON THE STRIP, AND NO NAME ON THE PANEL, both deliberately.
  // A screen reader announces the tablist's accessible name every time you arrive at the strip,
  // and then the panel's name again as you move into it, so walking one tab reads as "Settings
  // categories for the chosen channel", "tab control", "Channel settings", "property page".
  // Three of those four are furniture. The tab's OWN name is the only part that says where you
  // are, and it is announced by the tab itself.
  //
  // The APG suggests naming a tablist. It is not a WCAG requirement, every one of these strips
  // sits directly under a heading that says what page it is, and the cost is paid on every single
  // move along the strip - which on this app is constant. The user it is for asked for it gone.
  strip.setAttribute("role", "tablist");
  panel.setAttribute("role", "tabpanel");

  // A remembered tab wins; if its category is not on offer for this context, start at the first.
  let index = 0;
  const at = keys.indexOf(tabState()[pageKey]);
  if (at >= 0) index = at;

  const buttons = keys.map((key, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "tab");
    b.id = tabId(key);
    b.setAttribute("aria-controls", panelId);
    b.textContent = titles[i];
    b.addEventListener("click", () => select(i, true, true));
    b.addEventListener("keydown", (ev) => onKey(ev, i));
    strip.appendChild(b);
    return b;
  });

  function onKey(ev, i) {
    if (ev.altKey || ev.ctrlKey || ev.metaKey) return;   // leave the app's Alt shortcuts alone
    const n = keys.length;
    let next = null;
    if (ev.key === "ArrowRight") next = (i + 1) % n;
    else if (ev.key === "ArrowLeft") next = (i - 1 + n) % n;
    else if (ev.key === "Home") next = 0;
    else if (ev.key === "End") next = n - 1;
    if (next === null) return;
    ev.preventDefault();
    select(next, true, true);
  }

  function paint() {
    buttons.forEach((b, i) => {
      const on = i === index;
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;                          // roving tabindex: one stop for the strip
    });
    panes.forEach((pane, i) => { pane.hidden = (i !== index); });
    setPanelTabStop(panel, panes[index]);
  }

  function select(i, focus, fire) {
    if (i < 0 || i >= keys.length || i === index) {
      if (focus) focusSelected();
      return;
    }
    // Never hide the pane focus is sitting in: put focus on the tab first, or a screen reader is
    // left on an element that has just left the accessibility tree.
    const active = document.activeElement;
    if (active && panes[index].contains(active)) buttons[i].focus();
    index = i;
    rememberTab(pageKey, keys[index]);
    paint();
    if (focus) focusSelected();
    if (fire && onSelect) onSelect(keys[index]);
  }

  function focusSelected() {
    const b = buttons[index];
    if (b) { try { b.focus(); } catch (e) { /* not focusable yet; leave it */ } }
  }

  paint();
  return {
    strip,
    panel,
    select: (i) => select(i, true, true),
    selectKey: (key) => select(keys.indexOf(key), true, true),
    current: () => keys[index] || null,
    focusSelected,
    // Re-decide the panel's own tab stop without changing tab. A page whose pane shows and hides
    // rows as its data changes (the SMART pads page's Settings tab, empty versus filled) can go from
    // having focusable content to having none, and the rule has to follow it.
    refresh: () => setPanelTabStop(panel, panes[index]),
  };
}

/* Build a tab strip and its panel from scratch. `tabs` is [{ key, title, fill(pane, ctx) }]; `fill`
   draws into that tab's own pane, which already carries an h2 naming the category AND the context
   (for example "Dynamics for Mic 1"), so several tabs away from a page's context dropdown you still
   know what you are editing.
   Options: `context` (appended to each pane heading), `ctx` (passed through to fill as its second
   argument), `onSelect(key)`.
   Ids are DETERMINISTIC (tab-<pageKey>-<key>), so a rebuild that follows a device read puts the same
   ids back and keepingFocus() can restore focus onto a tab.
   Returns wireTabStrip's controller - append `strip` then `panel` where the categories go. Anything
   with role="status" stays OUTSIDE the panel, at page level, or a tab change would destroy the
   message mid-announcement. */
function buildTabs(pageKey, tabs, opts) {
  opts = opts || {};
  const panelId = "tabpanel-" + pageKey;

  const panel = document.createElement("div");
  panel.className = "cat-panel";
  panel.id = panelId;

  // One pane per tab, built ONCE (see the note above). Each opens with the h2 that names the
  // category and the context, so heading navigation lands on it whichever tab is showing.
  const panes = tabs.map((t) => {
    const pane = document.createElement("div");
    pane.className = "cat-tab";
    pane.dataset.tabKey = t.key;
    const h = document.createElement("h2");
    h.textContent = opts.context ? t.title + " for " + opts.context : t.title;
    pane.appendChild(h);
    if (t.fill) t.fill(pane, opts.ctx);
    panel.appendChild(pane);
    return pane;
  });

  return wireTabStrip(pageKey, panelId, panel, panes,
                      tabs.map((t) => t.key), tabs.map((t) => t.title), opts.onSelect);
}

/* One sub-group inside a MERGED tab: an h3 you can jump to with H, plus a role="group" carrying the
   same name, so a slider called Threshold still announces whether it belongs to the compressor, the
   noise gate or the de-esser. Heading levels stay in order: page h1, panel h2, sub-group h3. */
/* A sub-heading and a plain wrapper. NO role="group" and no name on it: a named group is
   announced on the way in, before the control you are actually going to, and again as "out of
   grouping" on the way out. The h3 says the same thing, is reachable with H, and costs nothing
   when you are not looking for it. */
function tabSection(body, title) {
  const h = document.createElement("h3");
  h.textContent = title;
  body.appendChild(h);
  const wrap = document.createElement("div");
  body.appendChild(wrap);
  return wrap;
}

/* The same tab strip for a page whose controls are written in index.html rather than built in JS
   (Routing, and the rest as they are converted). The strip itself IS the same code as buildTabs':
   both are thin adapters over wireTabStrip, which is where the keyboard handling, the roving
   tabindex and the focus rules live.
   ONE DELIBERATE DIFFERENCE: the panes are NOT built and destroyed, they STAY IN THE DOCUMENT and
   are hidden. Every control on a static page is wired by id ONCE at startup, and a lot of code
   reaches for those ids later (loadRouting fills #routing-inputs, the read-only path inserts notes
   into #routing-master, and so on), so a detached pane would turn every one of those lookups into
   null. `hidden` gives the accessibility half of what we want anyway: a hidden pane is out of the
   accessibility tree, out of the tab order and out of heading navigation, so a screen reader still
   sees exactly one category at a time.
   Markup contract: `<div id="..." class="cat-panel" role="tabpanel">` holding one
   `<div class="cat-tab" data-tab-key="..." data-tab-title="...">` per tab, each opening with the h2
   that names the category. Panes after the first carry `hidden` in the markup so the page is not
   blank if this never runs.
   `opts.onSelect(key)` fires when the USER changes tab, and deliberately NOT for the first paint:
   the page's own onShow does the arrival read, and firing here would read the device at startup for
   a page nobody has opened yet. */
function buildStaticTabs(pageKey, panelId, opts) {
  opts = opts || {};
  const panel = document.getElementById(panelId);
  if (!panel) return null;
  const panes = Array.from(panel.children).filter((el) => el.classList.contains("cat-tab"));
  if (!panes.length) return null;

  const ctl = wireTabStrip(pageKey, panelId, panel, panes,
                           panes.map((p) => p.dataset.tabKey),
                           panes.map((p) => p.dataset.tabTitle || p.dataset.tabKey),
                           opts.onSelect);
  // The panel is already in the document, so the strip goes in front of it here rather than being
  // handed back for a caller to place.
  panel.parentNode.insertBefore(ctl.strip, panel);
  return ctl;
}

/* Focus the first of these ids that exists and can take focus. Used when the control a user just
   activated is legitimately gone or disabled afterwards (e.g. Clear this pad, which disables
   itself once the pad is empty), so focus lands somewhere sensible instead of nowhere. */
function focusFirst(ids) {
  for (const id of ids) {
    const el = document.getElementById(id);
    if (el && !el.disabled && !el.hidden) {
      try { el.focus(); return el; } catch (e) { /* try the next one */ }
    }
  }
  return null;
}

async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return resp.json();
}

/* ---- Remote access -------------------------------------------------------
   The link that lets a phone on the same network drive the desk. The server binds 0.0.0.0, so
   without the token anyone who can reach the port has full control, Clear all pads included: fine
   at home, not at a venue.

   THIS PANEL ONLY EVER WORKS ON THE MACHINE THE DESK IS PLUGGED INTO. /api/access is LOCAL ONLY,
   so a remote page that is legitimately signed in gets a refusal here rather than the token: being
   allowed in is not the same as being allowed to read or replace the key. */
async function loadAccess() {
  const box = document.getElementById("dev-access-url");
  if (!box) return;
  try {
    const data = await (await fetch("/api/access")).json();
    if (data.ok === false) {                       // a remote page asking: say why, show nothing
      setText("dev-access-note", data.message || "Not available from here.");
      box.value = "";
      setAvailable(box, false, data.message || "");
      ["dev-access-copy", "dev-access-new"].forEach((id) =>
        setAvailable(document.getElementById(id), false, data.message || ""));
      return;
    }
    box.value = data.url || "";
    setText("dev-access-note", data.note || "");
    setText("dev-access-warning", data.warning || "");
  } catch (err) {
    setText("dev-access-note", "Could not read the access link.");
  }
}

function wireAccess() {
  const copy = document.getElementById("dev-access-copy");
  const make = document.getElementById("dev-access-new");
  if (!copy || !make) return;
  copy.addEventListener("click", async () => {
    const box = document.getElementById("dev-access-url");
    if (!box || !box.value) return;
    // SELECT IT EITHER WAY. The clipboard call can be refused (it needs a secure context in some
    // browsers, and this is plain HTTP), so the fallback is not an error path: the text is left
    // selected so Ctrl+C works, and the live region says which happened.
    box.focus();
    box.select();
    try {
      await navigator.clipboard.writeText(box.value);
      announce("The access link is on the clipboard.");
      setStatus("dev-access-status", "Copied.");
    } catch (err) {
      announce("Could not copy automatically. The link is selected, so press Control C.", true);
      setStatus("dev-access-status", "The link is selected. Press Control C to copy it.");
    }
  });
  make.addEventListener("click", async () => {
    const ok = await confirmAction(
      "Make a new access link?",
      "The link you have now will stop working, on every device you have shared it with. This "
      + "computer is not affected and never needs the link.",
      "Make a new link");
    if (!ok) return;
    setBusy(make, true);
    try {
      const res = await postJSON("/api/access/new", {});
      if (res.access && res.access.url) {
        document.getElementById("dev-access-url").value = res.access.url;
      }
      setStatus("dev-access-status", res.message || "Done.");
    } catch (err) {
      announce("Could not reach the device service.", true);
    } finally {
      setBusy(make, false);
    }
  });
}

/* ---- Device status -------------------------------------------------- */

/* The app's own version, for the Help & About page. Do NOT take it from the /api/status payload:
   that is a USB enumeration made by the Device page alone, so anyone going straight to Help & About
   reads "Version unknown". /api/version touches no hardware, so this runs once at startup and the
   number is always there. */
async function loadVersion() {
  try {
    const resp = await fetch("/api/version");
    const data = await resp.json();
    if (data.app_version) {
      setText("app-version", data.app_version);
      // AND THE PAGE'S OWN HEADING, which is where focus lands on open, so the build is the first
      // thing said. See the markup for why the number must be here before the first render rather
      // than arriving later: a heading that changes is not re-read.
      // "version 2.50", NEVER "v2.50": a screen reader reads the letter v against a number as
      // "volt".
      setText("app-title", "AccessCaster version " + data.app_version);
    }
  } catch (err) { /* the placeholder in the markup stands */ }
}

/* THE FIRST RENDER WAITS FOR THE VERSION, BUT NEVER FOR LONG. It is our own server serving this
   very page and /api/version takes no device access, so it answers in milliseconds; the timeout is
   there so a wedged or dying server cannot leave the window with no focus at all. Losing the race
   costs the number in the heading, nothing else. */
const VERSION_WAIT_MS = 1500;

function loadVersionBeforeRender() {
  return Promise.race([loadVersion(),
                       new Promise((done) => window.setTimeout(done, VERSION_WAIT_MS))]);
}

async function loadStatus(announceResult) {
  setText("status-line", "Checking for the RODECaster...");
  let data;
  try {
    const resp = await fetch("/api/status");
    data = await resp.json();
  } catch (err) {
    setText("status-line", "Could not reach the AccessCaster service.");
    setText("status-connection", "Service unreachable");
    setText("app-device", "AccessCaster service unreachable");
    if (announceResult) announce("Could not reach the AccessCaster service.", true);
    return;
  }

  setText("status-line", data.message || "");
  // The header's live region: the SHORT form, built by the server beside the long one. It is read
  // out when it changes, which is how the desk gets announced at all - the answer arrives after the
  // window has opened and focus has landed on the heading above it.
  setText("app-device", data.headline || data.message || "");

  // App version (Project/version.py, surfaced in the /api/status payload) shown on the About page.
  // loadVersion() at startup is what guarantees it is there; this keeps it right if the number ever
  // changes under a running window.
  if (data.app_version) setText("app-version", data.app_version);

  // GAP-10: reflect the actually-connected desk (Pro II or Duo) in the window title, rather than a
  // hardcoded model. The server resolves the name from the USB product id (get_active_device().name).
  if (data.device_name)
    document.title = "AccessCaster - " + data.device_name + " controller";

  const conn = document.getElementById("status-connection");
  if (data.connected) {
    conn.textContent = data.online ? "Connected and responding" : "Connected";
    conn.className = "status-ok";
  } else {
    conn.textContent = "Not connected";
    conn.className = "status-err";
  }
  setText("status-hardware", data.hardware_id || "Unknown");
  setText("status-info", data.info_hex ? "0x" + data.info_hex : "Unknown");

  // The Technical tab re-reads this when it is opened, so the list is only rebuilt when the USB
  // interfaces themselves have changed. Same rule as everywhere else: nothing may replace a subtree
  // a screen reader may be reading, and here the read lands the moment you arrive on the tab.
  const lines = (data.interfaces || []).length ? data.interfaces : ["None found."];
  rebuildIfChanged(document.getElementById("interfaces-list"), JSON.stringify(lines), (list) => {
    lines.forEach((line) => {
      const li = document.createElement("li");
      li.textContent = line;
      list.appendChild(li);
    });
  });

  if (announceResult) announce(data.message || "Status updated.");
}

/* ---- Wireless connections: wifi, Bluetooth and wireless mics ---------------
   The endpoint and the tab key are both still `network` while the tab is called Wireless
   connections: renaming either would only break the remembered tab and the URL. FIRMWARE AND THE
   WIRED NETWORK are in this payload but are painted by the Hardware tab (loadDeviceState), so
   nothing here writes them. */

async function loadNetwork() {
  const btn = document.getElementById("net-read-btn");
  setBusy(btn, true);
  try {
    const resp = await fetch("/api/network");
    const data = await resp.json();
    if (data.ok === false) {
      setText("net-status", data.message || "Could not read the device.");
      announce(data.message || "Could not read the device.", true);
      return;
    }
    // The MIDI switch is NOT painted from here, even though this payload carries its value: it
    // lives on Unit settings and is painted by that tab's own read.
    const w = data.wifi || {};
    setText("net-wifi-ssid", w.ssid
      ? w.ssid + (w.has_password ? " (has a password saved)" : "")
      : "None set");
    setText("net-wifi-connected", w.connected ? "Yes" : "No");
    setText("net-wifi-ip", w.ip ? w.ip : "None");
    // An EMPTY scan list means "nothing has been scanned for", not "nothing is in range", and the
    // difference matters: we cannot start a scan from here, so a bare "None" would be read as the
    // unit reporting an empty airwave. Same wording for both radios.
    const scanText = (list) => (list && list.length)
      ? list.join(", ")
      : "Nothing found. The unit only fills this in when a scan is run on its own screen, so this "
        + "usually means no scan has been run rather than nothing being in range.";
    setText("net-wifi-scan", scanText(w.scan));
    const bt = data.bluetooth || {};
    setText("net-bt", bt.connected ? ("Connected to " + bt.connected_address)
                                    : "No device connected");
    setText("net-bt-visible", bt.visible === true ? "Yes, other devices can find it"
      : bt.visible === false ? "No, other devices cannot find it" : "Unknown");
    setText("net-bt-paired",
      bt.paired && bt.paired.length ? bt.paired.join(", ") : "None paired");
    setText("net-bt-scan", scanText(bt.scan));
    setText("net-readonly", data.still_read_only || "");
    // The three controls reflect what the unit reports, so opening the tab shows its real state
    // rather than whatever was last left in the page.
    const wifiToggle = document.getElementById("net-wifi-toggle");
    if (wifiToggle) {
      // "connected" means it HAS an address. The radio being on is a different fact, and the one
      // this switch owns: a radio can be on and not yet joined to anything.
      wifiToggle.checked = !!(w.on !== undefined ? w.on : (w.connected || w.ssid));
      setBusy(wifiToggle, data.writable === false);
    }
    const btToggle = document.getElementById("net-bt-toggle");
    if (btToggle) {
      btToggle.checked = bt.visible === true;
      setBusy(btToggle, data.writable === false);
    }
    const joinBtn = document.getElementById("net-join-btn");
    if (joinBtn) setBusy(joinBtn, data.writable === false);
    // Scanning is a write (it sets `wifiScan`), so it follows the same gate as joining.
    const scanBtn = document.getElementById("net-scan-btn");
    if (scanBtn) setBusy(scanBtn, w.can_scan === false || data.writable === false);
    // The network name is prefilled with what the unit already has, so rejoining after a password
    // change is one field rather than two. The PASSWORD is never prefilled and never comes back
    // from the server, which is the whole point of the rule around it.
    const ssidBox = document.getElementById("net-join-ssid");
    if (ssidBox && !ssidBox.value && w.ssid) ssidBox.value = w.ssid;
    paintWirelessMics(data.wireless_mics);
    setText("net-status", "");
  } catch (err) {
    setText("net-status", "Could not reach the device service.");
    announce("Could not reach the device service.", true);
  } finally {
    setBusy(btn, false);
  }
}

/* The wireless mic readout, in sentences rather than a table, like the Hardware report it came
   from. THREE OUTCOMES, not two, and the difference matters: slots that all read off really do mean
   no transmitter is on, but a dump carrying NO slots at all means the unit told us nothing, and
   reading that out as "none connected" would be inventing an answer. Nothing has ever been paired
   on this desk, so the connected wording is unproven in practice (server._wireless_mics). */
function paintWirelessMics(mics) {
  const out = document.getElementById("net-mics");
  if (!out) return;
  const m = mics || {};
  const lines = [];
  if (!m.present) {
    lines.push("Wireless mics: the unit did not report any, so there is nothing to show.");
  } else if (m.any_connected) {
    (m.transmitters || []).filter((t) => t.connected).forEach((t) => {
      lines.push(`Wireless mic ${t.index + 1}: connected`
        + (t.battery_pct === null || t.battery_pct === undefined
            ? "" : `, battery ${t.battery_pct} percent`)
        + (t.rssi === null || t.rssi === undefined ? "" : `, signal ${t.rssi}`) + ".");
    });
  } else {
    lines.push("Wireless mics: none connected.");
  }
  out.textContent = "";
  lines.forEach((l) => {
    const p = document.createElement("p");
    p.textContent = l;
    out.appendChild(p);
  });
}

/* ---- Network: the three writable things ------------------------------------
   Wifi on/off, joining a network, and Bluetooth discoverability. Everything else on the tab is
   still a reading, and the tab says which is which.

   ONE STATUS LINE (`net-write-status`) carries every reply, for the same reason the Recording page
   has one: two live regions firing at once means a reader hears neither properly. These replies
   are worth waiting for - a join can take 45 seconds and the answer is the unit's own IP address -
   so nothing here is announced twice.

   THE PASSWORD IS CLEARED FROM THE FIELD the moment it has been sent, whatever the outcome. It is
   never read back, never prefilled and never stored: the unit keeps it, and this app deliberately
   does not. */

async function netPost(url, body, el, working) {
  if (isBusy(el)) return null;
  setBusy(el, true);
  setText("net-write-status", working);
  let res;
  try {
    res = await postJSON(url, body);
  } catch (e) {
    setBusy(el, false);
    setText("net-write-status", "Could not reach the AccessCaster service.");
    return null;
  }
  setBusy(el, false);
  setText("net-write-status", res.message || "Done.");
  return res;
}

async function onWifiToggle(ev) {
  const box = ev.currentTarget;
  const on = box.checked;
  const res = await netPost("/api/network/wifi", { on },
    box, on ? "Turning the wifi on..." : "Turning the wifi off...");
  // Put the switch back if the unit did not take it, so the page never claims a state the unit
  // is not in. Same contract as sendToggle uses everywhere else.
  if (!res || res.ok === false) box.checked = !on;
}

async function onBtVisibleToggle(ev) {
  const box = ev.currentTarget;
  const on = box.checked;
  const res = await netPost("/api/network/bt-visible", { on }, box,
    on ? "Making the unit discoverable..." : "Turning discoverability off...");
  if (!res || res.ok === false) box.checked = !on;
}

/* Scan for networks, then let one be picked.

   THE PICKER FILLS IN THE NAME BOX rather than replacing it as a second source of truth. One place
   holds the network being joined, so it can be read back, corrected, or typed from scratch when the
   network is hidden or beyond the unit's ten-result limit. Join keeps reading that box and does not
   need to know a scan ever happened.

   The list is only revealed once there is something in it: an empty dropdown in the tab order is a
   control that says nothing when you reach it. */
async function onWifiScan(ev) {
  const btn = ev.currentTarget;
  const res = await netPost("/api/network/wifi/scan", {}, btn, "Scanning for networks...");
  if (!res || res.ok === false) return;
  const sel = document.getElementById("net-scan-list");
  const nets = res.networks || [];
  sel.innerHTML = "";
  nets.forEach((n) => {
    const o = document.createElement("option");
    o.value = n.ssid;
    // One line per network carrying everything that decides which to pick: the name, whether a
    // password is needed, and whether it is the one the unit is already set to.
    o.textContent = (n.spoken || n.ssid) + (n.current ? ", the one this unit is set to" : "");
    sel.appendChild(o);
  });
  document.getElementById("net-scan-row").hidden = !nets.length;
  if (!nets.length) return;
  // Move focus to the results. A scan is a deliberate act with a deliberate next step, and landing
  // on the list means the count that was just announced is followed by the thing it counted.
  sel.focus();
  onScanPick();
}

// Picking from the list writes the name into the box that Join actually reads.
function onScanPick() {
  const sel = document.getElementById("net-scan-list");
  if (!sel.value) return;
  document.getElementById("net-join-ssid").value = sel.value;
}

async function onWifiJoin(ev) {
  const btn = ev.currentTarget;
  const ssidBox = document.getElementById("net-join-ssid");
  const pskBox = document.getElementById("net-join-psk");
  const ssid = (ssidBox.value || "").trim();
  if (!ssid) {
    setText("net-write-status", "Type the name of the network to join first.");
    ssidBox.focus();
    return;
  }
  const password = pskBox.value || "";
  const res = await netPost("/api/network/wifi/join", { ssid, password }, btn,
    "Joining " + ssid + ". This can take up to 45 seconds...");
  // Cleared whatever happened, including on a failure: a password left sitting in a form field is
  // one screen-reader review command away from being read out loud in a room with people in it.
  pskBox.value = "";
  if (res && res.ok) {
    const toggle = document.getElementById("net-wifi-toggle");
    if (toggle) toggle.checked = true;
    loadNetwork();          // the address and the reported SSID have both just changed
  }
}

/* Send an on/off switch change to the device: disable the control, POST the body,
   announce the result, and revert the checkbox to !on if the write failed or the
   service was unreachable, then re-enable. Every switch (MIDI, announcer, phantom,
   polarity, channel effects, processing on, modules) goes through this so the
   announce text and the revert-on-failure behaviour are identical everywhere. */
async function sendToggle(url, body, input, on) {
  setBusy(input, true);   // NOT input.disabled: that would blur the switch you just flipped
  try {
    const res = await postJSON(url, body);
    announce(res.message || "Done.", !res.ok);
    if (!res.ok) input.checked = !on;  // revert the visual on failure
  } catch (err) {
    announce("Could not reach the device service.", true);
    input.checked = !on;
  } finally {
    setBusy(input, false);
  }
}

/* ---- System: MIDI control ------------------------------------------- */

function onMidiToggle(ev) {
  return sendToggle("/api/midi", { on: ev.target.checked }, ev.target, ev.target.checked);
}

function onAnnounceToggle(ev) {
  return sendToggle("/api/announce", { on: ev.target.checked }, ev.target, ev.target.checked);
}

/* Device page: the "current state" overview, the first thing on the page and the reason it exists.
   One device read on entering the page, and no Refresh button: the read is about a second, so
   arriving IS the refresh. Written as plain sentences in a polite live region rather than a table, so a
   screen reader reads it as a short spoken report.

   BY EXCEPTION on purpose. Mute, solo and routing are reported only when there is something to
   report, so a short summary means all is well and there is nothing to wade through. Fader levels
   are the exception to the exception: all of them are listed, because a level has no "normal"
   value you could infer from silence. */
async function loadOverview() {
  const out = document.getElementById("ov-body");
  const head = document.getElementById("ov-heading");
  if (!out) return;
  out.textContent = "";
  const wait = document.createElement("p");
  wait.textContent = "Reading the unit...";
  out.appendChild(wait);
  const lines = [];
  try {
    const resp = await fetch("/api/device/overview");
    const d = await resp.json();
    if (head) {
      head.textContent = d.ok && d.device_name
        ? `${d.device_name} connected. Current state:`
        : "No unit connected. Current state:";
    }
    if (!d.ok) {
      lines.push(d.message || "Could not read the unit.");
    } else {
      // STORAGE FIRST, and only when it is a problem. Everything else on this page describes how
      // the desk sounds; a nearly-full volume is the one thing here that stops it WORKING, and it
      // is the fact you want before you press record rather than after. Silent when there is room,
      // like the routing notes below.
      // A PAD LEFT SWITCHED ON goes first, above even storage. It is the only thing reported on
      // this page that can be changing how you SOUND right now while giving no other sign: a voice
      // effect pad makes no noise of its own, so the symptom is just that you sound wrong.
      // BY EXCEPTION, and it stays that way: do not add "no pads are switched on" to match the way
      // mute and solo always say so. A page that reports nothing about pads IS the healthy case.
      (d.pads_engaged || []).forEach((p) => {
        lines.push(`Pad ${p.grid}, ${p.name}, is switched on and processing a live channel. `
          + "Press that pad on the unit to switch it off, or clear it from the SMART pads page.");
      });
      // A RECORDING IN PROGRESS, reported only when there is one. It is above storage because a
      // recording you did not know about is the thing on this page you would want to act on first.
      if (d.recording) {
        lines.push(d.recording.state === 1
          ? "A recording is PAUSED. The Recording page, Alt plus 4, carries on or stops it."
          : "The unit is RECORDING. The Recording page, Alt plus 4, pauses or stops it.");
      }
      (d.storage_low || []).forEach((v) => {
        lines.push(`${v.label} is nearly full: ${v.free_pct} percent left, about ${v.free_mb} MB.`);
      });
      const muted = d.muted || [];
      const soloed = d.soloed || [];
      lines.push(muted.length
        ? `Muted: ${muted.map((c) => c.label).join(", ")}.`
        : "Nothing is muted.");
      if (soloed.length) {
        lines.push(`Soloed: ${soloed.map((c) => c.label).join(", ")}. While anything is soloed, `
          + "your headphones hear only the soloed channels.");
      } else {
        lines.push("Nothing is soloed.");
      }
      // Levels last within their own sentence run: they are the longest part to listen to.
      const faders = d.faders || [];
      if (!d.levels_ok) {
        lines.push("Fader levels could not be read: the matrix changed shape, which usually means "
          + "a phone caller is connected.");
      } else if (faders.length) {
        lines.push("Fader levels: " + faders.map((f) => {
          if (f.label === "empty") return `fader ${f.fader} empty`;
          if (f.muted) return `${f.label} muted`;
          return `${f.label} ${f.percent === null ? "unknown" : f.percent + " percent"}`;
        }).join(", ") + ".");
      }
      if (d.monitor_percent !== null && d.monitor_percent !== undefined) {
        lines.push(`Monitor speaker level: ${d.monitor_percent} percent.`);
      }
      const rt = d.routing || {};
      if (!rt.ok) {
        lines.push(rt.reason || "The routing could not be read.");
      } else if (!(rt.notes || []).length) {
        lines.push("Routing: nothing unusual, every output is on its usual mix.");
      } else {
        lines.push("Routing, only what differs from the usual:");
        (rt.notes || []).forEach((n) => lines.push(n));
      }
      // "Currently selected" is load-bearing: read on its own, "SMART pad bank: 1"
      // sounds like a setting you are being offered rather than the bank the desk is sitting on now.
      if (d.bank !== null && d.bank !== undefined)
        lines.push(`Currently selected SMART pad bank: ${d.bank}.`);
    }
  } catch (err) {
    lines.push("Could not reach the app service.");
  }
  out.textContent = "";
  lines.forEach((l) => {
    const p = document.createElement("p");
    p.textContent = l;
    out.appendChild(p);
  });
}

/* THE SPOKEN STARTUP SUMMARY: the overview reduced to what is WRONG, in one sentence.
   Deliberately NOT the Device page's list, which is the same payload read the other way round. That
   page answers "tell me everything", and its fader levels alone are nine clauses; this answers "is
   anything up?", which is the only question worth asking before you have started work. So the
   reference facts - every fader position, the monitor level, the pad bank - are left out here and
   stay one Alt+7 away.
   Silent when the desk is not connected: the header's own live region has already said so, and
   saying it twice in two wordings is worse than saying it once. */
function summariseOverview(d) {
  if (!d || !d.ok) return "";
  const alerts = [];
  (d.pads_engaged || []).forEach((p) => {
    alerts.push(`Pad ${p.grid}, ${p.name}, is switched on and processing a live channel`);
  });
  if (d.recording) {
    alerts.push(d.recording.state === 1 ? "a recording is paused" : "the unit is recording");
  }
  (d.storage_low || []).forEach((v) => {
    alerts.push(`${v.label} is nearly full, ${v.free_pct} percent left`);
  });
  const fwu = d.firmware_update || {};
  if (fwu.available) {
    alerts.push((fwu.version ? `firmware ${fwu.version}` : "a firmware update")
      + " is available, on the Device page under Hardware");
  }
  const muted = (d.muted || []).map((c) => c.label);
  const soloed = (d.soloed || []).map((c) => c.label);
  if (muted.length) alerts.push("muted: " + muted.join(", "));
  if (soloed.length) {
    alerts.push("soloed: " + soloed.join(", ") + ", so your headphones hear only those");
  }
  const rt = d.routing || {};
  const routingBroken = rt.ok === false;
  if (routingBroken) alerts.push(rt.reason || "the routing could not be read");
  else (rt.notes || []).forEach((n) => alerts.push(n));

  // EVERY ALERT IS MADE INTO A SENTENCE HERE rather than trusted to be one. They come from several
  // places: some arrive with a full stop (the routing notes, written for the Device page) and some
  // without, and some start lower case. Joined raw they produced "a custom mix.. the unit is
  // recording", which reads fine and sounds wrong.
  const sentence = (t) => {
    const clean = String(t).trim().replace(/\.+$/, "");
    return clean.charAt(0).toUpperCase() + clean.slice(1) + ".";
  };

  // NAME WHAT WAS CHECKED, not just what was wrong. Hearing "Muted: Mic 3, Mic 4" and then silence
  // leaves you unable to tell a clean solo bus from a summary that stops at the first problem it
  // finds. So the clear ones say so, and only the ones not already covered above.
  const clear = [];
  if (!muted.length && !soloed.length) clear.push("nothing is muted or soloed");
  else if (!soloed.length) clear.push("nothing is soloed");
  if (!routingBroken && !(rt.notes || []).length) clear.push("every output is on its usual mix");
  const parts = alerts.map(sentence);
  if (clear.length) parts.push(sentence(clear.join(" and ")));
  // THE PAD BANK IS ALWAYS SAID. It is not an alert - no bank is wrong - but it is a state with no
  // outward sign that changes what every pad on the desk does, so leaving it out means the one fact
  // you cannot discover by listening is also the one the summary skips.
  if (d.bank !== null && d.bank !== undefined) parts.push(`SMART pad bank ${d.bank}.`);
  return parts.join(" ");
}

/* Long enough for the opening page's own device reads to have gone in first. Not a guess at how
   long they take: they queue on the HID lock either way, and this only decides who is in front. */
const STARTUP_SUMMARY_DELAY_MS = 1200;

async function speakStartupSummary() {
  try {
    const resp = await fetch("/api/announce");
    const prefs = await resp.json();
    if (prefs.startup_summary === false) return;
    const ov = await (await fetch("/api/device/overview")).json();
    const text = summariseOverview(ov);
    if (text) announce(text);
  } catch (err) { /* nothing to say is better than an error nobody asked for */ }
}

/* Device page: read-only live state (card, recorder, firmware, wired network). Written as plain
   sentences in a polite live region rather than a table, so a screen reader reads it as a short
   report. Anything the unit cannot tell us honestly says so instead of showing a zero. */
function firmwareLine(fw, upd) {
  const u = upd || {};
  if (!u.available) return fw || "Unknown";
  return (fw || "Unknown") + ". " + (u.version ? `Version ${u.version}` : "An update")
    + " is available.";
}

/* Ask the desk to check RODE's server. The reply is worded by the server; this only shows it. */
async function onFirmwareCheck(btn) {
  if (isBusy(btn)) return;
  setBusy(btn, true);
  setStatus("dev-fw-status", "Checking with RODE, please wait.");
  let res;
  try {
    res = await postJSON("/api/device/firmware-check", {});
  } catch (e) {
    res = { ok: false, message: "Could not reach the AccessCaster service." };
  }
  setBusy(btn, false);
  if (res.ok) setText("dev-firmware", firmwareLine(res.firmware, res.firmware_update));
  setStatus("dev-fw-status", res.message || "Done.");
}

async function loadDeviceState() {
  const btn = document.getElementById("dev-state-btn");
  const out = document.getElementById("dev-state");
  if (!out) return;
  if (btn) setBusy(btn, true);
  out.textContent = "Reading the unit...";
  try {
    const resp = await fetch("/api/device/state");
    const d = await resp.json();
    if (!d.ok) { out.textContent = d.message || "Could not read the unit."; return; }
    const lines = [];
    const vols = (d.storage && d.storage.volumes) || [];
    const cards = vols.filter((v) => !/^Internal/.test(v.label || ""));
    const internal = vols.find((v) => /^Internal/.test(v.label || ""));
    if (!cards.length) lines.push("Storage: the unit reported no card slot or drive.");
    // Percent as well as megabytes, and the "nearly full" judgement in words. Two raw numbers make
    // a bar to glance at; read aloud they are a division you have to do in your head, and the
    // answer only matters when it is bad. The server decides `low` so the page and the Current
    // state overview cannot disagree about what nearly full means.
    const space = (v) => `${v.free_mb} MB free of ${v.capacity_mb} MB`
      + (v.free_pct === null || v.free_pct === undefined ? "" : `, ${v.free_pct} percent left`)
      + (v.low ? ". This is nearly full" : "");
    // "Nothing in it" rather than "no card in": slot 2 is the USB port, where a card would never
    // go, and the slots are named for what they are.
    cards.forEach((v) => {
      lines.push(v.inserted ? `${v.label}: in use, ${space(v)}.` : `${v.label}: nothing in it.`);
    });
    if (internal) {
      lines.push(`${internal.label}: ${space(internal)}.`);
    }
    // The recorder's own state, in the words the Recording page uses. It is a READING here; the
    // driving is on that page.
    const r = d.recorder || {};
    lines.push(`Recorder: ${(r.state_label || "unknown").toLowerCase()}. `
      + `${r.total_recordings || 0} recordings on the unit. `
      + "Start and stop it on the Recording page, Alt plus 4.");
    // Monitor speaker level deliberately does NOT appear here: it is not a hardware status, and it
    // belongs with the other levels on the Current state overview.
    // Wireless mics are not here either. A radio link to another device belongs with the other
    // radio links on the Wireless connections tab, not between a memory card and a recorder
    // (paintWirelessMics).
    //
    // Firmware and the wired network came the other way, from that tab, and are a dl rather than
    // lines in this live region: they are values you look up, not news.
    setText("dev-firmware", firmwareLine(d.firmware, d.firmware_update));
    setText("dev-wired",
      d.wired_connected === true ? "Connected"
      : d.wired_connected === false ? "Not connected" : "Unknown");
    out.textContent = "";
    lines.forEach((l) => {
      const p = document.createElement("p");
      p.textContent = l;
      out.appendChild(p);
    });
  } catch (err) {
    out.textContent = "Could not reach the app service.";
  } finally {
    if (btn) setBusy(btn, false);
  }
}

/* ---- Device page: LEVEL METERS -------------------------------------------------------------
   The whole point of this group is that a spoken meter is not a meter read aloud. So the page is
   built around the on-request CHECK (which never interrupts) with the background warnings as the
   opt-in, and every threshold is a control rather than a constant, because how often this should
   speak is a judgement to make by ear.

   Same tab rules as everywhere else: opening the tab re-reads, so the controls are STRUCTURED ONCE
   and only their values are updated. The stream list goes through fillOptions for the same reason. */
function loadMeters() {
  return fetch("/api/meters").then((r) => r.json()).then((d) => {
    // Unavailable is not an error state here, it is a normal and explainable one: over Remote
    // Desktop the unit's audio belongs to another session, and while the unit is mounted as a drive
    // its audio device disappears. Say which, rather than showing dead controls.
    // The status line carries the mix warning when there is one: metering the unit's whole mix
    // measures this computer's own audio too, which reads as clipping that is not yours.
    setText("meter-status", d.available ? (d.mix_note || "") : (d.reason || "No streams to meter."));
    // Named by DIRECTION, not by Windows' wording: "Microphone (... Main Stereo)" is the desk's
    // output mix and "Speakers (... Main Stereo)" is what this PC sends it, which is a trap when
    // the two differ only by a prefix.
    fillOptions("meter-stream", (d.streams || []).map(
      (s) => ({ value: s.id, label: s.label || s.name })));
    const streamSel = document.getElementById("meter-stream");
    if (streamSel) {
      if (d.chosen && d.chosen_present) streamSel.value = d.chosen;
      // aria-disabled, never the attribute: a natively disabled select LEAVES THE TAB ORDER, so
      // this dropdown vanishes entirely whenever there is nothing to meter (every Remote Desktop
      // session, for one) and Tab sails past it with no explanation.
      setAvailable(streamSel, !!d.available,
        (document.getElementById("meter-status") || {}).textContent
          || "There is nothing to meter at the moment.");
    }
    buildInputLevel(d);
    buildMeterWatchControls(d);
    return d;
  }).catch(() => {
    setText("meter-status", "Could not reach the app service.");
  });
}

/* WINDOWS' OWN LEVEL for the metered stream, in dB.
   It is on this tab because it is the hidden multiplier on everything else here: Windows boosting
   an input makes it clip in Windows while the desk's meters look fine, and Windows attenuating one
   made our own clipping warning unreachable for the whole life of the feature.

   IN dB, NOT PERCENT, AND AS A NUMBER BOX. On this endpoint 50 percent is -1.08 dB and unity is
   about 55 percent, so a percentage is a number you cannot reason about, and dragging cannot land
   on "no change". The button next to it is what most people actually want, in one press.
   Structured once and only its value updated, like every other per-tab read on this page. */
function buildInputLevel(d) {
  const host = document.getElementById("meter-input-level");
  if (!host) return;
  const lvl = d.input_level;
  if (!lvl) {
    // No stream chosen, or Windows will not report a level for it. Say so rather than showing a
    // control that would write to nothing.
    rebuildIfChanged(host, "no-level", (box) => {
      const p = document.createElement("p");
      p.className = "hint";
      p.textContent = d.available
        ? "Choose a stream above and its Windows level will appear here."
        : "";
      box.appendChild(p);
    });
    return;
  }
  rebuildIfChanged(host, "input-level-v1", (box) => {
    const row = document.createElement("div");
    row.className = "field";
    const label = document.createElement("label");
    label.setAttribute("for", "meter-input-db");
    label.textContent = "Windows level, dB";
    const input = document.createElement("input");
    input.type = "number";
    input.id = "meter-input-db";
    input.step = "0.5";
    // NO aria-describedby. A hint attached to a spin button is read in full on EVERY arrow press,
    // which makes the control unwalkable. The hint stays on the page to be read once;
    // meters.level_note speaks up when the level is actually doing damage.
    const hint = document.createElement("span");
    hint.className = "hint";
    hint.id = "meter-input-hint";
    hint.textContent = "Zero means no change. Set your gain on the unit. Affects every "
      + "application on this computer.";
    row.appendChild(label);
    row.appendChild(input);
    row.appendChild(hint);
    box.appendChild(row);
    const p = document.createElement("p");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "secondary";
    btn.id = "meter-input-unity";
    btn.textContent = "Set it to no change";
    p.appendChild(btn);
    box.appendChild(p);
    // WINDOWS' MUTE, which is invisible everywhere else: a muted input reads exactly like a dead
    // microphone, and the fix is in a Windows dialog rather than anywhere on the desk. Both of a
    // unit's inputs can be muted there with nothing anywhere saying so.
    box.appendChild(buildSwitch("meter-input-muted", "Muted in Windows", false,
      (on, sw) => sendToggle("/api/meters/input-mute", { on }, sw, on)));
    const note = document.createElement("p");
    note.id = "meter-input-note";
    note.className = "hint";
    box.appendChild(note);
    input.addEventListener("change", () => sendInputLevel(parseFloat(input.value), input));
    btn.addEventListener("click", () => sendInputLevel(0, btn));
  });
  const input = document.getElementById("meter-input-db");
  if (input) {
    input.min = String(lvl.min_db);
    input.max = String(lvl.max_db);
    input.value = lvl.db.toFixed(1);
    // A FIXED-LEVEL ENDPOINT HAS NO CONTROL TO OFFER, so the spin button and its button are
    // disabled rather than left to fail. Every one of RODE's virtual devices is like this - all
    // nine Fader inputs and System - and there the level lives on the desk's own fader. A live
    // control that silently does nothing is the worst of the three states to land a screen reader
    // on; disabled is announced, and the note below says where the volume actually is.
    input.disabled = !!lvl.fixed;
    const unity = document.getElementById("meter-input-unity");
    if (unity) unity.disabled = !!lvl.fixed;
  }
  const mutedBox = document.getElementById("meter-input-muted");
  if (mutedBox) mutedBox.checked = !!lvl.muted;
  // level_note() only speaks up when the level is doing something worth knowing about, so an empty
  // note here is the healthy case and correctly leaves the line silent. A MUTE always says so:
  // every reading on the tab is meaningless while it is on.
  setText("meter-input-note", lvl.muted
    ? "Muted in Windows: every level here reads zero and nothing recorded from it has any sound."
    : (lvl.note || ""));
}

async function sendInputLevel(db, el) {
  if (isBusy(el) || !isFinite(db)) return;
  setBusy(el, true);
  let res;
  try {
    res = await postJSON("/api/meters/input-level", { db });
  } catch (e) {
    setBusy(el, false);
    announce("Could not reach the app service.", true);
    return;
  }
  setBusy(el, false);
  // The value comes back from a READ-BACK, not from what was asked for: a driver can refuse
  // silently, and this desk's Chat input has done exactly that.
  const input = document.getElementById("meter-input-db");
  if (input && res.level && typeof res.level.db === "number")
    input.value = res.level.db.toFixed(1);
  setText("meter-input-note", (res.level && res.level.note) || "");
  announce(res.message || "Done.", !res.ok);
}

function buildMeterWatchControls(d) {
  const host = document.getElementById("meter-watch-controls");
  if (!host) return;
  const s = d.settings || {};
  rebuildIfChanged(host, "meter-watch-v1", () => {
    host.appendChild(buildSwitch("meter-watch-on", "Watch this stream and warn me", false,
      (on, input) => sendToggle("/api/meters/watch", { on }, input, on)));
    host.appendChild(buildSwitch("meter-warn-clip", "Say when it is clipping", true,
      (on, input) => sendToggle("/api/meters/option",
        { name: "warn_clip", value: on ? 1 : 0 }, input, on)));
    // A SOUND as well as the words. Clipping is the one thing here you react to rather than think
    // about, and a spoken sentence arrives a word at a time.
    host.appendChild(buildSwitch("meter-clip-sound", "Play a sound when it clips", true,
      (on, input) => sendToggle("/api/meters/option",
        { name: "clip_sound", value: on ? 1 : 0 }, input, on)));
    // WHERE the warning fires. True full scale is genuinely hard to reach on this desk, whose own
    // compressor and limiter hold a shouted mic well below it, so a warning there arrives only
    // once the damage is done. Minus 1 warns while there is still something you can do.
    host.appendChild(buildMeterNumber("meter-clip-db", "clip_db",
      "Warn me at, dB", -20, 0,
      "Zero is where sound is actually damaged. The unit's own limiter holds processed speech just "
      + "under that, so a lower setting will fire on every sentence."));
    host.appendChild(buildSwitch("meter-warn-silence",
      "Say when it goes silent, and when it comes back", true,
      (on, input) => sendToggle("/api/meters/option",
        { name: "warn_silence", value: on ? 1 : 0 }, input, on)));
    host.appendChild(buildMeterNumber("meter-silence-after", "silence_after",
      "Seconds of silence before it says so", 1, 60,
      "Five is long enough to ignore a pause for breath."));
    host.appendChild(buildMeterNumber("meter-speak-every", "speak_every",
      "Speak the level every, seconds, 0 for never", 0, 600,
      "Leave at 0 while you are talking. Try 10 or 20 while setting gains."));
  });
  const set = (id, val) => {
    const el = document.getElementById(id);
    if (el && val !== null && val !== undefined) el.checked = !!val;
  };
  set("meter-watch-on", (d.live || {}).running);
  set("meter-warn-clip", s.warn_clip);
  set("meter-clip-sound", s.clip_sound);
  set("meter-warn-silence", s.warn_silence);
  const num = (id, v) => {
    const el = document.getElementById(id);
    if (el && v !== null && v !== undefined && document.activeElement !== el) el.value = String(v);
  };
  num("meter-silence-after", s.silence_after);
  num("meter-speak-every", s.speak_every);
  num("meter-clip-db", s.clip_db);
  const live = d.live || {};
  setText("meter-watch-status", live.error || (live.running
    ? `Watching ${live.name}.` : "Not watching."));
}

/* A NUMBER BOX, not a slider, and the same reasoning as the MIDI control numbers on the pads page:
   these are values you already know ("warn me after 5 seconds"), and arrowing a slider to 5 is a
   different kind of work from typing it. */
function buildMeterNumber(id, option, label, min, max, hint) {
  const row = document.createElement("div");
  row.className = "field";
  const hintId = id + "-hint";
  const lab = document.createElement("label");
  lab.setAttribute("for", id);
  lab.textContent = label;
  row.appendChild(lab);
  const input = document.createElement("input");
  input.type = "number";
  input.id = id;
  input.min = String(min);
  input.max = String(max);
  input.step = "1";
  // NO aria-describedby. On a spin button the hint is read IN FULL on every arrow press, which
  // makes the whole tab exhausting to walk. The hint stays on the page, right after the control,
  // to be read once.
  input.addEventListener("change", async () => {
    const v = Math.max(min, Math.min(max, parseFloat(input.value) || 0));
    input.value = String(v);
    try {
      const res = await postJSON("/api/meters/option", { name: option, value: v });
      announce(res.message || "Done.", !res.ok);
    } catch (e) {
      announce("Could not reach the app service.", true);
    }
  });
  row.appendChild(input);
  const p = document.createElement("p");
  p.className = "hint";
  p.id = hintId;
  p.textContent = hint;
  row.appendChild(p);
  return row;
}

/* ---- Device page: the unit's OWN settings ----------------------------------
   Screen and button brightness, auto-brightness, dim time, meter style, haptics, what the record
   button does, listen mode, and the monitor's fixed output level. Every one of these is otherwise
   reachable ONLY from the unit's touchscreen; their write addresses are captured and read-back
   verified.

   The server sends the SPEC as well as the values (label, kind, choices/range, and a plain-English
   detail line), so the controls are built from one definition rather than hand-written twice. A
   setting added on the server appears here, in a profile, and in the profile's coverage audit.

   READ ON SHOW, not on a timer and not from a cache: this reads the unit whenever the group is
   opened, and on entering the page if the group was left open. Nothing here is pushed by the desk,
   so a value changed on the touchscreen would otherwise sit stale. There is deliberately no Refresh
   button - opening the group IS the refresh. */
let unitSettingsWritable = true;
const unitDebounce = {};        // per-slider write timers, so arrowing a brightness does not flood USB

/* Why every one of these controls is unavailable on a desk that cannot take the writes. One string,
   because the switch, the select and the slider must all give the SAME reason - three wordings for
   one cause is how a page starts sounding like three pages. */
const UNIT_SETTINGS_OFF = "Changing the unit's settings is not enabled on this device yet. "
  + "You can still read what it has.";

/* aria-disabled leaves these controls in the tab order, so a change event still ARRIVES at their
   handlers. Each one calls refuseChange and then puts itself back to what the unit really has - a
   control left showing the value somebody just chose would report a state the desk is not in. */

/* What the currently built controls cover. A read whose spec matches this one UPDATES the controls
   in place; only a different spec (a different unit, or a firmware that reports a different set)
   rebuilds them. */
let _unitBuiltKey = null;

function unitSpecKey(d) {
  return (d.settings || []).map((s) => s.prop + ":" + s.kind).join(",")
    + "|" + (d.writable !== false);
}

async function loadUnitSettings() {
  const host = document.getElementById("dev-unit-controls");
  if (!host) return;
  setText("dev-unit-status", "Reading the unit's settings...");
  let d;
  try {
    const resp = await fetch("/api/device/settings");
    d = await resp.json();
  } catch (err) {
    setText("dev-unit-status", "Could not reach the app service.");
    return;
  }
  if (!d.ok) {
    setText("dev-unit-status", d.message || "Could not read the unit's settings.");
    return;
  }
  unitSettingsWritable = d.writable !== false;
  // The MIDI switch sits at the bottom of this tab and rides on this read. It is not part of
  // the built spec above, so it is set here rather than in applyUnitSettingValues, and an unknown
  // value leaves the switch alone rather than showing a confident "off".
  const midiToggle = document.getElementById("midi-toggle");
  if (midiToggle && d.midi !== null && d.midi !== undefined) midiToggle.checked = !!d.midi;
  // STRUCTURE ONCE, VALUES EVERY TIME - the same rule the Routing page follows. Rebuilding these ten
  // controls on every read replaces the DOM underneath a screen reader that is still reading the
  // panel, and it comes out as fragments of settings in a random order: "Never", "Default", "On
  // button press" on opening the tab.
  const spec = unitSpecKey(d);
  if (spec !== _unitBuiltKey) {
    await keepingFocus(async () => {
      host.textContent = "";
      (d.settings || []).forEach((s) => host.appendChild(buildUnitSetting(s)));
    });
    _unitBuiltKey = spec;
  } else {
    applyUnitSettingValues(d);
  }
  setText("dev-unit-status", unitSettingsWritable ? ""
    : `These settings can be read but not changed on the ${d.device_name || "connected unit"}: `
      + "changing them has only ever been confirmed on a RODECaster Pro 2, so AccessCaster "
      + "leaves them alone rather than risk changing the wrong one.");
}

/* Put the unit's current values onto controls that are ALREADY on the page: no element is created,
   moved or destroyed, so re-opening the tab cannot interrupt a screen reader mid-sentence. Anything
   a build decides (the hint's "did not report this" wording, the disabled state on a read-only unit,
   an off-list value needing its own option) is decided again here, or the two paths would drift. */
function applyUnitSettingValues(d) {
  (d.settings || []).forEach((s) => {
    const el = document.getElementById("unit-" + s.prop);
    if (!el) return;
    const known = s.value !== null && s.value !== undefined;
    if (s.kind === "bool") {
      el.checked = s.value === 1;
    } else if (s.kind === "choice") {
      if (known) {
        const want = String(s.value);
        if (!Array.from(el.options).some((o) => o.value === want)) {
          const opt = document.createElement("option");
          opt.value = want;
          opt.textContent = "Currently " + s.value;      // an off-list value, never a silent lie
          el.appendChild(opt);
        }
        el.value = want;
        el.dataset.prev = want;                          // what a failed write reverts to
      }
    } else if (known) {
      el.value = String(s.value);
      el.dataset.prev = String(s.value);                 // what a refused slider write reverts to
      if (el._refreshValueText) el._refreshValueText();  // aria-valuetext and the visible number
    }
    // aria-disabled, NOT the attribute: a natively disabled control drops out of the tab order and
    // takes its description with it, so a Duo owner arrowing the Unit settings tab meets a silent
    // gap where the brightness should be instead of hearing why it cannot be changed.
    setAvailable(el, unitSettingsWritable, UNIT_SETTINGS_OFF);
    const hint = document.getElementById("unit-" + s.prop + "-hint");
    if (hint) hint.textContent = unitSettingHint(s, known);
  });
}

/* ONE wording for "the unit did not report this", shared by the build and the in-place update. */
function unitSettingHint(s, known) {
  return known ? s.detail
    : s.detail + " The unit did not report this setting, so the control shows a default rather "
      + "than its real value.";
}

/* One setting: a switch, a select or a slider, plus its own description. The description is a real
   <p> associated with aria-describedby rather than a title attribute, so every screen reader reads
   it and it is available in browse mode too. */
function buildUnitSetting(s) {
  const wrap = document.createElement("div");
  wrap.className = "field-block";
  const id = "unit-" + s.prop;
  const hintId = id + "-hint";
  const known = s.value !== null && s.value !== undefined;

  let control;
  if (s.kind === "bool") {
    control = buildSwitch(id, s.label, s.value === 1,
      (on, input) => {
        // The switch goes BACK on a refusal, or it claims a setting the unit does not have.
        if (refuseChange(input) !== null) { input.checked = !on; return; }
        sendToggle("/api/device/setting", { prop: s.prop, value: on ? 1 : 0 }, input, on);
      });
  } else if (s.kind === "choice") {
    control = buildUnitChoice(s, id, known);
  } else {
    control = buildUnitSlider(s, id, known);
  }
  const input = control.querySelector("input, select");
  if (input) {
    input.setAttribute("aria-describedby", hintId);
    setAvailable(input, unitSettingsWritable, UNIT_SETTINGS_OFF);
  }
  wrap.appendChild(control);

  const hint = document.createElement("p");
  hint.className = "hint";
  hint.id = hintId;
  hint.textContent = unitSettingHint(s, known);
  wrap.appendChild(hint);
  return wrap;
}

/* A select for a setting with named values. A value the unit reports that is NOT one of the listed
   choices is added as its own option rather than silently showing the wrong one: the dim timeout in
   particular can hold any number of seconds, and quietly reading back "Never" for a real 45 would
   be a lie. */
function buildUnitChoice(s, id, known) {
  const row = document.createElement("div");
  row.className = "field";
  const label = document.createElement("label");
  label.setAttribute("for", id);
  label.textContent = s.label;
  row.appendChild(label);

  const sel = document.createElement("select");
  sel.id = id;
  let matched = false;
  (s.choices || []).forEach(([val, name]) => {
    const opt = document.createElement("option");
    opt.value = String(val);
    opt.textContent = name;
    if (known && Number(s.value) === Number(val)) { opt.selected = true; matched = true; }
    sel.appendChild(opt);
  });
  if (known && !matched) {
    const opt = document.createElement("option");
    opt.value = String(s.value);
    opt.textContent = "Currently " + s.value;
    opt.selected = true;
    sel.appendChild(opt);
  }
  // What a failed write reverts to. Kept ON THE ELEMENT rather than in this closure, because a
  // re-read now updates the control in place and would otherwise leave the closure holding the value
  // this select had when it was first built.
  sel.dataset.prev = sel.value;
  sel.addEventListener("change", async () => {
    const chosen = sel.value;
    if (refuseChange(sel) !== null) { sel.value = sel.dataset.prev; return; }
    setBusy(sel, true);
    try {
      const res = await postJSON("/api/device/setting",
                                 { prop: s.prop, value: parseInt(chosen, 10) });
      announce(res.message || "Done.", !res.ok);
      if (res.ok) sel.dataset.prev = chosen; else sel.value = sel.dataset.prev;
    } catch (err) {
      announce("Could not reach the device service.", true);
      sel.value = sel.dataset.prev;
    } finally {
      setBusy(sel, false);
    }
  });
  row.appendChild(sel);
  return row;
}

/* A slider for a 0-255 brightness. Spoken as "180 of 255, about 71 percent": the raw number is what
   the unit stores and what a profile restores, and the percentage is the part a person can picture.
   Debounced like every other slider here so arrowing across the range does not flood USB. */
function buildUnitSlider(s, id, known) {
  const row = document.createElement("div");
  row.className = "field proc-field";
  const label = document.createElement("label");
  label.setAttribute("for", id);
  label.textContent = s.label;
  row.appendChild(label);

  const input = document.createElement("input");
  input.type = "range";
  input.id = id;
  input.min = String(s.min);
  input.max = String(s.max);
  input.step = String(s.step || 1);
  input.value = String(known ? s.value : s.max);
  input.dataset.prev = input.value;      // what a refused write reverts to; see applyUnitSettingValues
  const out = document.createElement("output");
  out.setAttribute("for", id);
  out.className = "proc-value";
  // <output> is a live region by default (its implicit role is status), so this visible copy of the
  // value would be SPOKEN on its own every time it changed or was inserted, on top of the slider's
  // own aria-valuetext. It is here for sighted use only; the same rule the level sliders follow.
  out.setAttribute("aria-hidden", "true");
  out.setAttribute("aria-live", "off");
  const span = Math.max(1, Number(s.max) - Number(s.min));
  const show = () => {
    const v = parseInt(input.value, 10);
    const pct = Math.round(((v - Number(s.min)) / span) * 100);
    input.setAttribute("aria-valuetext", `${v} of ${s.max}, about ${pct} percent`);
    out.textContent = v + " of " + s.max;
  };
  show();
  input.addEventListener("input", show);
  // Reachable from applyUnitSettingValues, which sets the slider's value without a user gesture and
  // must refresh what a screen reader reads off it.
  input._refreshValueText = show;
  input.addEventListener("change", () => {
    const v = parseInt(input.value, 10);
    if (Number.isNaN(v)) return;
    // Back to the unit's own number, and SAY why: a slider left where it was dragged reads out as
    // the setting having changed.
    if (refuseChange(input) !== null) { input.value = input.dataset.prev; show(); return; }
    window.clearTimeout(unitDebounce[id]);
    unitDebounce[id] = window.setTimeout(async () => {
      try {
        const res = await postJSON("/api/device/setting", { prop: s.prop, value: v });
        if (res && !res.ok) announce(res.message || "Could not set it.", true);
      } catch (err) { announce("Could not reach the device service.", true); }
    }, WRITE_DEBOUNCE_MS);
  });
  row.appendChild(input);
  row.appendChild(out);
  return row;
}

/* ---- Device page: the tab strip, and reading per tab ---------------------
   Six tabs, and deliberately not more: MIDI is one switch and a unit setting, so it lives on Unit
   settings, and Technical was three facts of the kind Hardware already carries. Each tab reads its
   own part of the unit when it is opened, which is the same rule the Routing page uses: that read is
   a full model dump, and paying for it while nobody is looking would slow down the page the app
   opens on.
   HARDWARE IS THE ONE TAB THAT TAKES TWO CALLS, and neither is a dump it could have shared: the
   folded-in Technical part comes from /api/status, which is a USB enumeration rather than a model
   read, and page ENTRY takes it anyway for the connection line and the window title. (The Help &
   About version does not depend on it: see loadVersion.) The MIDI switch's value rides in
   /api/device/settings so Unit settings still makes exactly one call.
   Reads are debounced, because selection follows the arrow keys and arrowing along the strip must
   not fire a read per tab. */
let deviceTabs = null;
let _deviceTabReadTimer = null;

const DEVICE_TAB_READ = {
  state: () => loadOverview(),
  hardware: () => Promise.all([loadDeviceState(), loadStatus(false)]),
  unit: () => loadUnitSettings(),
  speak: () => loadAnnounceState(),
  shortcuts: () => loadHotkeys(),
  levels: () => loadMeters(),
  network: () => loadNetwork(),
  // Reads nothing from the DESK: the link is this app's own, so opening the tab is free.
  access: () => loadAccess(),
};

function wireDeviceTabs() {
  deviceTabs = buildStaticTabs("device", "device-panel", { onSelect: (key) => {
      clearTimeout(_deviceTabReadTimer);
      _deviceTabReadTimer = window.setTimeout(() => readDeviceTab(key), 350);
    },
  });
}

async function readDeviceTab(key) {
  const read = DEVICE_TAB_READ[key];
  if (!read) return;
  try { await read(); } catch (e) { /* each loader reports its own failure */ }
}

/* Page entry: the status read always happens (it is the connection line and the window title), then
   the tab you arrived on reads its own part on top of that. */
async function readDevice() {
  clearTimeout(_deviceTabReadTimer);          // a pending tab read from the last visit is stale
  loadStatus(false);
  const key = deviceTabs ? deviceTabs.current() : "state";
  // Hardware's second call IS the status read the line above just made, so on entry it takes only
  // its own half. This is the same skip the Technical tab had before it was folded into Hardware.
  if (key === "hardware") { await loadDeviceState(); return; }
  await readDeviceTab(key);
}

function onAnnounceStartupSummaryToggle(ev) {
  return sendToggle("/api/announce/startup-summary", { on: ev.target.checked }, ev.target,
                    ev.target.checked);
}

function onAnnounceMuteTonesToggle(ev) {
  return sendToggle("/api/announce/mute-tones", { on: ev.target.checked }, ev.target,
                    ev.target.checked);
}

function onAnnounceLevelsToggle(ev) {
  return sendToggle("/api/announce/levels", { on: ev.target.checked }, ev.target,
                    ev.target.checked);
}

async function loadAnnounceState() {
  try {
    const resp = await fetch("/api/announce");
    const data = await resp.json();
    const el = document.getElementById("announce-toggle");
    if (el) el.checked = !!data.on;
    const lv = document.getElementById("announce-levels-toggle");
    if (lv) lv.checked = data.levels !== false;
    // Defaults the other way round to levels: speech unless asked otherwise, so an older server
    // that does not send the field at all reads as off rather than silently changing what mute does.
    const mt = document.getElementById("announce-mute-tones-toggle");
    if (mt) mt.checked = data.mute_tones === true;
    const ss = document.getElementById("announce-startup-summary-toggle");
    if (ss) ss.checked = data.startup_summary !== false;
    if (typeof data.volume === "number") setAnnounceVolumeDisplay(data.volume);
    fillSpeechChoices(data);
  } catch (err) { /* leave the controls as-is if the service is unreachable */ }
}

/* The voice and output lists come from SAPI itself, the same engine the announcer speaks through, so
   what is offered is exactly what can be chosen. Rebuilt only when the list has actually CHANGED:
   these two live in the tab you sit on to listen, and replacing a select you are on would move you
   off it. "Windows default" is a real choice, not a placeholder, so it carries an empty value. */
function fillSpeechChoices(data) {
  const missing = [];
  const fill = (id, names, current, defaultLabel, what) => {
    const sel = document.getElementById(id);
    if (!sel) return;
    // A service that reports no list at all (an older build still running, or a machine with no
    // speech stack) must still leave a usable control saying what it can do. An EMPTY dropdown is
    // the one thing this must never render: there is nothing to read and nothing to explain it.
    if (!Array.isArray(names)) { names = []; missing.push(what); }
    const want = [""].concat(names).join("\n");
    if (sel.dataset.filled !== want) {
      sel.innerHTML = "";
      [["", defaultLabel]].concat(names.map((n) => [n, n])).forEach(([val, label]) => {
        const o = document.createElement("option");
        o.value = val;
        o.textContent = label;
        sel.appendChild(o);
      });
      sel.dataset.filled = want;
    }
    sel.value = names.indexOf(current) >= 0 ? current : "";
    // With nothing to choose between, the one option left IS the current setting. Marked
    // unavailable rather than pretending to offer a choice - but NOT natively disabled, which
    // would take the control out of the tab order and out of the page with no explanation.
    setAvailable(sel, !!names.length,
      "Windows reported no " + what + " to this computer, so there is nothing to choose.");
  };
  fill("announce-voice", data.voices, data.voice || "", "Windows default voice", "voices");
  fill("announce-output", data.outputs, data.output || "", "Windows default output", "outputs");
  if (missing.length) {
    setStatus("announce-speech-status",
      "The app service did not report the available " + missing.join(" or ")
      + ", so only the Windows default is on offer. If you have just updated AccessCaster, close it "
      + "and start it again: this part of the app is served by the app service, not the page, so "
      + "reloading the window is not enough.");
  }
}

/* Both of these restart the speech worker on the server, and the confirmation is spoken THROUGH the
   new voice and the new output, which is the real test: if you cannot hear it, that output is not
   the one you are listening to. The written status says the same thing for the record. */
async function onAnnounceVoiceChange(ev) {
  const sel = ev.target;
  setBusy(sel, true);
  try {
    const res = await postJSON("/api/announce/voice", { voice: sel.value });
    setStatus("announce-speech-status", res.message || "Could not change the voice.");
  } catch (e) {
    setStatus("announce-speech-status", "Could not reach the app service.");
  } finally {
    setBusy(sel, false);
  }
}

async function onAnnounceOutputChange(ev) {
  const sel = ev.target;
  setBusy(sel, true);
  try {
    const res = await postJSON("/api/announce/output", { output: sel.value });
    setStatus("announce-speech-status", res.message || "Could not change the output.");
  } catch (e) {
    setStatus("announce-speech-status", "Could not reach the app service.");
  } finally {
    setBusy(sel, false);
  }
}

async function onAnnounceDevicesRefresh(ev) {
  const btn = ev.currentTarget;
  if (isBusy(btn)) return;
  setBusy(btn, true);
  try {
    const res = await postJSON("/api/announce/devices", {});
    await loadAnnounceState();
    setStatus("announce-speech-status", res.message || "Done.");
  } catch (e) {
    announce("Could not reach the app service.", true);
  } finally {
    setBusy(btn, false);
  }
}

/* Announcement volume. SAPI stream volume on the server side, so it scales our speech ONLY and
   never Windows volume or the desk's audio. Debounced like the pad volume slider: dragging a range
   input fires on every step, and each commit speaks a confirmation, so writing per step would talk
   over itself the whole way along. */
const ANNOUNCE_VOL_DEBOUNCE_MS = 400;
let _annVolDebounce = null;

function setAnnounceVolumeDisplay(percent) {
  const el = document.getElementById("announce-volume");
  if (!el) return;
  el.value = String(percent);
  setText("announce-volume-value", percent + "%");
  // aria-valuetext so a screen reader reads "60 percent" rather than a bare "60" on a slider whose
  // unit is not otherwise announced.
  el.setAttribute("aria-valuetext", percent === 0 ? "silent" : percent + " percent");
}

function onAnnounceVolumeInput() {
  const el = document.getElementById("announce-volume");
  if (!el) return;
  setAnnounceVolumeDisplay(parseInt(el.value, 10));
  if (_annVolDebounce) clearTimeout(_annVolDebounce);
  _annVolDebounce = setTimeout(commitAnnounceVolume, ANNOUNCE_VOL_DEBOUNCE_MS);
}

async function commitAnnounceVolume() {
  _annVolDebounce = null;
  const el = document.getElementById("announce-volume");
  if (!el) return;
  const percent = parseInt(el.value, 10);
  try {
    const res = await postJSON("/api/announce/volume", { percent });
    // At 0 the server speaks nothing, so the only feedback is here; above 0 the spoken
    // confirmation arrives at the new level and saying it here as well would double it up.
    if (res.ok && percent === 0) announce("Announcements are silenced.");
  } catch (e) {
    announce("Could not set the announcement volume.", true);
  }
}

/* ===== Device page: global keyboard shortcuts =====
   Reads GET /api/hotkeys and writes to /api/hotkeys/{set,add,delete}. The SERVER owns everything
   that can be got wrong: what commands exist (commands.py, generated from the app's own spec
   tables), whether a combination is legal, and whether Windows will accept it. This page shows what
   it is told and shows what comes back, so the two can never reach different answers.

   THE LIST IS THE USER'S. Version one had ten fixed commands and a row of controls each, which was
   about seventy controls to walk past and could not answer the real ask - any control the app has
   should be bindable. Now: a dropdown of the shortcuts you have added, Add and Delete beside it,
   and ONE set of controls that edits whichever is picked.

   THE EDITOR IS STATIC, IN index.html, and only its VALUES change. That is what makes picking a
   different shortcut safe: nothing is rebuilt, so a screen reader is never standing on a node that
   gets replaced. */
let hotkeyData = null;
let hotkeyCommands = null;      // the catalogue, read once when the Add dialog is first opened
let _hotkeyReturnFocus = null;

const HOTKEY_MOD_IDS = ["ctrl", "alt", "win", "shift"];

async function loadHotkeys() {
  let data;
  try {
    const resp = await fetch("/api/hotkeys");
    data = await resp.json();
  } catch (err) {
    setText("hotkeys-status", "Could not reach the app service, so the shortcuts cannot be read.");
    return;
  }
  applyHotkeyState(data);
}

/* One place that takes a /api/hotkeys payload and puts the whole tab in that state, so a read, a
   write's reply, an add and a delete all land the same way. */
function applyHotkeyState(data, keepId) {
  hotkeyData = data;
  const sel = document.getElementById("hotkey-pick");
  const want = keepId || (sel && sel.value);
  fillOptions("hotkey-pick", (data.shortcuts || []).map(
    (s) => ({ value: s.id, label: hotkeyPickLabel(s) })));
  if (sel && want && [...sel.options].some((o) => o.value === want)) sel.value = want;
  const master = document.getElementById("hotkeys-enabled");
  if (master && document.activeElement !== master) master.checked = data.enabled !== false;
  showHotkey(currentHotkey());
  setText("hotkeys-status", data.enabled === false
    ? "Shortcuts are off, so none of these combinations is in use." : "");
}

/* Each option says what the shortcut does AND what it is on, so the list alone answers "what are my
   shortcuts" without opening anything. */
function hotkeyPickLabel(s) {
  return s.label + (s.enabled ? ": " + s.combo : ": off");
}

function currentHotkey() {
  const sel = document.getElementById("hotkey-pick");
  const rows = (hotkeyData && hotkeyData.shortcuts) || [];
  return rows.find((s) => s.id === (sel && sel.value)) || rows[0] || null;
}

/* Paint the editor for one shortcut. VALUES ONLY: every control already exists. The control that
   has focus is left alone - normally that is the picker itself, and the whole point is that
   everything else updates under it. */
function showHotkey(s) {
  const editor = document.getElementById("hotkey-editor");
  if (!editor) return;
  const has = !!s;
  // With no shortcuts at all there is nothing to edit, and controls that edit nothing would still
  // be in the tab order. setAvailable keeps them readable and says why, which the raw disabled
  // attribute cannot.
  ["hotkey-key", "hotkey-off", "hotkey-speak", "hotkey-delete-btn"]
    .concat(HOTKEY_MOD_IDS.map((m) => "hotkey-mod-" + m))
    .forEach((id) => {
      const el = document.getElementById(id);
      if (el) setAvailable(el, has, "You have no shortcuts yet. Add one first.");
    });
  if (!has) {
    setText("hotkey-editor-name", "No shortcuts yet");
    setText("hotkey-detail-status", "Press Add a shortcut to make one.");
    return;
  }
  setText("hotkey-editor-name", s.label);
  setText("hotkey-detail-status", hotkeyRowStatus(s, hotkeyData || {}));

  HOTKEY_MOD_IDS.forEach((m) => {
    const cb = document.getElementById("hotkey-mod-" + m);
    if (cb && document.activeElement !== cb) cb.checked = (s.mods || []).indexOf(m) >= 0;
  });
  const keys = fillOptions("hotkey-key",
    (((hotkeyData || {}).keys) || []).map((k) => ({ value: k, label: k })));
  if (keys && document.activeElement !== keys && s.key) keys.value = s.key;
  // THE SWITCH IS INVERTED: it reads "Disable this shortcut", so ticked means NOT enabled.
  const off = document.getElementById("hotkey-off");
  if (off && document.activeElement !== off) off.checked = !s.enabled;
  const say = document.getElementById("hotkey-speak");
  if (say && document.activeElement !== say) say.checked = s.speak !== false;
}

function hotkeyRowStatus(s, data) {
  if (s.problem) return s.problem;
  if (!s.enabled) return "Off.";
  if (data.enabled === false) return "Shortcuts are switched off.";
  return "Press " + s.combo + (s.speak === false ? ". It will not say so." : ".");
}

function onHotkeyPick() {
  showHotkey(currentHotkey());
}

/* Read the editor back and send the WHOLE binding, because a modifier on its own is not a shortcut:
   the server has to see the combination to judge it. A rejected change is NOT applied, so the
   editor is repainted from the state the reply carries. */
async function saveHotkey() {
  const s = currentHotkey();
  if (!s) return;
  const mods = HOTKEY_MOD_IDS.filter((m) => {
    const cb = document.getElementById("hotkey-mod-" + m);
    return cb && cb.checked;
  });
  const key = document.getElementById("hotkey-key");
  const off = document.getElementById("hotkey-off");
  const say = document.getElementById("hotkey-speak");
  if (!key || !off) return;
  try {
    const res = await postJSON("/api/hotkeys/set", {
      // Inverted here, and in showHotkey, and NOWHERE ELSE: the server only ever sees `enabled`.
      id: s.id, mods, key: key.value, enabled: !off.checked, speak: !say || say.checked,
    });
    announce(res.message || "Done.", !res.ok);
    if (res.state) {
      applyHotkeyState(res.state, s.id);
      if (!res.ok) forceHotkeyValues(currentHotkey());
    }
  } catch (err) {
    announce("Could not reach the app service.", true);
  }
}

/* showHotkey deliberately skips the control that has focus - which, after a rejected change, is the
   very control that has to be put back. So a refusal writes those unconditionally. */
function forceHotkeyValues(s) {
  if (!s) return;
  HOTKEY_MOD_IDS.forEach((m) => {
    const cb = document.getElementById("hotkey-mod-" + m);
    if (cb) cb.checked = (s.mods || []).indexOf(m) >= 0;
  });
  const key = document.getElementById("hotkey-key");
  if (key && s.key) key.value = s.key;
  const off = document.getElementById("hotkey-off");
  if (off) off.checked = !s.enabled;
  const say = document.getElementById("hotkey-speak");
  if (say) say.checked = s.speak !== false;
}

function onHotkeysEnabledToggle(ev) {
  const input = ev.target;
  const on = input.checked;
  setBusy(input, true);
  return postJSON("/api/hotkeys/enabled", { on })
    .then((res) => {
      announce(res.message || "Done.", !res.ok);
      if (res.state) applyHotkeyState(res.state);
      if (!res.ok) input.checked = !on;
    })
    .catch(() => {
      announce("Could not reach the app service.", true);
      input.checked = !on;
    })
    .finally(() => setBusy(input, false));
}

/* ---- Adding a shortcut: category, then command ------------------------
   The catalogue is a separate, slower read (it looks at the pads and the presets on the unit), so
   it is fetched when the dialog is first opened rather than on every visit to the tab, and kept for
   the session. Opening the dialog with nothing loaded says so rather than showing an empty list. */
async function onHotkeyAddOpen() {
  _hotkeyReturnFocus = document.getElementById("hotkey-add-btn");
  const dlg = document.getElementById("hotkey-add-dialog");
  setText("hotkey-add-status", "Reading what can be put on a shortcut...");
  fillOptions("hotkey-add-category", []);
  fillOptions("hotkey-add-command", []);
  dlg.showModal();
  // Focus the first thing there is to DO. Focusing the heading instead means the whole dialog is
  // read out and then its title again before you reach anything.
  document.getElementById("hotkey-add-category").focus();
  if (!hotkeyCommands) {
    try {
      const resp = await fetch("/api/hotkeys/commands");
      hotkeyCommands = await resp.json();
    } catch (err) {
      setText("hotkey-add-status", "Could not reach the app service.");
      return;
    }
  }
  const groups = (hotkeyCommands && hotkeyCommands.groups) || [];
  if (!groups.length) {
    setText("hotkey-add-status", hotkeyCommands.message || "Nothing could be listed.");
    return;
  }
  fillOptions("hotkey-add-category", groups.map((g) => ({ value: g.category, label: g.category })));
  fillHotkeyAddCommands();
  setText("hotkey-add-status", hotkeyCommands.message || "");
}

function fillHotkeyAddCommands() {
  const cat = document.getElementById("hotkey-add-category").value;
  const group = ((hotkeyCommands && hotkeyCommands.groups) || [])
    .find((g) => g.category === cat);
  fillOptions("hotkey-add-command",
    ((group && group.commands) || []).map((c) => ({ value: c.id, label: c.label })));
}

function closeHotkeyDialog(id) {
  const dlg = document.getElementById(id);
  if (dlg.open) dlg.close();
  if (_hotkeyReturnFocus) { try { _hotkeyReturnFocus.focus(); } catch (e) { /* gone */ } }
  _hotkeyReturnFocus = null;
}

async function onHotkeyAddConfirm() {
  const sel = document.getElementById("hotkey-add-command");
  const command = sel && sel.value;
  if (!command) { setText("hotkey-add-status", "Choose what the shortcut should do first."); return; }
  try {
    const res = await postJSON("/api/hotkeys/add", { command });
    announce(res.message || "Done.", !res.ok);
    if (res.state) applyHotkeyState(res.state, res.id);
    closeHotkeyDialog("hotkey-add-dialog");
    // Land on the thing just made, so the keys can be changed straight away without hunting.
    const pick = document.getElementById("hotkey-pick");
    if (pick) pick.focus();
  } catch (err) {
    setText("hotkey-add-status", "Could not reach the app service.");
  }
}

/* Add the whole pad set at once. Safe to press twice: the server skips anything already bound and
   says what it skipped, so this never quietly makes duplicates or overwrites a key you chose. */
async function onHotkeyAddPads() {
  const btn = document.getElementById("hotkey-add-pads-btn");
  setBusy(btn, true);
  try {
    const res = await postJSON("/api/hotkeys/add-pads", {});
    announce(res.message || "Done.", !res.ok);
    if (res.state) applyHotkeyState(res.state);
  } catch (err) {
    announce("Could not reach the app service.", true);
  } finally {
    setBusy(btn, false);
  }
}

function onHotkeyDeleteOpen() {
  const s = currentHotkey();
  if (!s) { announce("There is no shortcut to delete.", true); return; }
  _hotkeyReturnFocus = document.getElementById("hotkey-delete-btn");
  setText("hotkey-delete-name", s.label);
  document.getElementById("hotkey-delete-dialog").showModal();
  // Cancel first: the safe action is the one already focused.
  document.getElementById("hotkey-delete-cancel").focus();
}

async function onHotkeyDeleteConfirm() {
  const s = currentHotkey();
  closeHotkeyDialog("hotkey-delete-dialog");
  if (!s) return;
  try {
    const res = await postJSON("/api/hotkeys/delete", { id: s.id });
    announce(res.message || "Done.", !res.ok);
    if (res.state) applyHotkeyState(res.state);
    const pick = document.getElementById("hotkey-pick");
    if (pick) pick.focus();
  } catch (err) {
    announce("Could not reach the app service.", true);
  }
}

function wireHotkeys() {
  document.getElementById("hotkeys-enabled").addEventListener("change", onHotkeysEnabledToggle);
  document.getElementById("hotkey-pick").addEventListener("change", onHotkeyPick);
  ["hotkey-key", "hotkey-off", "hotkey-speak"]
    .concat(HOTKEY_MOD_IDS.map((m) => "hotkey-mod-" + m))
    .forEach((id) => document.getElementById(id).addEventListener("change", saveHotkey));
  document.getElementById("hotkey-add-btn").addEventListener("click", onHotkeyAddOpen);
  document.getElementById("hotkey-add-pads-btn").addEventListener("click", onHotkeyAddPads);
  document.getElementById("hotkey-add-category").addEventListener("change", fillHotkeyAddCommands);
  document.getElementById("hotkey-add-confirm").addEventListener("click", onHotkeyAddConfirm);
  document.getElementById("hotkey-add-cancel")
          .addEventListener("click", () => closeHotkeyDialog("hotkey-add-dialog"));
  // ESCAPE CLOSES IT IN ONE PRESS, and puts focus back on the button that opened it. Letting the
  // dialog close ITSELF leaves focus wherever the browser chose, and it then takes several presses
  // to get out. Taking the cancel event and doing both halves ourselves is the same pattern the pad
  // confirmation uses.
  document.getElementById("hotkey-add-dialog").addEventListener("cancel", (ev) => {
    ev.preventDefault();
    closeHotkeyDialog("hotkey-add-dialog");
  });
  document.getElementById("hotkey-delete-btn").addEventListener("click", onHotkeyDeleteOpen);
  document.getElementById("hotkey-delete-confirm").addEventListener("click", onHotkeyDeleteConfirm);
  document.getElementById("hotkey-delete-cancel")
          .addEventListener("click", () => closeHotkeyDialog("hotkey-delete-dialog"));
  document.getElementById("hotkey-delete-dialog").addEventListener("cancel", (ev) => {
    ev.preventDefault();
    closeHotkeyDialog("hotkey-delete-dialog");
  });
}


/* ---- Mic channels: gain + phantom ----------------------------------- */

/* ===== Channels: one adaptive panel per channel (mics + Bluetooth/USB/Chat) =====
   Driven by GET /api/channels. Mic channels add gain/phantom/polarity/type/effects;
   line channels (Bluetooth, USB 1, Chat, USB 2) have a Level. EVERY channel here has
   full processing (master on/off, module toggles, parameter sliders), mirroring the
   RODE app's per-channel panel. Writes go to /api/channels/* (level/processing-onoff/
   module/param/effect) and, for mic-only switches, /api/channel/phantom|polarity|mic-type. */
let channelsData = null;        // last /api/channels payload
let currentChannelName = null;  // the channel shown in the panel
let channelPresets = [];        // mic processing presets
const channelDebounce = {};     // per-control timers so dragging/arrowing doesn't flood USB
const MIC_NAME_TO_INDEX = { Mic1: 1, Mic2: 2, Mic3: 3, Mic4: 4 };

async function loadChannels(focusProcessing) {
  setText("mixer-note", "Reading channels from the device...");
  let data;
  try {
    const resp = await fetch("/api/channels");
    data = await resp.json();
  } catch (err) {
    setText("mixer-note", "Could not reach the device service.");
    announce("Could not reach the device service.", true);
    return;
  }
  if (!data.ok) {
    setText("mixer-note", data.message || "Could not read the channels.");
    announce(data.message || "Could not read the channels.", true);
    return;
  }
  channelsData = data;
  channelPresets = data.presets || channelPresets;
  setText("mixer-note", "");
  populateChannelSelect();
  populateFxPreviewMics();
  renderChannelPanel(currentChannelName);
  // Land on the SELECTED TAB: natively focusable, and it announces its own name and position
  // ("Dynamics, tab, 5 of 7") rather than needing a heading given tabindex="-1", which every
  // screen reader treats slightly differently.
  if (focusProcessing && channelTabs) channelTabs.focusSelected();
}

function populateChannelSelect() {
  const sel = document.getElementById("channel-select");
  const keep = currentChannelName;
  sel.innerHTML = "";
  channelsData.channels.forEach((c) => {
    const opt = document.createElement("option");
    opt.value = c.name;
    opt.textContent = c.label;
    sel.appendChild(opt);
  });
  if (keep && channelsData.channels.some((c) => c.name === keep)) {
    sel.value = keep;
  } else {
    currentChannelName = channelsData.channels.length ? channelsData.channels[0].name : null;
    sel.value = currentChannelName;
  }
  if (!sel.dataset.bound) {
    sel.addEventListener("change", () => {
      currentChannelName = sel.value;
      renderChannelPanel(currentChannelName);
    });
    sel.dataset.bound = "1";
  }
}

// Rebuild the FX live-preview mic dropdown from the connected device's real mic
// channels (Duo = Mic 1/Mic 2; Pro II = Mic 1-4) instead of a hardcoded 1-4 list.
function populateFxPreviewMics() {
  const sel = document.getElementById("fx-preview-mic");
  if (!sel || !channelsData) return;
  const mics = channelsData.channels.filter(
    (c) => c.kind === "mic" && MIC_NAME_TO_INDEX[c.name]);
  if (!mics.length) return;
  const keep = sel.value;
  sel.innerHTML = "";
  mics.forEach((c) => {
    const opt = document.createElement("option");
    opt.value = String(MIC_NAME_TO_INDEX[c.name]);
    opt.textContent = c.label;
    sel.appendChild(opt);
  });
  if (mics.some((c) => String(MIC_NAME_TO_INDEX[c.name]) === keep)) sel.value = keep;
}

function channelByName(name) {
  return channelsData && channelsData.channels.find((c) => c.name === name);
}

/* Which TAB each of the server's parameter groups (PROCESSING_CONTROLS) belongs to. The groups are
   merged so the strip stays one row: seven tabs for a mic, five for anything else. A group not
   listed here is not lost - it gets a tab of its own, so a group added to the server later still
   appears without a UI change. */
const CHANNEL_PARAM_TAB = {
  "Equaliser": "eq",
  "High-pass filter": "eq",
  "Compressor": "dyn",
  "Noise gate": "dyn",
  "De-esser": "dyn",
  "Tone shaping": "tone",
  "Pan": "tone",
};

// The tab strip currently on the page, so a device read can put focus back on the selected tab.
let channelTabs = null;

/* The panel is a TAB STRIP (see buildTabs), in the order you actually work: the channel's own input
   settings first with MUTE at the top, then presets (the control reached for straight after gain),
   then the whole-chain switches, then the parameter detail, then the effects. Only the selected
   tab exists in the DOM, which is the whole point: a mic is over 50 controls and this page used to
   be one long walk whatever was collapsed.
   The channel dropdown deliberately stays ABOVE the strip (static markup) because it decides what
   every tab acts on; inside tab one you would have to arrow back to it, and could end up editing a
   channel you cannot see from where you are.
   Only the layout changed here; every control still writes exactly the property it did before. */
// WHY A CONTROL IS DEAD, in the words that let somebody act on it. A channel with no fader has
// no strip, so it has no mute, no solo and no processing - and saying "the unit did not report
// this" there sends a screen-reader user hunting for a fault instead of to the Routing page.
// Expanded's four virtual inputs arrive in exactly that state, and so does a Duo's Mic 2 while it
// is combo linked.
function noStripNote(ch, what) {
  if (ch.on_fader === false) {
    return ` ${ch.label} is not on a fader, so it has no ${what}. Put it on a fader on the`
      + " Routing page.";
  }
  return "";
}

function renderChannelPanel(name) {
  const host = document.getElementById("channel-panel");
  host.innerHTML = "";
  channelTabs = null;
  const ch = channelByName(name);
  if (!ch) return;
  const isMic = ch.kind === "mic";

  // Bucket the server's parameter groups into the merged tabs, keeping the server's own order.
  const buckets = {};
  const extras = [];
  (channelsData.groups || []).forEach((group) => {
    const key = CHANNEL_PARAM_TAB[group.group];
    if (!key) { extras.push(group); return; }
    (buckets[key] = buckets[key] || []).push(group);
  });

  // A BUS HAS NO PROCESSING AT ALL, so it gets ONE tab and no strip worth arrowing along.
  // The SMART pads strip publishes a full chain and accepts every write to it, so a read-back
  // suggests it is processable. It is not: tested BY EAR on both desks, none of it reaches the
  // audio, and RODE's own guide agrees ("You can add processing to any audio channel except the
  // SMART pads"). Do not offer an equaliser here on the strength of the read-back. A control that
  // is read out, focused, operated and does nothing is worse than one that is not there.
  const isBus = ch.kind === "bus";
  const tabs = [];
  tabs.push({ key: "settings", title: "Channel settings",
    fill: (body) => fillChannelSettings(body, ch) });
  // Mic only, and second because applying a whole voicing is the usual next step after gain.
  if (isMic) tabs.push({ key: "presets", title: "Presets",
    fill: (body) => fillPresetControls(body, ch) });
  if (!isBus) tabs.push({ key: "processing", title: "Processing",
    fill: (body) => fillChannelProcessing(body, ch) });
  const paramTab = (key, title) => {
    if (isBus || !buckets[key]) return;
    tabs.push({ key, title, fill: (body) => fillParamGroups(body, ch, buckets[key], title) });
  };
  paramTab("eq", "Equaliser");
  paramTab("dyn", "Dynamics");
  paramTab("tone", "Tone and pan");
  if (!isBus) extras.forEach((group) => {
    tabs.push({ key: "param." + group.group, title: group.group,
      fill: (body) => fillParamGroups(body, ch, [group], group.group) });
  });
  if (isMic) tabs.push({ key: "fx", title: "Effects",
    fill: (body) => fillChannelEffects(body, ch) });

  // The channel's LABEL goes in every panel heading ("Dynamics for Mic 1"), so being several tabs
  // away from the dropdown never leaves you guessing which channel you are editing.
  channelTabs = buildTabs("channels", tabs, { context: ch.label,
  });
  host.appendChild(channelTabs.strip);
  host.appendChild(channelTabs.panel);
  // A check can outlive the pane its button was on (change channel mid-run and the panes are
  // rebuilt for the new one), so the label is set from the SERVER's idea of what is running rather
  // than from a variable this page happens to hold. Fire and forget: it costs no device read.
  syncLevelCheckButton();
}

/* Put "Stop checking" on the button if a run is going, whichever channel it belongs to. Pressing it
   then stops that run, and its summary names the channel it was measuring. */
async function syncLevelCheckButton() {
  try {
    const resp = await fetch("/api/channels/level-check/status");
    const st = await resp.json();
    document.querySelectorAll("[id^='levelcheck-']").forEach((btn) => {
      btn.textContent = st.running ? "Stop checking" : "Check this channel's level";
    });
  } catch (e) { /* the label stays as built, which is the safe way round */ }
}

function fillChannelSettings(body, ch) {
  const isMic = ch.kind === "mic";
  // MUTE FIRST. It is the control reached for most often, and the point of having it here is
  // REACH: it is the same mute as the button on the unit, so it can be pressed from a phone or
  // another room instead of by finding the right button by touch.
  body.appendChild(buildChannelMute(ch));
  // SOLO right after it: the two are a pair on the desk, and the one thing that must not happen is
  // reaching for Mute and finding Solo, so they are worded as opposites rather than as siblings.
  body.appendChild(buildChannelSolo(ch));
  // THE WIRELESS MIC'S OWN MUTE, next to the other two because that is where it is reached for.
  if (isMic) body.appendChild(buildWirelessMute(ch));
  // A BUS (the SMART pads mix) HAS NO LEVEL HERE: its level is its fader, not an input gain, so the
  // slider is left out rather than shown reading "not read" forever. Everything else on the panel -
  // mute, solo and the whole processing chain - is the same as any other channel.
  if (ch.kind !== "bus") {
    const min = isMic ? channelsData.gain_min_db : channelsData.level_min_db;
    const max = isMic ? channelsData.gain_max_db : channelsData.level_max_db;
    body.appendChild(buildLevelField(ch, min, max));
  }
  // AUTO GAIN, immediately after the gain it sets. Setting mic gain is the one studio job that is
  // purely visual - talk, watch a meter, turn the knob - so this is the accessible equivalent
  // rather than a convenience. Mic channels only: nothing else has a preamp gain.
  if (isMic) body.appendChild(buildAutoGainButton(ch));
  // AND THE CHECK, right after it: auto gain sets the gain, this one tells you what a channel is
  // doing now. Every channel has it, not just the mics - a USB feed or a phone on Bluetooth arrives
  // at a level nobody chose either. Chat is the exception, and it is the mechanism rather than a
  // rule: the check listens on the Chat output, which leaves the caller out of their own mix.
  if (ch.name !== "Chat") body.appendChild(buildLevelCheckButton(ch));
  if (!isMic) return;
  const idx = MIC_NAME_TO_INDEX[ch.name];
  body.appendChild(buildSwitch("p48-" + ch.name, "48V phantom power", ch.phantom === true,
    (on, input) => sendMicToggle("/api/channel/phantom", idx, on, input)));
  body.appendChild(buildSwitch("pol-" + ch.name, "Invert polarity (phase)", ch.polarity === true,
    (on, input) => sendMicToggle("/api/channel/polarity", idx, on, input)));
  if (channelsData.mic_types && channelsData.mic_types.length) {
    body.appendChild(buildMicTypeField(ch, idx));
  }
}

function fillChannelProcessing(body, ch) {
  body.appendChild(buildSwitch("proc-on-" + ch.name, "Processing on",
    ch.processing_on === true,
    (on, input) => sendToggle("/api/channels/processing-onoff", { name: ch.name, on }, input, on)));
  // ADVANCED vs SIMPLE (channelAdvancedProcessing). In simple mode the unit works the chain out
  // from Depth/Sparkle/Punch, so the sliders on the other tabs may be recomputed from those three -
  // say so rather than leaving a screen-reader user wondering why a value moved on its own.
  if (ch.advanced !== null && ch.advanced !== undefined) {
    const hintId = "adv-hint-" + ch.name;
    const sw = buildSwitch("adv-" + ch.name, "Advanced processing (use the modules below)",
      ch.advanced === true,
      (on, input) => sendToggle("/api/channels/advanced", { name: ch.name, on }, input, on));
    const inp = sw.querySelector("input");
    if (inp) inp.setAttribute("aria-describedby", hintId);
    body.appendChild(sw);
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.id = hintId;
    hint.textContent = "With this off, the unit sets the modules and their parameters itself from "
      + "Tone shaping (Depth, Sparkle and Punch), so values on the other tabs can change on their "
      + "own. Turn it on to control each module yourself. Switching modes here does not wipe your "
      + "settings.";
    body.appendChild(hint);
  }
  (channelsData.modules || []).forEach((m) => {
    const onNow = ch.modules ? ch.modules[m.key + "On"] === true : false;
    body.appendChild(buildSwitch("mod-" + m.key + "-" + ch.name, m.label, onNow,
      (on, input) => sendToggle("/api/channels/module", { name: ch.name, module: m.key, on }, input, on)));
  });
}

/* One or more parameter groups into a tab. A merged tab gives each group its own h3 and
   role="group", so H walks you straight to Noise gate instead of arrowing through 17 sliders, and
   a Threshold slider still announces which module it belongs to. A tab holding a single group of
   the same name skips the sub-heading, since the panel's own h2 already says it. */
function fillParamGroups(body, ch, groups, tabTitle) {
  const single = groups.length === 1 && groups[0].group === tabTitle;
  groups.forEach((group) => {
    const wrap = single ? body : tabSection(body, group.group);
    (group.controls || []).forEach((ctrl) => {
      wrap.appendChild(buildParamSlider(ch, ctrl, ch.params ? ch.params[ctrl.prop] : null));
    });
  });
}

function fillChannelEffects(body, ch) {
  ["echo", "reverb"].forEach((effect) => {
    const title = effect.charAt(0).toUpperCase() + effect.slice(1);
    fillEffectControls(tabSection(body, title), ch, effect, title);
  });
}

/* MUTE for one channel (channelOutputMute, writable).
   Two things the wording has to get right, because a mute you misunderstand is worse than no mute.
   It is the unit's OWN mute, so pressing the button on the desk changes this checkbox's real state
   (and, with the announcer on, is spoken); and it silences the channel in EVERY mix, not just what
   you are monitoring. The state shown is read live from the unit, so a channel muted on the desk
   already reads as muted when the panel opens. */
function buildChannelMute(ch) {
  const wrap = document.createElement("div");
  wrap.className = "field-block";
  const id = "mute-" + ch.name;
  const hintId = id + "-hint";
  const sw = buildSwitch(id, "Mute " + ch.label, ch.muted === true,
    (on, input) => {
      ch.muted = on;
      return sendToggle("/api/channels/mute", { name: ch.name, on }, input, on);
    });
  const input = sw.querySelector("input");
  if (input) input.setAttribute("aria-describedby", hintId);
  wrap.appendChild(sw);
  const hint = document.createElement("p");
  hint.className = "hint";
  hint.id = hintId;
  // ONE FACT. This is attached with aria-describedby, so it is read on EVERY focus, and length
  // costs more here than anywhere else on the page. That the unit's own button does the same thing
  // is on the Help page.
  hint.textContent = "Silences this channel everywhere, including the recording."
    + (ch.on_fader === false ? noStripNote(ch, "mute")
      : (ch.muted === null || ch.muted === undefined
        ? " The unit did not report this channel's mute state, so this shows unmuted." : ""));
  wrap.appendChild(hint);
  return wrap;
}

/* SOLO / LISTEN for one channel (channelCueEnable, writable).

   The wording matters more here than on the mute, and in the opposite direction. A mute is
   dangerous because it silences a channel for everybody; a solo is dangerous because it silences
   everything EXCEPT one channel, in your headphones only, and a blind operator cannot see the lit
   Solo button that would explain it. So the label says Listen (RODE's own word for it on the desk),
   the hint says outright that the mixes and the recording are untouched, and the reply from the
   write names everything currently soloed rather than just acknowledging this one.

   The "Clear every solo" button sits beside it for the same reason: the way out has to be reachable
   without first working out which channel did it. It is a plain button, not a switch, because it is
   an action with no state of its own. */
function buildChannelSolo(ch) {
  const wrap = document.createElement("div");
  wrap.className = "field-block";
  const id = "solo-" + ch.name;
  const hintId = id + "-hint";
  const sw = buildSwitch(id, "Listen (solo) " + ch.label, ch.solo === true,
    (on, input) => {
      ch.solo = on;
      return sendToggle("/api/channels/solo", { name: ch.name, on }, input, on);
    });
  const input = sw.querySelector("input");
  if (input) input.setAttribute("aria-describedby", hintId);
  wrap.appendChild(sw);
  const hint = document.createElement("p");
  hint.className = "hint";
  hint.id = hintId;
  // The caution stays where the rest went, and it earns its place: this is the one control on the
  // page that can leave a screen-reader user with no audio at all and nothing to explain it,
  // because announcements arriving on USB 1 are not what you are soloing.
  hint.textContent = "Sends only this channel to your headphones. Care: while anything is soloed "
    + "you will not hear announcements."
    + (ch.on_fader === false ? noStripNote(ch, "solo")
      : (ch.solo === null || ch.solo === undefined
        ? " The unit did not report this channel's solo state, so this shows off." : ""));
  wrap.appendChild(hint);

  const clear = document.createElement("button");
  clear.type = "button";
  clear.id = "solo-clear";
  clear.textContent = "Clear every solo";
  clear.addEventListener("click", async () => {
    setBusy(clear, true);
    try {
      const res = await postJSON("/api/channels/solo/clear", {});
      announce(res.message || "Done.", !res.ok);
      if (res.ok) {
        ch.solo = false;
        if (input) input.checked = false;
      }
    } catch (err) {
      announce("Could not reach the device service.", true);
    } finally {
      setBusy(clear, false);
    }
  });
  wrap.appendChild(clear);
  return wrap;
}

/* MUTE THE WIRELESS MIC ITSELF (channelWirelessMute). Mic channels only: it is the desk telling a
   paired RODE transmitter to stop sending, so there is nothing for it to reach on a line channel or
   on the pads bus.

   IT SITS BESIDE THE CHANNEL MUTE BECAUSE THAT IS WHERE SOMEBODY REACHES FOR IT, which makes the
   wording the whole job: two controls a keystroke apart, both called mute, doing different things.
   The label says which mic, the hint says the difference in one sentence, and the reply from the
   write says the same thing again. The difference is worth the words: muting the channel still leaves
   an open microphone in the room feeding the desk, and muting the transmitter is what you actually
   want when somebody walks off still wearing it.

   NOBODY HERE HAS A WIRELESS MIC TO TEST IT WITH, so the app never claims the mic went quiet - it
   says the mute was SENT. The switch still shows the desk's own state, which is real either way.

   UNAVAILABLE WHEN THE UNIT REPORTS NO TRANSMITTER, AND NEVER HIDDEN. Hidden fails silently: the
   signal it would hide on, `txConnected`, has never seen a real transmitter (server._wireless_mics
   stamps `unproven`), so if it means something other than we assume, a user who DOES have wireless
   mics loses the control with no way to discover it existed. Unavailable keeps it in the tab order
   and says why, the same call the matrix source switches make.

   GATED ON THE WHOLE DESK, NOT THIS CHANNEL, and that is a limit rather than a preference:
   `txConnected` is a flat list of transmitter slots and which slot belongs to which mic has never
   been established, so a per-channel gate would need a mapping we do not have. An EMPTY slot list
   leaves the switch available on purpose: "could not read it" is not "there is not one". */
function buildWirelessMute(ch) {
  const wrap = document.createElement("div");
  wrap.className = "field-block";
  const id = "wireless-mute-" + ch.name;
  const hintId = id + "-hint";
  const wireless = (channelsData && channelsData.wireless_mics) || null;
  const noneConnected = !!(wireless && wireless.present && !wireless.any_connected);
  const sw = buildSwitch(id, "Mute the wireless mic on " + ch.label, ch.wireless_muted === true,
    (on, input) => {
      // setAvailable only marks it; the HANDLER has to refuse, or the switch writes anyway and then
      // reports a state the unit is not in. Same shape as onMatrixSourceToggle.
      const no = unavailableReason(input);
      if (no !== null) { input.checked = !input.checked; announce(no, true); return; }
      ch.wireless_muted = on;
      return sendToggle("/api/channels/wireless-mute", { name: ch.name, on }, input, on);
    });
  const input = sw.querySelector("input");
  if (input) {
    input.setAttribute("aria-describedby", hintId);
    setAvailable(input, !noneConnected, "No wireless mics detected.");
  }
  wrap.appendChild(sw);
  const hint = document.createElement("p");
  hint.className = "hint";
  hint.id = hintId;
  hint.textContent = noneConnected
    ? "No wireless mics detected."
    : "Mutes a paired RODE wireless transmitter at the mic, not the channel here."
      + (ch.wireless_muted === null || ch.wireless_muted === undefined
        ? " The unit did not report this, so the switch shows unmuted." : "");
  wrap.appendChild(hint);
  return wrap;
}

/* A labelled range slider in dB. Debounced so arrow-keying does not flood USB. */
function buildLevelField(ch, min, max) {
  const row = document.createElement("div");
  row.className = "field proc-field";
  const id = "level-" + ch.name;
  const label = document.createElement("label");
  label.setAttribute("for", id);
  label.textContent = "Level";
  row.appendChild(label);

  const input = document.createElement("input");
  input.type = "range";
  input.id = id;
  input.min = String(min);
  input.max = String(max);
  input.step = "1";
  const known = ch.level_db !== null && ch.level_db !== undefined;
  input.value = known ? String(ch.level_db) : String(min);
  input.setAttribute("aria-valuetext", known ? ch.level_db + " decibels" : "not read yet");
  row.appendChild(input);

  const out = document.createElement("output");
  out.setAttribute("for", id);
  out.className = "proc-value";
  out.textContent = known ? ch.level_db + " dB" : "not read";

  input.addEventListener("input", () => {
    out.textContent = input.value + " dB";
    input.setAttribute("aria-valuetext", input.value + " decibels");
  });
  input.addEventListener("change", () => {
    const db = parseInt(input.value, 10);
    if (Number.isNaN(db)) return;
    ch.level_db = db;
    const key = "level-" + ch.name;
    window.clearTimeout(channelDebounce[key]);
    channelDebounce[key] = window.setTimeout(async () => {
      try {
        const res = await postJSON("/api/channels/level", { name: ch.name, db });
        announce(res.message || "Done.", !res.ok);
      } catch (err) { announce("Could not reach the device service.", true); }
    }, WRITE_DEBOUNCE_MS);
  });
  row.appendChild(out);
  return row;
}

/* A checkbox switch. onChange(checked, inputEl) does the work and may revert on failure. */
function buildSwitch(id, labelText, checked, onChange) {
  const p = document.createElement("p");
  p.className = "field";
  const label = document.createElement("label");
  label.className = "switch";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.id = id;
  input.checked = !!checked;
  input.addEventListener("change", () => onChange(input.checked, input));
  const span = document.createElement("span");
  span.textContent = labelText;
  label.appendChild(input);
  label.appendChild(span);
  p.appendChild(label);
  return p;
}

function sendMicToggle(url, channel, on, input) {
  return sendToggle(url, { channel, on }, input, on);
}

/* MIC TYPE IS STAGED BEHIND ITS OWN APPLY, and it is the most important of the staged selects:
   choosing a voicing makes the DESK apply that voicing's own default gain, so an arrow press
   through this list would change the level as well as the sound. A closed select fires on every
   arrow, and arrowing a closed select is how you read one. */
function buildMicTypeField(ch, idx) {
  const opts = channelsData.mic_types.map(([val, name]) => [val, name]);
  const known = opts.some(([val]) => val === ch.mic_type);
  if (!known && ch.mic_type !== null && ch.mic_type !== undefined) {
    opts.push([ch.mic_type, "Other model (value " + ch.mic_type + ")"]);
  }
  const made = makeStagedSelect("mictype-" + ch.name, "Mic type", opts, {
    className: "field proc-field",
    applyText: "Apply mic type",
    onApply: (sel, btn) => setMicType(idx, sel, ch, btn),
  });
  setStagedValue(made.sel, ch.mic_type, null);
  return made.row;
}

/* Echo + Reverb (mic channels only), matching the app's Effects tab. */
// Channel Echo/Reverb param sliders. Names and selector decoded from an app-drive capture: global
// float64 on selector 0x26+(ch-1). Friendly labels for the screen reader.
const FX_PARAM_LABELS = {
  echoMix: "Level", echoLowCut: "Low cut", echoHighCut: "High cut",
  echoDelay: "Delay", echoDecay: "Decay",
  reverbMix: "Level", reverbModel: "Size", reverbLowCut: "Low cut", reverbHighCut: "High cut",
};
const FX_PARAMS_BY_EFFECT = {
  echo: ["echoMix", "echoLowCut", "echoHighCut", "echoDelay", "echoDecay"],
  reverb: ["reverbMix", "reverbModel", "reverbLowCut", "reverbHighCut"],
};
const _fxParamDebounce = {};

function buildFxParamSlider(ch, effect, param, value01) {
  const row = document.createElement("div");
  row.className = "field fx-ctl";
  const id = "fxp-" + param + "-" + ch.name;
  const label = document.createElement("label");
  label.setAttribute("for", id);
  // Self-describing name, e.g. "Echo level", "Reverb size" (survives NVDA caching the legend).
  label.textContent = effect.charAt(0).toUpperCase() + effect.slice(1) + " "
    + (FX_PARAM_LABELS[param] || param).toLowerCase();
  const input = document.createElement("input");
  input.type = "range"; input.id = id; input.min = "0"; input.max = "100"; input.step = "1";
  const known = (value01 !== null && value01 !== undefined);
  input.value = known ? String(Math.round(value01 * 100)) : "50";
  const out = document.createElement("span");
  out.className = "fx-ctl-value"; out.setAttribute("aria-hidden", "true");
  const fmt = () => known || input.dataset.touched ? input.value + " percent" : "not read yet";
  const show = () => { input.setAttribute("aria-valuetext", fmt()); out.textContent = input.value + "%"; };
  show();
  input.addEventListener("input", () => {
    input.dataset.touched = "1"; show();
    clearTimeout(_fxParamDebounce[id]);
    _fxParamDebounce[id] = setTimeout(async () => {
      try {
        const res = await postJSON("/api/channels/effect-param",
          { name: ch.name, param, value: parseInt(input.value, 10) / 100 });
        if (res && !res.ok && res.message) announce(res.message, true);
      } catch (e) { /* transient */ }
    }, WRITE_DEBOUNCE_MS);
  });
  row.appendChild(label); row.appendChild(input); row.appendChild(out);
  return row;
}

/* One effect's on/off switch plus its parameter sliders, into a collapsible group's body. */
function fillEffectControls(body, ch, effect, title) {
  const fx = ch.effects || {};
  body.appendChild(buildSwitch("fx-" + effect + "-" + ch.name, title, fx[effect] === true,
    (on, input) => sendToggle("/api/channels/effect", { name: ch.name, effect, on }, input, on)));
  // Param sliders for this effect (Level, cuts, etc.), prefilled from the live read.
  const params = fx[effect + "_params"] || {};
  FX_PARAMS_BY_EFFECT[effect].forEach((param) => {
    body.appendChild(buildFxParamSlider(ch, effect, param, params[param]));
  });
}

function buildParamSlider(ch, ctrl, percent) {
  const row = document.createElement("div");
  row.className = "field proc-field";
  const id = "param-" + ctrl.prop + "-" + ch.name;
  const label = document.createElement("label");
  label.setAttribute("for", id);
  label.textContent = ctrl.label;
  row.appendChild(label);

  const input = document.createElement("input");
  input.type = "range";
  input.id = id;
  input.min = "0"; input.max = "100"; input.step = "1";
  const known = percent !== null && percent !== undefined;
  input.value = known ? String(percent) : "50";
  input.setAttribute("aria-valuetext", known ? percent + " percent" : "not read yet");

  const out = document.createElement("output");
  out.setAttribute("for", id);
  out.className = "proc-value";
  out.textContent = known ? percent + "%" : "not read";

  input.addEventListener("input", () => {
    out.textContent = input.value + "%";
    input.setAttribute("aria-valuetext", input.value + " percent");
  });
  input.addEventListener("change", () => {
    const pct = parseInt(input.value, 10);
    if (ch.params) ch.params[ctrl.prop] = pct;
    const key = "param-" + ctrl.prop + "-" + ch.name;
    window.clearTimeout(channelDebounce[key]);
    channelDebounce[key] = window.setTimeout(async () => {
      try {
        const res = await postJSON("/api/channels/param",
          { name: ch.name, prop: ctrl.prop, value: pct / 100 });
        announce(res.message || "Done.", !res.ok);
      } catch (err) { announce("Could not reach the device service.", true); }
    }, WRITE_DEBOUNCE_MS);
  });
  row.appendChild(input);
  row.appendChild(out);
  return row;
}

/* (Re)fill a preset dropdown's options from channelPresets, keeping the current choice if it is
   still there. Saving or deleting a preset only changes this LIST, so the options are refreshed in
   place: rebuilding the panel around it would destroy the button the user just pressed. */
function fillPresetOptions(sel) {
  const keep = sel.value;
  sel.innerHTML = "";
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = channelPresets.length ? "Choose a preset to apply..." : "No presets saved";
  sel.appendChild(blank);
  channelPresets.forEach((p) => {
    const opt = document.createElement("option");
    opt.value = p.name;
    opt.textContent = p.custom ? p.name + " (custom)" : p.name;
    sel.appendChild(opt);
  });
  if (keep && channelPresets.some((p) => p.name === keep)) sel.value = keep;
}

/* Mic processing presets: apply a whole voicing, or save/delete a custom one. */
/* The factory marker names are NOT duplicated here. They live in one place,
   server._FACTORY_MARKER_NAMES, which resolves channelCurrentFxPreset and sends back a ready-made
   {name, edited, source}. */

function fillPresetControls(wrap, ch) {
  const idx = MIC_NAME_TO_INDEX[ch.name];

  // WHAT THIS CHANNEL IS ON. The server works it out (server._preset_now): the unit's own marker
  // when it has an answer, otherwise our record of what we applied, checked against the live values
  // so an edited preset is reported as edited rather than as the preset.
  //
  // IT RIDES ON THE SELECT'S OWN LABEL, not only on the paragraph below. Tabbing into this panel
  // lands on the dropdown, so a label reading "Choose a preset to apply..." makes the app look like
  // it never remembered the preset. On the label it is announced whenever you reach the control,
  // and the paragraph is left to explain the caveat.
  const now = ch.preset_now || null;
  const currently = !now ? "not on a preset we can name"
    : now.edited === true ? `${now.name}, edited`
    : now.edited === null ? `${now.name}, which no longer exists to check against`
    : now.name;

  const applyRow = document.createElement("div");
  applyRow.className = "field";
  const applyLabel = document.createElement("label");
  const applyId = "preset-apply-" + ch.name;
  const applyBtnId = "preset-apply-btn-" + ch.name;
  const hintId = "preset-apply-hint-" + ch.name;
  applyLabel.setAttribute("for", applyId);
  applyLabel.textContent = `Preset, currently ${currently}`;
  const applySel = document.createElement("select");
  applySel.id = applyId;
  applySel.setAttribute("aria-describedby", hintId);
  fillPresetOptions(applySel);
  // The list DELIBERATELY does not write on change. Applying from the `change` event breaks the
  // dropdown for anyone who does not open the popup first: a collapsed select fires `change` on
  // EVERY arrow key, so each keypress applies a preset, re-reads the device and rebuilds the panel,
  // destroying the select mid-interaction. It appears to work when you tab in (the screen reader
  // opens the popup, so change fires once, on Enter) and fails when you jump straight to it with
  // quick navigation. Choosing is free; nothing is sent until this button is pressed.
  const applyBtn = document.createElement("button");
  applyBtn.type = "button";
  applyBtn.id = applyBtnId;
  applyBtn.textContent = "Apply this preset";
  applyBtn.addEventListener("click", async () => {
    if (isBusy(applyBtn)) return;
    const name = applySel.value;
    if (!name) {
      announce("Choose a preset in the list first, then press Apply this preset.", true);
      applySel.focus();
      return;
    }
    setBusy(applyBtn, true);
    try {
      const res = await postJSON("/api/processing/preset", { channel: idx, preset: name });
      announce(res.message || "Done.", !res.ok);
      // Re-read so the sliders show the applied voicing. That rebuilds the panel, so keep focus
      // on this button instead of dropping the user at the top of the page.
      if (res.ok) await keepingFocus(loadChannels);
    } catch (err) { announce("Could not reach the device service.", true); }
    finally {
      setBusy(document.getElementById(applyBtnId), false);   // the rebuild replaced the element
    }
  });
  const applyHint = document.createElement("span");
  applyHint.className = "hint";
  applyHint.id = hintId;
  // Say what a preset covers. A preset that sets VALUES only is applied to a channel whose modules
  // are off, reports success and sounds identical, so it switches the modules the preset uses and
  // names them in the result. It still leaves the channel's own processing on/off alone, which is
  // the setting most likely to be off on purpose.
  // The second half is the honesty about "currently" in the label. Where the claim comes from
  // matters: the unit's marker is exact but goes blank at the first tweak, while our record is
  // checked against the live values, so "edited" means the values really have moved.
  applyHint.textContent =
    "A preset sets this channel's processing values and switches on the parts of the chain it "
    + "uses, and it tells you which ones it switched. It does not turn this channel's processing "
    + "on or off; that stays as you left it. Move through the list freely; nothing changes on the "
    + "unit until you press Apply this preset. "
    + (!now
        ? "This channel is not on a preset we can name: its settings were adjusted on the unit, or "
          + "applied before this app was installed."
        : now.source === "device"
          ? `The unit itself reports this channel as on the ${now.name} preset, which means nothing `
            + "has been changed since it was applied."
          : now.edited === true
            ? `This app applied the ${now.name} preset here, and the channel's values no longer `
              + "match it, so something has been adjusted since."
            : now.edited === null
              ? `This app applied the ${now.name} preset here, but that preset has since been `
                + "deleted, so there is nothing left to check the values against."
              : `This app applied the ${now.name} preset here, and the channel's values still `
                + "match it.");
  applyRow.appendChild(applyLabel);
  applyRow.appendChild(applySel);
  applyRow.appendChild(applyBtn);
  applyRow.appendChild(applyHint);
  wrap.appendChild(applyRow);

  const saveRow = document.createElement("div");
  saveRow.className = "field";
  const saveLabel = document.createElement("label");
  const saveId = "preset-name-" + ch.name;
  saveLabel.setAttribute("for", saveId);
  saveLabel.textContent = "Save current settings as a preset";
  const nameInput = document.createElement("input");
  nameInput.type = "text"; nameInput.id = saveId;
  nameInput.placeholder = "New preset name"; nameInput.autocomplete = "off";
  const saveBtn = document.createElement("button");
  saveBtn.type = "button"; saveBtn.textContent = "Save preset";
  saveBtn.addEventListener("click", async () => {
    const name = nameInput.value.trim();
    if (!name) { announce("Please enter a name for the preset.", true); nameInput.focus(); return; }
    try {
      const res = await postJSON("/api/processing/preset/save", { channel: idx, name });
      announce(res.message || "Done.", !res.ok);
      // Only the preset LIST changed, so refresh the dropdown in place. Rebuilding the panel here
      // destroys this button and throws focus to the top of the page.
      if (res.ok) { channelPresets = res.presets || channelPresets; nameInput.value = ""; fillPresetOptions(applySel); }
    } catch (err) { announce("Could not reach the device service.", true); }
  });
  const delBtn = document.createElement("button");
  delBtn.type = "button"; delBtn.textContent = "Delete selected preset";
  delBtn.addEventListener("click", async () => {
    const name = applySel.value;
    if (!name) { announce("Choose a custom preset in the list first, then delete it.", true); return; }
    try {
      const res = await postJSON("/api/processing/preset/delete", { name });
      announce(res.message || "Done.", !res.ok);
      if (res.ok) { channelPresets = res.presets || channelPresets; fillPresetOptions(applySel); }
    } catch (err) { announce("Could not reach the device service.", true); }
  });
  saveRow.appendChild(saveLabel);
  saveRow.appendChild(nameInput);
  saveRow.appendChild(saveBtn);
  saveRow.appendChild(delBtn);
  wrap.appendChild(saveRow);
}

/* THE UNIT MOVES THE GAIN **AND THE WHOLE PROCESSING CHAIN** WHEN THE VOICING CHANGES, so most of
   this panel is stale the moment the write returns: the Level slider (measured: SM7B 55,
   Condenser 25, and so on, up to a 30 dB jump) and, on a Pro II, 13 processing values as well.
   Patching the one slider by hand is not enough, so a success re-reads the whole panel the way
   applying a preset does. The moves are also in the spoken message, because controls that silently
   update themselves announce nothing. */
async function setMicType(channel, sel, ch, btn) {
  const type = parseInt(sel.value, 10);
  if (Number.isNaN(type)) return;
  setBusy(btn || sel, true);
  try {
    const res = await postJSON("/api/channel/mic-type", { channel, type });
    announce(res.message || "Done.", !res.ok);
    if (res.ok) {
      // Rebuilds the panel, which re-reads the type onto the dropdown as well, so `sel` and `btn`
      // are detached from here on and must not be touched again.
      await keepingFocus(loadChannels);
      return;
    }
    // Refused, so the unit still holds the old type: put the dropdown back in step with the desk.
    setStagedValue(sel, ch && ch.mic_type, null);
  } catch (err) {
    announce("Could not reach the device service.", true);
    setStagedValue(sel, ch && ch.mic_type, null);
  } finally {
    setBusy(btn || sel, false);
  }
}

/* ---- SMART pads: replace sound -------------------------------------- */

let padsData = null;
// True when the active device's pad WRITES are not enabled (e.g. the Duo, pending a reversible
// create/clear test). Reads still work; every write control is disabled so nothing is a silent
// no-op. Set from the /api/soundpads read_only flag on each load.
let padsReadOnly = false;
// The padPlayMode value that means One Shot, from the device payload. Loop cannot apply in that
// mode, so the Loop row is hidden for it rather than offering a setting the pad ignores.
let padsPlayModeOneShot = 2;

// Every interactive control in the pads section that performs a DEVICE write. The pad browse
// dropdown (pad-ed-select) and the custom-effect creator (fx-create-*, which only saves to a
// local file, not the device) are intentionally left out so viewing and building presets still
// work on a read-only device. Preview (fx-preview-*) DOES write a scratch pad, so it is included.
const PAD_WRITE_CONTROL_IDS = [
  "pad-ed-type", "pad-ed-file", "pad-ed-fx", "pad-ed-mixer-mode", "pad-ed-apply-btn",
  "pad-ed-name", "pad-ed-name-btn", "pad-ed-volume", "pad-ed-colour", "pad-ed-replay",
  "pad-ed-loop", "pad-ed-playmode", "pad-ed-mixer-trigger", "pad-ed-clear-btn", "fx-preview-toggle", "fx-preview-mic",
  // "export-pad-btn" is deliberately NOT here: an export only READS the unit, so it stays usable on
  // a read-only device - which is exactly when getting a sound off the unit matters most.
  "clear-all-btn", "restore-fx-btn", "tidy-audio-btn",
  "pad-ed-trigger-custom", "pad-ed-fade-host", "pad-ed-censor-custom",
];

// The one reason every pad-write control gives on a desk whose pad writes are not enabled.
const PADS_READ_ONLY_OFF = "Editing SMART pads is not enabled on this device yet, so this cannot "
  + "be changed. You can still see what every pad holds.";

let _padsGuardInstalled = false;

/* Refuse, OUT LOUD, any press or change on a pad control that is marked unavailable.
   Capture phase, so it runs before the control's own handler. `preventDefault` on the click is what
   stops a switch or a checkbox moving at all; a select, a text box or a slider can be changed from
   the keyboard with no click at all, so a change that does get through is undone by repainting the
   editor from the pad the unit actually has.

   ONE DELEGATED LISTENER, NOT A GUARD PER HANDLER, and that is deliberate. These controls have
   fifteen or so separate handlers plus two families built from the device payload with no fixed IDs
   at all, so a per-handler guard is a list a new pad control can quietly be left off. A listener on
   the section cannot be forgotten by the next person to add a control. */
function installPadsGuard() {
  if (_padsGuardInstalled) return;
  const host = document.getElementById("sec-pads");
  if (!host) return;
  _padsGuardInstalled = true;
  // BUSY IS NOT UNAVAILABLE, and conflating the two breaks this whole page. `setBusy` marks a
  // control aria-disabled while its own write is in flight, deliberately, so that focus is not
  // thrown off it - which means every control here is briefly "unavailable" during normal use. A
  // guard that does not exclude those swallows real presses and leaves the page stuck. `isBusy` is
  // the flag that tells them apart, and it is why setBusy sets one.
  const refuseIf = (ev) => {
    const el = ev.target.closest("[aria-disabled='true']");
    if (!el || isBusy(el)) return null;
    return unavailableReason(el);
  };
  host.addEventListener("click", (ev) => {
    const no = refuseIf(ev);
    if (no === null) return;
    ev.preventDefault();
    ev.stopPropagation();
    announce(no, true);
  }, true);
  host.addEventListener("change", (ev) => {
    const no = refuseIf(ev);
    if (no === null) return;
    ev.stopPropagation();
    announce(no, true);
    syncPadEditor();          // back to the pad the unit has, from the read already in hand
  }, true);
}

// When the device is read-only, mark every pad-write control unavailable. Called after loadPads and
// after syncPadEditor (which re-enables some controls per pad), so it always wins. No-op otherwise,
// leaving the normal per-pad enable/disable logic in charge on a write-capable device.
//
// aria-disabled, NOT the `disabled` attribute. A natively disabled control leaves the tab order and
// takes its description with it, so on a read-only desk this page would lose about twenty controls
// AND the explanation of where they went: a note at the top saying pads are read-only, and then a
// Settings tab with fewer things in it than the same tab on the other desk, with nothing to arrow
// onto and ask. They stay in place and say why.
function applyPadsReadOnly() {
  installPadsGuard();      // before the early return: the guard also serves the per-pad reasons
  if (!padsReadOnly) return;
  PAD_WRITE_CONTROL_IDS.forEach((id) => {
    setAvailable(document.getElementById(id), false, PADS_READ_ONLY_OFF);
  });
  // The Back Channel member checkboxes and the MIDI trigger fields are built from the device
  // payload, so they have no fixed IDs to list above.
  document.querySelectorAll("#pad-ed-backchannel-list input, #pad-ed-trigger-fields input, "
                            + "#pad-ed-trigger-fields select, #pad-ed-env-fields input, "
                            + "#pad-ed-fade-fields input")
    .forEach((el) => setAvailable(el, false, PADS_READ_ONLY_OFF));
}

function padContentLabel(p) {
  if (!p.present) return "Empty";
  const nm = p.name ? p.name.replace(/_/g, " ") : "";
  return nm ? p.type_label + ": " + nm : p.type_label;
}

function padOptionLabel(p) {
  return "Bank " + p.bank + ", pad " + p.pad + " - " + padContentLabel(p);
}

/* Fill a <select> from [{value, label}], but ONLY when the option set has actually changed.
   STRUCTURE ONCE, VALUES EVERY TIME: the pads page reads the device when you open a tab, so these
   fills run while the pane is on screen and possibly while focus is sitting in the very control
   being filled. Replacing the options unconditionally moves a screen reader off it and speaks
   fragments of a list underneath it. The fingerprint means an unchanged list keeps its own nodes.
   Returns the element (or null), with `changed` on the dataset so a caller can pick a default only
   on a genuine rebuild. */
function fillOptions(id, items) {
  const sel = document.getElementById(id);
  if (!sel) return null;
  const want = JSON.stringify(items.map((i) => [String(i.value), i.label]));
  if (sel.dataset.filled === want) { delete sel.dataset.changed; return sel; }
  const keep = sel.value;
  sel.innerHTML = "";
  items.forEach((i) => {
    const opt = document.createElement("option");
    opt.value = String(i.value);
    opt.textContent = i.label;
    sel.appendChild(opt);
  });
  sel.dataset.filled = want;
  sel.dataset.changed = "1";
  if (keep !== "" && [...sel.options].some((o) => o.value === keep)) sel.value = keep;
  return sel;
}

/* The list counterpart of fillOptions, for a container whose CONTROLS are built from a device read.
   THE PRINCIPLE: nothing may replace a subtree a screen reader may be sitting in, whatever triggered
   the replacement - a read, a tab change, a poll or a write's confirmation. Every tab that re-reads
   the device when it is opened rebuilds its controls at the moment you arrive on it, and again every
   time you come back, so the read has to be able to land WITHOUT touching the nodes.
   `key` is a fingerprint of the STRUCTURE the device reported (ids, labels, order, read-only), never
   of the values: an unchanged structure keeps its own nodes and the caller updates the values in
   place. Returns true when it really did rebuild, so a caller can do build-time-only work then.
   Same rule as fillOptions and applyUnitSettingValues. */
function rebuildIfChanged(host, key, build) {
  if (!host) return false;
  if (host.dataset.built === key) return false;
  host.innerHTML = "";
  build(host);
  host.dataset.built = key;
  return true;
}

function fillPadSelect(id, pads) {
  fillOptions(id, pads.map((p) => ({ value: p.grid, label: padOptionLabel(p) })));
}

// Map the device padType to the editor's type-combo key.
const PAD_TYPE_TO_KEY = { 1: "sound", 2: "fx", 3: "mixer", 4: "trigger" };

function showEl(id, on) {
  const el = document.getElementById(id);
  if (el) el.hidden = !on;
}

function padWhere(p) { return "bank " + p.bank + ", pad " + p.pad; }

// The pad currently chosen in the editor's pad combo.
function currentPad() {
  if (!padsData) return null;
  const sel = document.getElementById("pad-ed-select");
  if (!sel) return null;
  const grid = parseInt(sel.value, 10);
  return (padsData.pads || []).find((p) => p.grid === grid) || null;
}

/* ---- SMART pads: the tab strip, and reading per tab ---------------------
   Three collapsible groups became FOUR tabs, and this page was REDESIGNED rather than converted
   The old single "Edit a pad" form mixed two behaviours: an
   Apply button that only applied to the type change, live controls that wrote as you touched them,
   and a mode where the live controls VANISHED behind a note while a type change was staged. The
   tabs are therefore split by BEHAVIOUR, not by subject:
     Settings   all live, no Apply
     Reassign   all deferred, one Apply, then Clear this pad
     Voice effects / Maintenance   neither, and not about the chosen pad at all
   That split is what retires the pending-type mode: Reassign holds no live control, so there is
   nothing to hide while a change is staged.
   READING: the pad chooser sits above the strip and every tab needs it, so page ENTRY does the one
   /api/soundpads read (~1.5 s of device time) and the tab you arrive on does not repeat it. Opening
   Settings or Reassign later re-reads, so a value is never older than the moment you arrived at it;
   Voice effects and Maintenance read nothing, because neither describes the device's pad state.
   Debounced, because selection follows the arrow keys and arrowing across the strip must not fire a
   read per tab. */
let padsTabs = null;
let _padsTabReadTimer = null;
let _padPreviewCleaned = false;
const PADS_TABS_READ = ["settings", "reassign"];

function wirePadsTabs() {
  padsTabs = buildStaticTabs("pads", "pads-panel", { onSelect: (key) => {
      clearTimeout(_padsTabReadTimer);
      if (PADS_TABS_READ.indexOf(key) < 0) return;
      _padsTabReadTimer = window.setTimeout(() => { keepingFocus(loadPads); }, 350);
    },
  });
}

// Page entry. The read fills the pad chooser above the strip, so it happens whichever tab you
// arrive on, and the arrived tab does not read again on top of it.
function readPads() {
  clearTimeout(_padsTabReadTimer);      // a pending tab read from the last visit is stale
  return loadPads();
}

async function loadPads() {
  let data;
  try {
    const resp = await fetch("/api/soundpads");
    data = await resp.json();
  } catch (err) {
    setText("pads-note", "Could not load the pad layout.");
    return;
  }
  padsData = data;

  // Clean up a preview scratch pad left behind by a previous session (e.g. the browser was
  // closed mid-preview, so the page could not clear it then). The server tracks which pad it
  // is, so we only ever clear that one. Skip if THIS page owns an active preview.
  // TRIED ONCE PER PAGE, deliberately: the re-read is what proves it worked, but a stop that does
  // not clear the flag (a wedged pad, or a stubbed POST under the UI probe) would otherwise loop
  // this read for ever, and each turn of that loop is a 1.5 second device read.
  if (data.preview_grid != null && !previewState.active && !_padPreviewCleaned) {
    _padPreviewCleaned = true;
    try { await postJSON("/api/fx/preview/stop", { grid: data.preview_grid }); } catch (e) { /* harmless */ }
    return loadPads();
  }

  padsReadOnly = !!data.read_only;
  const roNote = padsReadOnly
    ? "Editing SMART pads is not enabled on this device yet, so pads are read-only here. "
      + "You can view them but not change them. "
    : "";
  setText("pads-note", roNote + (data.reason || ""));
  setText("pads-health", data.health_message || "");

  fillPadSelect("pad-ed-select", data.pads || []);

  // EVERY dropdown below goes through fillOptions, which only touches the DOM when the list has
  // really changed. This read now runs when a TAB is opened, so the pane is on screen and focus may
  // be sitting in one of these controls; rebuilding them unconditionally would speak over a screen
  // reader and move it off the control.
  fillOptions("pad-ed-fx", (data.recipes || []).map((n) => ({ value: n, label: n })));

  // Custom voice-FX creator: build the effect controls once, then refresh the saved list.
  buildFxCreator();
  renderCustomFxList(data.custom_fx || []);
  refreshPreviewAvailability();
  applyPadsReadOnly();   // after preview availability, so read-only wins on the preview toggle

  // Mixer-action modes come from special_types (the "mixer:N" entries); MIDI trigger is
  // its own type in the editor, so it is filtered out here.
  fillOptions("pad-ed-mixer-mode", (data.special_types || [])
    .filter((t) => String(t.value).startsWith("mixer:"))
    .map((t) => ({ value: String(t.value).split(":")[1], label: t.label })));  // numeric mode 0-4

  // Mixer trigger modes (Latching / Momentary) and the Back Channel member list the connected
  // desk offers. Both come from the device payload, so a desk without (say) Mic 3 is never
  // offered it. Filled here; fillPadSettings just ticks and shows them per pad.
  padsPlayModeOneShot = data.play_mode_one_shot;
  fillOptions("pad-ed-playmode",
    (data.play_modes || []).map((m) => ({ value: m.value, label: m.label })));

  // Named colour palette for the pad editor (the value per pad is set by fillPadSettings).
  fillColourOptions("pad-ed-colour", 0);

  fillOptions("pad-ed-mixer-trigger",
    (data.mixer_triggers || []).map((t) => ({ value: t.value, label: t.label })));
  buildBackChannelList(data.back_channel_sources || []);

  syncPadEditor();
}

/* Reflect the chosen pad into BOTH pad tabs: the Reassign tab's type combo is set to what the pad
   is now, and the Settings tab is filled with the pad's live values.
   Both panel headings name the pad as well as the category, because the pad chooser is above the
   strip and you can be two tabs away from it. */
function syncPadEditor() {
  if (_volDebounce) { clearTimeout(_volDebounce); _volDebounce = null; }  // drop any pending write
  const pad = currentPad();
  const typeSel = document.getElementById("pad-ed-type");
  if (!typeSel) return;
  const present = !!(pad && pad.present);
  const curKey = present ? (PAD_TYPE_TO_KEY[pad.type] || "empty") : "empty";
  if ([...typeSel.options].some((o) => o.value === curKey)) typeSel.value = curKey;

  const where = pad ? padWhere(pad) : "this pad";
  setText("pad-h-settings", "Settings for " + where);
  setText("pad-h-reassign", "Change what " + where + " does");

  setText("pad-ed-type-note",
    present ? ("Currently: " + padContentLabel(pad) + ".") : "This pad is empty.");

  // Pre-select the current mixer mode so changing nothing keeps it the same.
  if (present && pad.type === 3 && pad.mixer_mode !== null && pad.mixer_mode !== undefined) {
    const mm = document.getElementById("pad-ed-mixer-mode");
    if (mm && [...mm.options].some((o) => o.value === String(pad.mixer_mode))) {
      mm.value = String(pad.mixer_mode);
    }
  }

  updateTypeGroups(false);
  fillPadSettings(pad);
  applyPadsReadOnly();   // re-assert read-only after per-pad controls are (re-)enabled
  // Showing or hiding rows can leave the visible pane with nothing focusable in it (an empty pad's
  // Settings tab on a read-only device), so the panel's own tab stop is re-decided here.
  if (padsTabs) padsTabs.refresh();
}

// Show the option group for the chosen type; the rest get the hidden attribute. Apply is
// shown for any real type and hidden for Empty (use Clear this pad to empty a pad).
function updateTypeGroups(announceIt) {
  const key = document.getElementById("pad-ed-type").value;
  ["sound", "fx", "mixer", "trigger"].forEach((k) => showEl("pad-ed-grp-" + k, k === key));
  showEl("pad-ed-apply-row", key !== "empty");
  if (announceIt) {
    const msgs = {
      empty: "Empty selected. Use the Clear this pad button to empty it.",
      sound: "Sound selected. Choose a file and press Apply.",
      fx: "Voice effect selected. Choose an effect and press Apply.",
      mixer: "Mixer action selected. Choose an action and press Apply.",
      trigger: "MIDI trigger selected. Press Apply to put it on the pad.",
    };
    if (msgs[key]) announce(msgs[key]);
  }
}

/* Fill the Settings tab: the pad's live values, and for an empty pad the note plus the way through
   to Reassign instead. Every control here is written in index.html, so this only ever sets VALUES
   and shows or hides rows; it never rebuilds a control, which is what lets the tab re-read the
   device without interrupting a screen reader. */
function fillPadSettings(pad) {
  const present = !!(pad && pad.present);
  showEl("pad-ed-settings", present);
  showEl("pad-ed-empty", !present);
  showEl("pad-ed-settings-help", present);
  // Clearing only makes sense for a pad that has content; disable it for an empty pad so the
  // screen reader announces it as unavailable rather than letting you press a no-op.
  const clearBtn = document.getElementById("pad-ed-clear-btn");
  // aria-disabled, never the attribute: a disabled button leaves the tab order, so Clear vanished
  // and reappeared as you moved between pads. onPadClear already says "This pad is already empty."
  setAvailable(clearBtn, present, "This pad is already empty, so there is nothing to clear.");
  if (!present) return;
  const isSound = pad.type === 1;

  const nameEl = document.getElementById("pad-ed-name");
  if (nameEl) nameEl.value = pad.name ? pad.name.replace(/_/g, " ") : "";

  const colourEl = document.getElementById("pad-ed-colour");
  if (colourEl && pad.colour !== null && pad.colour !== undefined) {
    const want = String(pad.colour);
    // An index the palette does not name (should not happen) gets its own option rather than
    // silently showing the wrong colour.
    if (![...colourEl.options].some((o) => o.value === want)) {
      const opt = document.createElement("option");
      opt.value = want;
      opt.textContent = "Colour " + want;
      colourEl.appendChild(opt);
    }
    colourEl.value = want;
  }

  // Volume (padGain) only applies to a pad that plays a SOUND file; a voice-FX pad has no sound
  // to set a level on (its loudness is the effect's own mix), so the control is hidden for it.
  showEl("pad-ed-volume-row", isSound);
  const volEl = document.getElementById("pad-ed-volume");
  if (volEl && isSound) {
    const g = (pad.gain !== null && pad.gain !== undefined) ? pad.gain : -12;
    volEl.value = String(g);
    volEl.setAttribute("aria-valuetext", g + " dB");   // NVDA reads "X dB", not a bare number
  }

  // How the pad plays (sound pads only). Replay only means anything in Toggle mode, and Loop
  // cannot apply in One Shot, so both rows follow the chosen mode instead of always showing.
  showEl("pad-ed-playmode-row", isSound);
  const playEl = document.getElementById("pad-ed-playmode");
  const playMode = isSound && pad.play_mode !== null && pad.play_mode !== undefined
    ? pad.play_mode : null;
  if (playEl && playMode !== null) {
    const want = String(playMode);
    if ([...playEl.options].some((o) => o.value === want)) playEl.value = want;
  }
  const oneShot = playMode === padsPlayModeOneShot;

  showEl("pad-ed-replay-row", isSound);
  showEl("pad-ed-loop-row", isSound && !oneShot);
  const replayEl = document.getElementById("pad-ed-replay");
  if (replayEl && isSound) replayEl.checked = pad.replay === true;
  const loopEl = document.getElementById("pad-ed-loop");
  if (loopEl && isSound) loopEl.checked = pad.loop === true;

  // Mixer pads: trigger mode always; the Back Channel members only on a Back Channel pad,
  // which the server marks by sending a back_channel map (null on every other pad).
  const isMixer = present && pad.type === 3;
  showEl("pad-ed-mixer-trigger-row", isMixer);
  const trigEl = document.getElementById("pad-ed-mixer-trigger");
  if (trigEl && isMixer && pad.mixer_trigger !== null && pad.mixer_trigger !== undefined) {
    const want = String(pad.mixer_trigger);
    if ([...trigEl.options].some((o) => o.value === want)) trigEl.value = want;
  }
  // MIDI trigger pads: what the pad sends (built from the server's spec; see fillTriggerSettings).
  fillTriggerSettings(pad);
  // A sound pad's trim/fade envelope, a Fade pad's times, a Censor pad's own sound. Each shows only
  // on the pad type that has it, so no pad ever carries a control that does nothing.
  fillEnvelopeSettings(pad);
  fillFadeSettings(pad);
  fillCensorSettings(pad);

  const members = isMixer ? pad.back_channel : null;
  showEl("pad-ed-backchannel-row", !!members);
  if (members) {
    Object.keys(members).forEach((prop) => {
      const cb = document.getElementById("pad-ed-bc-" + prop);
      if (cb) cb.checked = members[prop] === true;
    });
  }
}

/* ---- Confirm modal (true alertdialog) for any destructive action ----------
   NOT PAD-ONLY despite the ids: mix schemes, deleting a recording and the access link all use it.
   The DOM ids stay `pad-confirm-*` because they are load-bearing in index.html and in the pads
   checks. */

let _padConfirmResolve = null;
let _padConfirmReturnFocus = null;

function confirmAction(title, desc, okLabel) {
  return new Promise((resolve) => {
    _padConfirmResolve = resolve;
    setText("pad-confirm-title", title);
    setText("pad-confirm-desc", desc);
    const okBtn = document.getElementById("pad-confirm-ok");
    okBtn.textContent = okLabel || "Replace";
    _padConfirmReturnFocus = document.activeElement;
    const dlg = document.getElementById("pad-confirm-dialog");
    dlg.showModal();
    document.getElementById("pad-confirm-cancel").focus();
  });
}

function resolvePadConfirm(value) {
  const dlg = document.getElementById("pad-confirm-dialog");
  if (dlg.open) dlg.close();
  const r = _padConfirmResolve;
  _padConfirmResolve = null;
  if (_padConfirmReturnFocus && _padConfirmReturnFocus.focus) {
    try { _padConfirmReturnFocus.focus(); } catch (e) { /* element gone */ }
  }
  if (r) r(!!value);
}

/* ---- Apply a type change (create / replace pad content) -------------- */

async function onPadApply() {
  const pad = currentPad();
  if (!pad) return;
  const key = document.getElementById("pad-ed-type").value;
  if (key === "empty") return;     // Apply is hidden for Empty

  // Validate the type-specific input before we touch the device.
  let file = null;
  if (key === "sound") {
    const fi = document.getElementById("pad-ed-file");
    file = fi && fi.files && fi.files[0];
    if (!file) { announce("Choose a WAV or MP3 file first.", true); fi.focus(); return; }
  }

  // Replacing existing content is destructive: confirm via a true modal first.
  if (pad.present) {
    const ok = await confirmAction(
      "Replace this pad?",
      "This replaces " + padContentLabel(pad) + " on " + padWhere(pad) +
      ". It is reversible by setting the pad again.",
      "Replace");
    if (!ok) { announce("Cancelled. The pad was not changed."); return; }
  }

  const btn = document.getElementById("pad-ed-apply-btn");
  setBusy(btn, true);
  let res = null;
  try {
    if (key === "sound") {
      res = await applySound(pad, file);
    } else if (key === "fx") {
      const fx = document.getElementById("pad-ed-fx").value;
      res = await applyJsonAction("/api/pad/assign-fx", { pad: pad.grid, effect: fx },
        "Putting " + fx + " on " + padWhere(pad) + ", please wait.");
    } else if (key === "mixer") {
      const mode = document.getElementById("pad-ed-mixer-mode");
      const label = mode.options[mode.selectedIndex].text;
      res = await applyJsonAction("/api/pad/create-special", { pad: pad.grid, type: "mixer:" + mode.value },
        "Putting the " + label + " action on " + padWhere(pad) + ", please wait.");
    } else if (key === "trigger") {
      res = await applyJsonAction("/api/pad/create-special", { pad: pad.grid, type: "trigger" },
        "Putting a MIDI trigger on " + padWhere(pad) + ", please wait.");
    }
  } finally {
    setBusy(btn, false);
  }
  afterPadApply(res);
}

/* A successful Apply has finished the job Reassign exists for, and the next thing wanted is nearly
   always the new content's volume or looping, so it moves to Settings. ONE announcement carries
   both the result and where you now are: announce() replaces the live region's text rather than
   queueing, so two calls in a row would leave the first unspoken. The tab is selected FIRST, so the
   screen reader's own "Settings, tab" comes before the polite message rather than cutting it off. */
function afterPadApply(res) {
  if (!res || !res.ok) {
    if (res) announce(res.message || "Could not change the pad.", true);
    return;
  }
  if (padsTabs) padsTabs.selectKey("settings");
  const pad = currentPad();
  const where = pad ? padWhere(pad) : "the pad";
  const now = pad && pad.present
    ? " Now on Settings for " + where + ", " + padContentLabel(pad) + "."
    : "";
  announce((res.message || "Done.") + now);
}

/* Both apply paths RETURN their result and leave the announcing to afterPadApply, so a successful
   apply speaks once rather than once for the write and again for the tab move. They still write the
   page-level status line themselves, since that is read, not spoken. */
async function applySound(pad, file) {
  setText("pad-ed-status", "Writing the sound to " + padWhere(pad) +
    ". This enters transfer mode and can take several seconds, please wait.");
  announce("Writing the sound, please wait.");
  try {
    const buf = await file.arrayBuffer();
    const resp = await fetch(
      "/api/pad/replace?pad=" + encodeURIComponent(pad.grid) + "&replay=1",
      { method: "POST",
        headers: { "Content-Type": "application/octet-stream", "X-Filename": file.name },
        body: buf });
    const res = await resp.json();
    setText("pad-ed-status", res.message || "Done.");
    const fi = document.getElementById("pad-ed-file");
    if (fi) fi.value = "";
    await keepingFocus(loadPads);
    return res;
  } catch (err) {
    setText("pad-ed-status", "Could not reach the device service.");
    announce("Could not reach the device service.", true);
    return null;
  }
}

async function applyJsonAction(url, body, working) {
  setText("pad-ed-status", working);
  announce(working);
  try {
    const res = await postJSON(url, body);
    setText("pad-ed-status", res.message || "Done.");
    await keepingFocus(loadPads);
    return res;
  } catch (err) {
    setText("pad-ed-status", "Could not reach the device service.");
    announce("Could not reach the device service.", true);
    return null;
  }
}

/* ---- Live, in-place edits to the current pad ------------------------- */

async function padEditPost(url, body) {
  try {
    const res = await postJSON(url, body);
    setText("pad-ed-status", res.message || "Done.");
    announce(res.message || "Done.", !res.ok);
    return res;
  } catch (err) {
    setText("pad-ed-status", "Could not reach the device service.");
    announce("Could not reach the device service.", true);
    return { ok: false };
  }
}

// Live edits update local state in place rather than re-reading the whole device, so the
// editor does not lag or re-announce after every keystroke. Apply/Clear (which change the
// pad's content) still do a full reload.
async function onPadRename() {
  const pad = currentPad();
  const nameEl = document.getElementById("pad-ed-name");
  if (!pad || !pad.present) { announce("Pick a pad with content first.", true); return; }
  if (!nameEl.value.trim()) { announce("Enter a name for the pad.", true); nameEl.focus(); return; }
  const res = await padEditPost("/api/pad/name", { pad: pad.grid, name: nameEl.value.trim() });
  if (res.ok) {
    pad.name = nameEl.value.trim();
    // The pad chooser reads out the pad's content, so a rename has to reach it. Re-filling it here
    // (rather than patching the one option) keeps fillOptions' fingerprint honest, so the next
    // per-tab read does not rebuild the list a second time; focus is on the Rename button, not in
    // the chooser, so the rebuild is not heard.
    fillPadSelect("pad-ed-select", (padsData && padsData.pads) || []);
    setText("pad-ed-type-note", "Currently: " + padContentLabel(pad) + ".");
  }
}

// Volume is a slider. NVDA announces each value as you move it (native slider behaviour);
// the device write is debounced so it commits once, a moment after you stop, instead of
// once per step. _volDebounce is cleared on pad change so a pending write never lands on
// the wrong pad.
let _volDebounce = null;

function onPadVolumeInput() {
  const volEl = document.getElementById("pad-ed-volume");
  if (!volEl) return;
  volEl.setAttribute("aria-valuetext", volEl.value + " dB");
  if (_volDebounce) clearTimeout(_volDebounce);
  _volDebounce = setTimeout(commitPadVolume, PAD_WRITE_DEBOUNCE_MS);
}

async function commitPadVolume() {
  _volDebounce = null;
  const pad = currentPad();
  const volEl = document.getElementById("pad-ed-volume");
  if (!pad || !pad.present || !volEl || volEl.value === "") return;
  const db = parseFloat(volEl.value);
  if (pad.gain === db) return;            // already at this value; nothing to write
  const res = await padEditPost("/api/pad/gain", { pad: pad.grid, db: db });
  if (res.ok) pad.gain = db;
}

async function onPadColour() {
  const pad = currentPad();
  const colourEl = document.getElementById("pad-ed-colour");
  if (!pad || !pad.present || colourEl.value === "") return;
  const colour = Math.max(0, Math.min(11, parseInt(colourEl.value, 10) || 0));
  if (pad.colour === colour) return;      // already this colour; nothing to write
  setBusy(colourEl, true);
  const res = await padEditPost("/api/pad/colour", { pad: pad.grid, colour });
  setBusy(colourEl, false);
  if (res.ok) {
    pad.colour = colour;
  } else if (pad.colour !== null && pad.colour !== undefined) {
    colourEl.value = String(pad.colour);   // put the control back to the truth on failure
  }
}

async function onPadToggle(url, cb) {
  const pad = currentPad();
  if (!pad || !pad.present) return;
  setBusy(cb, true);
  const res = await padEditPost(url, { pad: pad.grid, on: cb.checked });
  if (!res.ok) cb.checked = !cb.checked;
  else if (url.endsWith("/replay")) pad.replay = cb.checked;
  else if (url.endsWith("/loop")) pad.loop = cb.checked;
  setBusy(cb, false);
}

// Build the Back Channel member checkboxes from the device's own list. Native checkboxes in a
// fieldset/legend group, one label each, so every screen reader announces "<name>, checkbox, not
// checked, grouped, Who is in the Back Channel".
// Built ONCE per list, not once per read: this sits on the Settings tab, which re-reads the device
// whenever it is opened, and rebuilding these ticks underneath a screen reader would speak
// fragments of the room list over whatever it was reading. fillPadSettings sets the ticks.
function buildBackChannelList(sources) {
  const box = document.getElementById("pad-ed-backchannel-list");
  if (!box) return;
  const want = JSON.stringify(sources.map((s) => [s.prop, s.label]));
  if (box.dataset.filled === want) return;
  box.dataset.filled = want;
  box.innerHTML = "";
  sources.forEach((s) => {
    const p = document.createElement("p");
    p.className = "field";
    const lab = document.createElement("label");
    lab.className = "switch";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.id = "pad-ed-bc-" + s.prop;
    cb.dataset.source = s.prop;
    cb.addEventListener("change", () => onBackChannelToggle(cb));
    const span = document.createElement("span");
    span.textContent = s.label;
    lab.appendChild(cb);
    lab.appendChild(span);
    p.appendChild(lab);
    box.appendChild(p);
  });
}

/* ---- Trim and fade (sound pads), fade times, censor sound ----------------
   All three groups are built from the server's spec (soundpad.ENV_EDIT / FADE_EDIT, in the payload
   as extra_edit), same as the trigger fields.

   THE ENVELOPE IS FOUR POSITIONS through the sound, ear-proven, and the editor works in
   PERCENT because the desk never reports a file's duration, so seconds could not be shown honestly.
   Sliders here rather than number boxes, the opposite choice to the MIDI numbers: a trim point is a
   value you feel your way to, 0 to 100 arrows one percent at a time, and there is no exact number a
   user arrives with. */
function buildScaleFields(hostId, fields, unit, onCommit) {
  const host = document.getElementById(hostId);
  if (!host || !fields) return;
  const want = JSON.stringify(fields);
  if (host.dataset.filled === want) return;      // structure once; values every read
  host.dataset.filled = want;
  host.innerHTML = "";
  fields.forEach((f) => {
    const row = document.createElement("div");
    row.className = "field";
    const id = hostId + "-" + f.key;
    const helpId = id + "-help";
    const label = document.createElement("label");
    label.setAttribute("for", id);
    label.textContent = f.label;
    const input = document.createElement("input");
    input.type = "range";
    input.id = id;
    input.min = String(f.min);
    input.max = String(f.max);
    input.step = unit === "percent" ? "1" : "0.5";
    input.dataset.field = f.key;
    input.setAttribute("aria-describedby", helpId);
    // The slider's own aria-valuetext carries the unit, so nothing needs a second live region
    // reading the value out on top of it (the lesson from the brightness sliders).
    input.addEventListener("input", () => setScaleValueText(input, unit));
    input.addEventListener("change", () => onCommit(input, f));
    const help = document.createElement("span");
    help.className = "hint";
    help.id = helpId;
    help.textContent = f.help;
    row.appendChild(label);
    row.appendChild(input);
    row.appendChild(help);
    host.appendChild(row);
  });
}

function setScaleValueText(input, unit) {
  input.setAttribute("aria-valuetext",
    unit === "percent" ? input.value + " percent" : input.value + " seconds");
}

function fillEnvelopeSettings(pad) {
  const isSound = !!(pad && pad.present && pad.type === 1);
  showEl("pad-ed-env-row", isSound);
  if (!isSound) return;
  const spec = (padsData && padsData.extra_edit) || null;
  buildScaleFields("pad-ed-env-fields", spec && spec.envelope, "percent", onEnvelopeChange);
  const env = pad.envelope || {};
  ((spec && spec.envelope) || []).forEach((f) => {
    const el = document.getElementById("pad-ed-env-fields-" + f.key);
    if (!el || env[f.key] === null || env[f.key] === undefined) return;
    el.value = String(Math.round(env[f.key] * 100));
    setScaleValueText(el, "percent");
  });
  // A fade point outside the played part is accepted by the desk and does nothing at all. That is
  // invisible, so it is said out loud here rather than left as a control that achieves nothing.
  setText("pad-ed-env-note", envelopeNote(env));
}

function envelopeNote(env) {
  const v = (k, d) => (env[k] === null || env[k] === undefined ? d : env[k]);
  const start = v("start", 0), stop = v("stop", 1);
  const fin = v("fade_in", 0), fout = v("fade_out", 1);
  if (start >= stop) return "Nothing will play: the start point is at or after the stop point.";
  const bad = [];
  if (fin <= start) bad.push("no fade in");
  if (fout >= stop) bad.push("no fade out");
  return bad.length ? "This pad has " + bad.join(" and ") + "." : "";
}

async function onEnvelopeChange(el, f) {
  const pad = currentPad();
  if (!pad || !pad.present) return;
  const percent = parseInt(el.value, 10);
  if (Number.isNaN(percent)) return;
  setBusy(el, true);
  const res = await padEditPost("/api/pad/envelope",
    { pad: pad.grid, field: f.key, percent });
  setBusy(el, false);
  if (res.ok) {
    if (pad.envelope) pad.envelope[f.key] = percent / 100;
    setText("pad-ed-env-note", envelopeNote(pad.envelope || {}));
  } else if (pad.envelope && pad.envelope[f.key] !== null) {
    el.value = String(Math.round(pad.envelope[f.key] * 100));   // back to the truth on failure
    setScaleValueText(el, "percent");
  }
}

function fillFadeSettings(pad) {
  const fade = (pad && pad.present && pad.fade) || null;
  showEl("pad-ed-fade-row", !!fade);
  if (!fade) return;
  const spec = (padsData && padsData.extra_edit) || null;
  buildScaleFields("pad-ed-fade-fields", spec && spec.fade, "seconds", onFadeChange);
  ((spec && spec.fade) || []).forEach((f) => {
    const el = document.getElementById("pad-ed-fade-fields-" + f.key);
    if (!el || fade[f.key] === null || fade[f.key] === undefined) return;
    el.value = String(fade[f.key]);
    setScaleValueText(el, "seconds");
  });
  const host = document.getElementById("pad-ed-fade-host");
  if (host) host.checked = fade.exclude_host === true;
}

async function onFadeChange(el, f) {
  const pad = currentPad();
  if (!pad || !pad.present) return;
  const seconds = parseFloat(el.value);
  if (Number.isNaN(seconds)) return;
  setBusy(el, true);
  const res = await padEditPost("/api/pad/fade", { pad: pad.grid, field: f.key, seconds });
  setBusy(el, false);
  if (res.ok && pad.fade) pad.fade[f.key] = seconds;
  else if (!res.ok && pad.fade && pad.fade[f.key] !== null) {
    el.value = String(pad.fade[f.key]);
    setScaleValueText(el, "seconds");
  }
}

function fillCensorSettings(pad) {
  const censor = (pad && pad.present && pad.censor) || null;
  showEl("pad-ed-censor-row", !!censor);
  if (!censor) return;
  const cb = document.getElementById("pad-ed-censor-custom");
  if (cb) cb.checked = censor.custom === true;
  const file = (censor.file || "").split("/").pop();
  setText("pad-ed-censor-file", file
    ? "The sound on the unit for this pad is " + file + "."
    : "No sound of your own is on the unit for this pad, so it uses the built-in beep.");
}

// Both switches go through one named endpoint (soundpad.PAD_FLAG), so the browser can never ask for
// an arbitrary pad property write.
async function onPadFlag(name, cb) {
  const pad = currentPad();
  if (!pad || !pad.present) return;
  setBusy(cb, true);
  const res = await padEditPost("/api/pad/flag", { pad: pad.grid, name, on: cb.checked });
  setBusy(cb, false);
  if (!res.ok) { cb.checked = !cb.checked; return; }
  if (name === "fade_exclude_host" && pad.fade) pad.fade.exclude_host = cb.checked;
  if (name === "censor_custom" && pad.censor) pad.censor.custom = cb.checked;
}

/* ---- MIDI trigger pads: what the pad sends ------------------------------
   A trigger pad sends ONE MIDI message to the computer when pressed. The firmware's default is a
   Control Change on the pad's own number (bank 1 pad 1 = CC 0, counting up), channel 1, 127 pressed
   and 0 released; the desk can also send a Note, on any control number, channel and pair of values,
   and can do it latching (press on, press again off) or momentary (on while held).

   The controls are BUILT FROM THE SERVER'S SPEC (soundpad.TRIGGER_EDIT, handed over in the
   /api/soundpads payload as trigger_edit), so the label, the help text and the limits of each field
   come from the same definition that validates the write and that a profile backs up. Adding a field
   to the spec puts it on the page with no change here.

   TWO DELIBERATE CHOICES:
   - The numbers are `input type="number"`, not sliders. The rest of the app prefers sliders and says
     so, but that is for values you feel your way to (a level, an amount). A MIDI control number is a
     value you KNOW, and arrowing from 0 to 60 on a slider is 60 keypresses; a number box takes the
     digits and still arrows by one.
   - Everything is disabled until the custom-mapping switch is on. The firmware only honours these
     values behind that flag, and offering a control that would silently do nothing is the exact
     failure this project keeps designing out. */
function buildTriggerFields(spec) {
  const host = document.getElementById("pad-ed-trigger-fields");
  if (!host || !spec) return;
  const want = JSON.stringify(spec);
  if (host.dataset.filled === want) return;      // structure once; values every read
  host.dataset.filled = want;
  host.innerHTML = "";
  (spec.fields || []).forEach((f) => {
    const row = document.createElement("div");
    row.className = "field";
    const id = "pad-ed-trig-" + f.key;
    const helpId = id + "-help";
    const label = document.createElement("label");
    label.setAttribute("for", id);
    label.textContent = f.label;
    let input;
    if (f.choices) {
      input = document.createElement("select");
      (spec[f.choices] || []).forEach((c) => {
        const o = document.createElement("option");
        o.value = String(c.value);
        o.textContent = c.label;
        input.appendChild(o);
      });
    } else {
      input = document.createElement("input");
      input.type = "number";
      input.min = String(f.min);
      input.max = String(f.max);
      input.step = "1";
      input.inputMode = "numeric";
    }
    input.id = id;
    input.dataset.field = f.key;
    input.setAttribute("aria-describedby", helpId);
    input.addEventListener("change", () => onTriggerField(input, f));
    const help = document.createElement("span");
    help.className = "hint";
    help.id = helpId;
    help.textContent = f.help;
    row.appendChild(label);
    row.appendChild(input);
    row.appendChild(help);
    host.appendChild(row);
  });
}

// Plain English for what the pad sends right now, so the answer to "what does this pad actually do"
// is on the page rather than inferred from six separate controls.
function triggerSummary(pad, t, types, modes) {
  if (!t) return "";
  const label = (list, v) => {
    const hit = (list || []).find((x) => String(x.value) === String(v));
    return hit ? hit.label : String(v);
  };
  // WITH CUSTOM OFF, do not read this out of the record. Turning the flag off makes the desk reset
  // the numbers to its defaults but LEAVES padTriggerType where it was (measured), so the
  // record can say "Note" on a pad whose default mapping is a control change, and what it really
  // sends is unproven. The default is a property of the pad's POSITION, so state that instead.
  if (!t.custom) {
    return "This pad is on the default mapping: control change " + (pad.grid - 1)
      + " on channel 1, value 127 when pressed and 0 when released.";
  }
  return "Now sending " + (String(t.type) === "1" ? "note " : "control change ") + t.control
    + " on channel " + t.channel + ", value " + t.on
    + " when pressed and " + t.off + " when released, "
    + label(modes, t.mode).toLowerCase() + ".";
}

function fillTriggerSettings(pad) {
  const isTrigger = !!(pad && pad.present && pad.type === 4);
  showEl("pad-ed-trigger-row", isTrigger);
  if (!isTrigger) return;
  const spec = (padsData && padsData.trigger_edit) || null;
  buildTriggerFields(spec);
  const t = pad.trigger || {};
  const custom = document.getElementById("pad-ed-trigger-custom");
  if (custom) custom.checked = t.custom === true;
  ((spec && spec.fields) || []).forEach((f) => {
    const el = document.getElementById("pad-ed-trig-" + f.key);
    if (!el || t[f.key] === null || t[f.key] === undefined) return;
    el.value = String(t[f.key]);
    // The custom flag is what the firmware honours, so a control that would do nothing says so
    // rather than accepting a value silently. aria-disabled, NOT the attribute: these are the
    // fields somebody arrows through while setting a pad up, and a disabled one is skipped with no
    // explanation at all. onTriggerField refuses the write and says why.
    setAvailable(el, t.custom === true,
      "This pad is on a preset mapping, so the individual fields do nothing. Turn on Custom "
      + "mapping first.");
  });
  setText("pad-ed-trigger-current",
    triggerSummary(pad, t, spec && spec.types, spec && spec.modes));
}

async function onTriggerField(el, f) {
  const no = unavailableReason(el);
  if (no !== null) { announce(no, true); return; }
  const pad = currentPad();
  if (!pad || !pad.present || el.value === "") return;
  const value = parseInt(el.value, 10);
  if (Number.isNaN(value)) return;
  if (value < f.min || value > f.max) {
    announce(f.label + " must be between " + f.min + " and " + f.max + ".", true);
    if (pad.trigger && pad.trigger[f.key] !== null) el.value = String(pad.trigger[f.key]);
    return;
  }
  if (pad.trigger && pad.trigger[f.key] === value) return;   // already there; nothing to write
  setBusy(el, true);
  const res = await padEditPost("/api/pad/trigger", { pad: pad.grid, field: f.key, value });
  setBusy(el, false);
  if (res.ok) {
    if (pad.trigger) pad.trigger[f.key] = value;
    fillPadSettings(pad);          // the summary line and the enabled state follow the new value
  } else if (pad.trigger && pad.trigger[f.key] !== null) {
    el.value = String(pad.trigger[f.key]);   // put the control back to the truth on failure
  }
}

async function onTriggerCustom(cb) {
  const pad = currentPad();
  if (!pad || !pad.present) return;
  setBusy(cb, true);
  const res = await padEditPost("/api/pad/trigger",
    { pad: pad.grid, field: "custom", value: cb.checked ? 1 : 0 });
  setBusy(cb, false);
  if (!res.ok) { cb.checked = !cb.checked; return; }
  if (pad.trigger) pad.trigger.custom = cb.checked;
  fillPadSettings(pad);            // enables or disables the mapping fields to match
}

async function onBackChannelToggle(cb) {
  const pad = currentPad();
  if (!pad || !pad.present) return;
  setBusy(cb, true);
  const res = await padEditPost("/api/pad/backchannel",
    { pad: pad.grid, source: cb.dataset.source, on: cb.checked });
  if (!res.ok) cb.checked = !cb.checked;
  else if (pad.back_channel) pad.back_channel[cb.dataset.source] = cb.checked;
  setBusy(cb, false);
}

async function onPlayMode() {
  const pad = currentPad();
  const sel = document.getElementById("pad-ed-playmode");
  if (!pad || !pad.present || !sel || sel.value === "") return;
  const mode = parseInt(sel.value, 10);
  if (pad.play_mode === mode) return;          // already there; nothing to write
  const res = await padEditPost("/api/pad/playmode", { pad: pad.grid, mode });
  if (res.ok) {
    pad.play_mode = mode;
    if (res.loop_off) pad.loop = false;        // One Shot turns Loop off device-side
    fillPadSettings(pad);                      // show/hide the Loop row for the new mode
  }
}

async function onMixerTrigger() {
  const pad = currentPad();
  const sel = document.getElementById("pad-ed-mixer-trigger");
  if (!pad || !pad.present || !sel || sel.value === "") return;
  const mode = parseInt(sel.value, 10);
  if (pad.mixer_trigger === mode) return;      // already there; nothing to write
  const res = await padEditPost("/api/pad/mixer-trigger", { pad: pad.grid, mode });
  if (res.ok) pad.mixer_trigger = mode;
}

async function onPadClear() {
  const pad = currentPad();
  if (!pad) return;
  if (!pad.present) { announce("This pad is already empty."); return; }
  const ok = await confirmAction(
    "Clear this pad?",
    "This empties " + padWhere(pad) + ", removing " + padContentLabel(pad) +
    ". It is reversible by setting the pad again.",
    "Clear");
  if (!ok) { announce("Cancelled. The pad was not cleared."); return; }
  const btn = document.getElementById("pad-ed-clear-btn");
  setBusy(btn, true);
  setText("pad-ed-status", "Clearing " + padWhere(pad) + ", please wait.");
  announce("Clearing the pad, please wait.");
  try {
    const res = await postJSON("/api/pad/clear", { pad: pad.grid });
    setText("pad-ed-status", res.message || "Done.");
    announce(res.message || "Done.", !res.ok);
    await loadPads();
  } catch (err) {
    setText("pad-ed-status", "Could not reach the device service.");
    announce("Could not reach the device service.", true);
  } finally {
    setBusy(btn, false);
    // Clearing worked, so this button is now genuinely unavailable (nothing left to clear) and
    // fillPadSettings has disabled it. Move focus to the type dropdown, still on this tab, which is
    // where you go next if you are emptying a pad to put something else on it, rather than leaving a
    // screen-reader user on a dead control - or worse, nowhere at all.
    if (btn.disabled) focusFirst(["pad-ed-type", "pad-ed-select"]);
  }
}

async function onClearAllPads() {
  const ok = await confirmAction(
    "Clear all pads?",
    "This empties every soundpad, voice-effect, mixer and MIDI pad on the grid. The built-in " +
    "System pads are left alone. It is reversible only by setting the pads again.",
    "Clear all");
  if (!ok) { announce("Cancelled. No pads were cleared."); return; }
  const btn = document.getElementById("clear-all-btn");
  setBusy(btn, true);
  setStatus("clear-all-status", "Clearing all pads, please wait.");
  try {
    const res = await postJSON("/api/pads/clear-all", {});
    setStatus("clear-all-status", res.message || "Done.");
    await keepingFocus(loadPads);
  } catch (err) {
    setStatus("clear-all-status", "Could not reach the device service.");
  } finally {
    setBusy(btn, false);
  }
}

async function onTidyPadAudio() {
  const ok = await confirmAction(
    "Delete unused pad audio?",
    "This removes sound files from the unit that no pad plays any more, freeing up space. " +
    "Nothing you can currently play is touched, but the files themselves are deleted from the " +
    "unit and cannot be recovered.",
    "Delete unused audio");
  if (!ok) { announce("Cancelled. Nothing was deleted."); return; }
  const btn = document.getElementById("tidy-audio-btn");
  setBusy(btn, true);
  setStatus("tidy-audio-status", "Looking for sound files no pad plays, please wait.");
  try {
    const res = await postJSON("/api/pads/tidy-audio", {});
    setStatus("tidy-audio-status", res.message || "Done.");
  } catch (err) {
    setStatus("tidy-audio-status", "Could not reach the device service.");
  } finally {
    setBusy(btn, false);
  }
}

/* Restore just the six factory VOICE EFFECTS. Deliberately a separate button from Restore pad
   defaults rather than an option inside it: the two differ in what they DESTROY, and that is not a
   thing to bury in a dropdown. Shares the pad-restore progress channel, so it shares pollPadRestore
   and reads the same way. */
async function onRestoreFactoryFx() {
  const ok = await confirmAction(
    "Restore the factory voice effects, Censor and Ducking?",
    "This puts the six factory voice effects back on their own pads, and the Censor and Ducking " +
    "pads with them, overwriting whatever is on those eight pads now. The factory sounds are not " +
    "touched and cannot be restored from here, and neither is anything past pad 16. It takes two " +
    "or three minutes, and is reversible only by setting those pads again.",
    "Restore those eight pads");
  if (!ok) { announce("Cancelled. The pads were not changed."); return; }
  const btn = document.getElementById("restore-fx-btn");
  setBusy(btn, true);
  setStatus("restore-fx-status", "Restoring the factory voice effects, Censor and Ducking. "
    + "Two or three minutes, please wait.");
  try {
    const start = await postJSON("/api/pads/restore-fx", {});
    if (!start.ok) {
      setStatus("restore-fx-status", start.message || "Could not start the restore.");
      return;
    }
    await pollPadRestore("restore-fx-status");
    await keepingFocus(loadPads);
  } catch (err) {
    setStatus("restore-fx-status", "Could not reach the device service.");
  } finally {
    setBusy(btn, false);
  }
}

/* Export the CHOSEN pad's sound. No confirm: nothing on the unit changes, and asking permission to
   copy a file would be noise. The pad it acts on is the one in the chooser above the tab strip -
   the same pad every other tab acts on - so the reply names it, and the pad number, to make that
   unambiguous without having to go back and check. */
async function onExportPad() {
  const sel = document.getElementById("pad-ed-select");
  const grid = sel ? parseInt(sel.value, 10) : NaN;
  if (!grid) { announce("Choose a pad first.", true); return; }
  const btn = document.getElementById("export-pad-btn");
  setBusy(btn, true);
  setStatus("export-pad-status", `Copying pad ${grid} off the unit, please wait.`);
  try {
    const res = await postJSON("/api/pad/export", { pad: grid });
    setStatus("export-pad-status", res.message || "Done.");
  } catch (err) {
    setStatus("export-pad-status", "Could not reach the device service.");
  } finally {
    setBusy(btn, false);
  }
}

async function onOpenPadExports() {
  try {
    const res = await postJSON("/api/pads/exports/open", {});
    announce(res.message || "Done.", !res.ok);
    setStatus("export-pad-status", res.message || "");
  } catch (err) {
    announce("Could not reach the device service.", true);
  }
}

// Poll the pad-restore status until done, announcing each new step through the live region.
// `statusId` is which status line to write into: the two restores share this channel, so the one
// that started it has to be the one that reports, or a message lands under the wrong heading.
function pollPadRestore(statusId) {
  const line = statusId || "restore-fx-status";
  return new Promise((resolve) => {
    let lastStep = 0;
    const iv = setInterval(async () => {
      let s;
      try {
        s = await (await fetch("/api/pads/restore-defaults/status")).json();
      } catch (e) {
        return;   // transient; try again on the next tick
      }
      if (!s.done && s.step && s.step !== lastStep) {
        lastStep = s.step;
        setText(line, s.message || "");
        announce(`${s.label}, ${s.step} of ${s.total}.`);
      }
      if (s.done) {
        clearInterval(iv);
        setText(line, s.message || "Done.");
        announce(s.message || "Done.", !s.ok);
        resolve();
      }
    }, 1200);
  });
}

/* ---- Custom voice-FX creator -------------------------------------------
   Build a voice effect from the device's six engines and save it as a preset.
   Spec-driven so each slider gets a self-describing accessible name (e.g.
   "Reverb amount"), which survives NVDA caching the fieldset legend. Percent
   sliders map 0-100 to the device's 0..1; pitch is in real semitones; the
   robot type is a discrete select. No spin boxes: numeric controls are sliders. */
const FX_BUILD_SPEC = [
  { engine: "reverb", label: "Reverb",
    controls: [
      { param: "reverbMix", label: "Amount", def: 50 },
      { param: "reverbModel", label: "Type or size", def: 60, advanced: true },
      { param: "reverbLowCut", label: "Low cut", def: 67, advanced: true },
      { param: "reverbHighCut", label: "High cut", def: 33, advanced: true },
    ] },
  { engine: "echo", label: "Echo",
    controls: [
      { param: "echoMix", label: "Level", def: 50 },
      { param: "echoDelay", label: "Delay, 0 to 1 second", def: 17, advanced: true },
      { param: "echoDecay", label: "Decay", def: 50, advanced: true },
      { param: "echoLowCut", label: "Low cut", def: 50, advanced: true },
      { param: "echoHighCut", label: "High cut", def: 50, advanced: true },
    ] },
  { engine: "distortion", label: "Megaphone",
    controls: [ { param: "distortionIntensity", label: "Intensity", def: 70 } ] },
  // robotMix is the audible control (factory Large vs Small Robot differ on it; robotLevel is 0
  // in both and was inaudible in testing), so it is the simple slider; robotLevel sits in Advanced.
  { engine: "robot", label: "Robot",
    controls: [
      { param: "robotMix", label: "Amount", def: 67 },
      { param: "robotLevel", label: "Level", def: 0, advanced: true },
    ] },
  { engine: "pitchShift", label: "Pitch shift",
    controls: [ { param: "pitchShiftSemitones", label: "Semitones", def: 7, kind: "semitones" } ] },
  { engine: "voiceDisguise", label: "Voice disguise", controls: [] },
];

function semitoneText(v) {
  v = parseInt(v, 10);
  if (v === 0) return "no change";
  return Math.abs(v) + (v > 0 ? " semitones up" : " semitones down");
}

// One numeric/select control. Returns the wrapping field element; reads its value via
// el._fxRead() which yields the device value (0..1, raw semitones, or the select number).
function buildFxControl(effLabel, ctl, idBase) {
  const id = idBase + "-" + ctl.param;
  const field = document.createElement("div");
  field.className = "field fx-ctl";
  field.dataset.param = ctl.param;
  const label = document.createElement("label");
  label.setAttribute("for", id);
  label.textContent = effLabel + " " + ctl.label.toLowerCase();   // e.g. "Reverb amount"

  if (ctl.kind === "select") {
    const sel = document.createElement("select");
    sel.id = id;
    ctl.options.forEach(([v, t]) => {
      const o = document.createElement("option");
      o.value = v; o.textContent = t;
      if (parseFloat(v) === ctl.def) o.selected = true;
      sel.appendChild(o);
    });
    field._fxRead = () => parseFloat(sel.value);
    sel.addEventListener("change", () => streamPreview(ctl.param, parseFloat(sel.value)));
    field.appendChild(label);
    field.appendChild(sel);
    return field;
  }

  const semis = ctl.kind === "semitones";
  const input = document.createElement("input");
  input.type = "range";
  input.id = id;
  input.min = semis ? "-12" : "0";
  input.max = semis ? "12" : "100";
  input.step = "1";
  input.value = String(ctl.def);
  const out = document.createElement("span");
  out.className = "fx-ctl-value";
  out.setAttribute("aria-hidden", "true");
  const fmt = () => semis ? semitoneText(input.value) : input.value + " percent";
  const show = () => { input.setAttribute("aria-valuetext", fmt()); out.textContent = fmt(); };
  show();
  field._fxRead = () => semis ? parseInt(input.value, 10) : (parseInt(input.value, 10) / 100);
  input.addEventListener("input", () => { show(); streamPreview(ctl.param, field._fxRead()); });
  field.appendChild(label);
  field.appendChild(input);
  field.appendChild(out);
  return field;
}

// Build the six per-effect fieldsets into #fx-create-effects (once).
function buildFxCreator() {
  const host = document.getElementById("fx-create-effects");
  if (!host || host.dataset.built === "1") return;
  host.dataset.built = "1";

  FX_BUILD_SPEC.forEach((eff) => {
    const fs = document.createElement("fieldset");
    fs.className = "fx-eff";
    fs.dataset.engine = eff.engine;
    const lg = document.createElement("legend");
    lg.textContent = eff.label;
    fs.appendChild(lg);

    // Include toggle.
    const onRow = document.createElement("p");
    onRow.className = "field";
    const onLab = document.createElement("label");
    onLab.className = "switch";
    const onCb = document.createElement("input");
    onCb.type = "checkbox";
    onCb.className = "fx-on";
    const onSpan = document.createElement("span");
    onSpan.textContent = "Include " + eff.label.toLowerCase();
    onLab.appendChild(onCb); onLab.appendChild(onSpan);
    onRow.appendChild(onLab);
    fs.appendChild(onRow);

    if (eff.controls.length === 0) {
      const note = document.createElement("p");
      note.className = "hint";
      note.textContent = "No settings: it is simply on or off.";
      fs.appendChild(note);
    } else {
      const controls = document.createElement("div");
      controls.className = "fx-eff-controls";
      controls.hidden = true;
      const idBase = "fx-" + eff.engine;
      const simple = eff.controls.filter((c) => !c.advanced);
      const advanced = eff.controls.filter((c) => c.advanced);
      simple.forEach((c) => controls.appendChild(buildFxControl(eff.label, c, idBase)));

      if (advanced.length) {
        const advWrap = document.createElement("div");
        const advId = idBase + "-adv";
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "fx-adv-toggle";
        toggle.setAttribute("aria-expanded", "false");
        toggle.setAttribute("aria-controls", advId);
        toggle.textContent = "Advanced " + eff.label.toLowerCase() + " controls";
        const advBox = document.createElement("div");
        advBox.id = advId;
        advBox.className = "fx-adv";
        advBox.hidden = true;
        advanced.forEach((c) => advBox.appendChild(buildFxControl(eff.label, c, idBase)));
        toggle.addEventListener("click", () => {
          const open = toggle.getAttribute("aria-expanded") === "true";
          toggle.setAttribute("aria-expanded", String(!open));
          advBox.hidden = open;
        });
        advWrap.appendChild(toggle);
        advWrap.appendChild(advBox);
        controls.appendChild(advWrap);
      }
      fs.appendChild(controls);

      onCb.addEventListener("change", () => {
        controls.hidden = !onCb.checked;
        announce((onCb.checked ? "Included " : "Removed ") + eff.label.toLowerCase() + ".");
        streamPreview(eff.engine + "On", onCb.checked);
        // On enable, also push this effect's current control values so it is audible straight
        // away at the slider settings (some engines default to a silent/zero value internally).
        if (onCb.checked) {
          fs.querySelectorAll(".fx-ctl").forEach((c) => {
            if (c.dataset.param && typeof c._fxRead === "function") {
              streamPreview(c.dataset.param, c._fxRead());
            }
          });
        }
      });
    }

    if (eff.controls.length === 0) {
      onCb.addEventListener("change", () => {
        announce((onCb.checked ? "Included " : "Removed ") + eff.label.toLowerCase() + ".");
        streamPreview(eff.engine + "On", onCb.checked);
      });
    }
    host.appendChild(fs);
  });

  // Pad colour choices, by NAME. Cosmetic LED colour only.
  fillColourOptions("fx-create-colour", 3);
}

/* Fill a colour dropdown with the device's named palette (Red, Orange, Amber, ...). The names come
   from the /api/soundpads payload (soundpad.PAD_COLOUR_NAME), so the controls do not offer bare
   numbers 0 to 11, which say nothing about the actual colour. Falls back to numbers only if the
   payload has not arrived yet. */
function fillColourOptions(id, selectValue) {
  const list = (padsData && padsData.colours) ||
    Array.from({ length: 12 }, (_, i) => ({ value: i, label: "Colour " + i }));
  const keep = document.getElementById(id) ? document.getElementById(id).value : "";
  const sel = fillOptions(id, list.map((c) => ({ value: c.value, label: c.label })));
  if (!sel) return;
  const want = keep !== "" && keep !== null && keep !== undefined ? keep : String(selectValue);
  if ([...sel.options].some((o) => o.value === want)) sel.value = want;
}

// Read the form into a {engine: {param: value}} stack for the save request.
function readFxStack() {
  const stack = {};
  document.querySelectorAll("#fx-create-effects .fx-eff").forEach((fs) => {
    const on = fs.querySelector(".fx-on");
    if (!on || !on.checked) return;
    const engine = fs.dataset.engine;
    const params = {};
    fs.querySelectorAll(".fx-ctl").forEach((c) => {
      if (c.dataset.param && typeof c._fxRead === "function") params[c.dataset.param] = c._fxRead();
    });
    stack[engine] = params;
  });
  return stack;
}

// Refresh the dropdown + saved-list from a server response (recipes + custom_fx).
function applyFxLists(recipes, customFx) {
  if (Array.isArray(recipes)) {
    fillOptions("pad-ed-fx", recipes.map((n) => ({ value: n, label: n })));
  }
  if (Array.isArray(customFx)) renderCustomFxList(customFx);
}

// Rebuilt only when the saved list has really changed. It is re-rendered by every pads read, and
// the Settings and Reassign tabs now read on open, so an unconditional rebuild could pull the list
// apart underneath a screen reader that was reading it (and would lose focus on a Delete button).
function renderCustomFxList(names) {
  const ul = document.getElementById("fx-custom-list");
  if (!ul) return;
  const want = JSON.stringify(names);
  if (ul.dataset.filled === want) return;
  ul.dataset.filled = want;
  ul.innerHTML = "";
  if (!names.length) {
    const li = document.createElement("li");
    li.className = "hint";
    li.textContent = "You have not created any effects yet.";
    ul.appendChild(li);
    return;
  }
  names.forEach((name, i) => {
    const li = document.createElement("li");
    li.className = "fx-custom-item";
    const span = document.createElement("span");
    span.textContent = name;
    const del = document.createElement("button");
    del.type = "button";
    del.className = "secondary";
    // Stable id per position so focus can land on the row that takes this one's place after a
    // delete, rather than being lost when the list is rebuilt.
    del.id = "fx-del-" + i;
    del.textContent = "Delete";
    del.setAttribute("aria-label", "Delete the " + name + " effect");
    del.addEventListener("click", () => onFxDelete(name, i));
    li.appendChild(span);
    li.appendChild(del);
    ul.appendChild(li);
  });
}

async function onFxCreateSave() {
  const nameEl = document.getElementById("fx-create-name");
  const name = nameEl.value.trim();
  if (!name) { announce("Give the effect a name first.", true); nameEl.focus(); return; }
  const stack = readFxStack();
  if (Object.keys(stack).length === 0) {
    announce("Switch on at least one effect before saving.", true); return;
  }
  const colour = parseInt(document.getElementById("fx-create-colour").value, 10) || 0;
  const btn = document.getElementById("fx-create-save");
  setBusy(btn, true);
  try {
    const res = await postJSON("/api/fx/save", { name, colour, stack });
    setStatus("fx-create-status", res.message || "Done.");
    if (res.ok) {
      applyFxLists(res.recipes, res.custom_fx);
      nameEl.value = "";
      if (previewState.active) await stopPreview(true);  // free the scratch pad
      refreshPreviewAvailability();
    }
  } catch (err) {
    setStatus("fx-create-status", "Could not save the effect.");
  } finally {
    setBusy(btn, false);
  }
}

async function onFxDelete(name, index) {
  try {
    const res = await postJSON("/api/fx/delete", { name });
    setStatus("fx-create-status", res.message || "Done.");
    if (res.ok) {
      applyFxLists(res.recipes, res.custom_fx);
      // The button just pressed no longer exists. Land on the Delete button that took its place
      // (or the one before it, or the name field once the list is empty) so focus is never lost.
      focusFirst(["fx-del-" + index, "fx-del-" + Math.max(0, index - 1), "fx-create-name"]);
    }
  } catch (err) {
    announce("Could not delete the effect.", true);
  }
}

/* ---- Live preview of the in-progress effect ----------------------------
   A scratch pad carries the effect; switching preview on engages it on the
   live mic (hands-free), and each control change streams to it in realtime.
   It only ever touches the mic while the switch is on. */
const previewState = { active: false, grid: null };
const _previewDebounce = {};

// Stream one control change to the preview pad (debounced per control so a slider drag
// commits once you pause, not on every step). param is a param name or an engine on-flag.
function streamPreview(param, value) {
  if (!previewState.active || previewState.grid == null) return;
  clearTimeout(_previewDebounce[param]);
  _previewDebounce[param] = setTimeout(async () => {
    try {
      const res = await postJSON("/api/fx/preview/update",
        { grid: previewState.grid, param, value });
      if (res && !res.ok && res.message) announce(res.message, true);
    } catch (e) { /* transient; the next change will retry */ }
  }, WRITE_DEBOUNCE_MS);
}

function emptyPadAvailable() {
  return !!(padsData && Array.isArray(padsData.pads) && padsData.pads.some((p) => !p.present));
}

// Enable/disable the preview switch from the current pad layout (needs one free pad). Never
// disturbs an active preview.
function refreshPreviewAvailability() {
  const toggle = document.getElementById("fx-preview-toggle");
  if (!toggle || previewState.active) return;
  const ok = emptyPadAvailable();
  // Same rule: unavailable in place, with the reason, rather than a switch that disappears from
  // the tab order whenever the grid happens to be full.
  setAvailable(toggle, ok, "Live preview needs one empty pad to borrow, and every pad is in use.");
  setStatus("fx-preview-status", ok ? "" :
    "Live preview needs one free pad to play through. Clear a pad to turn it on. " +
    "You can still build and save this effect.");
}

async function onPreviewToggle() {
  const toggle = document.getElementById("fx-preview-toggle");
  const no = unavailableReason(toggle);
  // A switch with no write behind it must go BACK, or it reports a state the unit is not in.
  if (no !== null) { toggle.checked = false; announce(no, true); return; }
  if (toggle.checked) await startPreview();
  else await stopPreview(false);
}

async function startPreview() {
  const toggle = document.getElementById("fx-preview-toggle");
  const micSel = document.getElementById("fx-preview-mic");
  const stack = readFxStack();
  const colour = parseInt(document.getElementById("fx-create-colour").value, 10) || 0;
  const mic = parseInt(micSel.value, 10) || 1;
  setBusy(toggle, true);
  setStatus("fx-preview-status", "Starting live preview, please wait.");
  try {
    const res = await postJSON("/api/fx/preview/start", { stack, colour, mic });
    if (res.no_pad) {
      toggle.checked = false;
      setStatus("fx-preview-status", res.message || "No free pad for preview.");
      return;
    }
    if (res.ok) {
      previewState.active = true;
      previewState.grid = res.grid;
      setAvailable(micSel, false,
        "The preview is running on this mic. Turn it off to choose another.");
      // Mark the slot used so the availability check stays accurate without a device re-read.
      const p = (padsData.pads || []).find((x) => x.grid === res.grid);
      if (p) { p.present = true; p.name = "Preview"; p.type = 2; }
      setStatus("fx-preview-status", res.message || "Live preview on.");
    } else {
      toggle.checked = false;
      setStatus("fx-preview-status", res.message || "Could not start preview.");
    }
  } catch (e) {
    toggle.checked = false;
    setStatus("fx-preview-status", "Could not reach the device service.");
  } finally {
    setBusy(toggle, false);
  }
}

async function stopPreview(silent) {
  if (!previewState.active) return;
  const grid = previewState.grid;
  previewState.active = false;        // stop streaming before the clear lands
  previewState.grid = null;
  const toggle = document.getElementById("fx-preview-toggle");
  const micSel = document.getElementById("fx-preview-mic");
  if (toggle) toggle.checked = false;
  if (micSel) setAvailable(micSel, true);
  const p = (padsData && padsData.pads || []).find((x) => x.grid === grid);
  if (p) { p.present = false; p.name = null; p.type = null; }
  try {
    const res = await postJSON("/api/fx/preview/stop", { grid });
    if (!silent) {
      setStatus("fx-preview-status", res.message || "Live preview stopped.");
    }
  } catch (e) { /* best effort */ }
  if (!silent) refreshPreviewAvailability();
}

/* ---- Routing: outputs, input sources, multitrack, auto-mute, headphones --- */

let routingModel = null;
const routingCtl = { outputs: {}, inputs: {},
                     monitorMute: null, btMute: null, headphones: [],
                     usbMode: null, usbBypass: null, usbBypassHint: null };

/* The reasons this page gives when a desk cannot take a write. One string per CAP, said by every
   control that cap gates, so a dropdown and the button beside it never explain the same refusal two
   different ways. */
const FADERS_READ_ONLY_OFF = "Changing the fader layout is not enabled on this device yet.";
const OUTPUTS_READ_ONLY_OFF = "Changing the unit's output settings is not enabled on this device "
  + "yet. You can still read what it has.";
const MONMIX_READ_ONLY_OFF = "Changing levels is not enabled on this device yet. You can still see "
  + "what each one is set to.";
const MATRIX_READ_ONLY_OFF = "Changing output mixes is not enabled on this device yet.";

/* The last values the unit reported for this page, so a refused control can be put back without a
   device read. Written only by applyRoutingValues. */
let _routingValues = null;

/* How long a dropdown waits after you stop moving before it writes to the unit.
   THIS IS NOT COSMETIC. A CLOSED select fires `change` on EVERY arrow press, so walking a
   three-item list sends three writes to the desk and leaves the intermediate value set: a desk
   found on the wrong multitrack mode because somebody arrowed past it. Worse for a screen reader,
   each write marks the control busy, so arrowing the list announces every option as "unavailable"
   while still, confusingly, working. Waiting for the gesture to finish fixes both: one write, of
   the option you actually landed on. */
const SELECT_COMMIT_MS = 500;

/* ---- A DROPDOWN THAT STAGES, AND THE BUTTON THAT COMMITS IT ----------
   THE RULE: a dropdown that changes WHAT COMES OUT OF THE DESK gets one of these. A dropdown that
   only changes WHAT YOU ARE LOOKING AT - which pad, which output, which shortcut - acts at once,
   because nothing leaves the app.

   WHY IT IS NEEDED AT ALL: a CLOSED select fires `change` on every arrow press, and arrowing a
   closed select is exactly how a screen-reader user reads one, so browsing the list IS the action.
   A delay does not fix it: it fires when you pause to listen, which is when you are still
   deciding.

   THE PIECES: the dropdown, a hint that appears ONLY while something is staged (otherwise it would
   repeat what the dropdown already says), and an Apply button that is unavailable until there is
   something to apply, so pressing it can never be a no-op. aria-disabled throughout, never the
   disabled attribute, so nothing leaves the tab order and every refusal can say why.
   `opts.applyText` should NAME what it applies to when there is more than one on a page. */
/* Is this dropdown showing something the unit does NOT have? One definition, used by the button,
   the hint and the group logic, so they cannot disagree about what "staged" means. */
function isStaged(sel) {
  return !!sel && sel.value !== (sel.dataset.deviceValue || "");
}

/* AN APPLY BUTTON THAT CAN SERVE SEVERAL DROPDOWNS.

   WHY A GROUP IS BETTER HERE THAN ONE BUTTON EACH. Two settings that are really one decision - what
   the take contains and what it is saved as - otherwise mean two Apply buttons a Tab apart, with
   nothing after arrowing a dropdown to say a press is still needed. One button named for the pair
   is fewer stops, and it is the thing you reach next either way.

   It is available when ANY of its dropdowns is staged, and pressing it applies only the ones that
   actually changed. A single-dropdown group is the same code path, so nothing else needed special
   casing. */
function makeStagedApply(id, text, onApply) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "secondary";
  btn.id = id;
  btn.textContent = text;
  btn.stagedSelects = [];
  btn.addEventListener("click", () => {
    const blocked = btn.dataset.blockedReason || "";
    if (blocked) { announce(blocked, true); return; }
    const pending = btn.stagedSelects.filter(isStaged);
    if (!pending.length) {
      announce("Nothing to apply: that is what the unit already has.", true);
      if (btn.stagedSelects[0]) btn.stagedSelects[0].focus();
      return;
    }
    if (isBusy(btn)) return;
    onApply(pending, btn);
  });
  return btn;
}

function makeStagedSelect(id, labelText, options, opts) {
  opts = opts || {};
  const row = document.createElement("div");
  row.className = opts.className || "field";
  const label = document.createElement("label");
  label.setAttribute("for", id);
  label.textContent = labelText;
  const sel = document.createElement("select");
  sel.id = id;
  (options || []).forEach((opt) => {
    const o = document.createElement("option");
    o.value = String(opt[0]);
    o.textContent = opt[1];
    sel.appendChild(o);
  });
  const hint = document.createElement("span");
  hint.className = "hint";
  hint.id = id + "-now";
  // NOT aria-describedby. A description is re-read as the value changes, so the staged warning
  // would be spoken between every option: the same noise the tablist name and the spin-button
  // hints are kept off for. The text stays ON THE PAGE, and the Apply button is what
  // tells you there is something waiting: it is unavailable until there is.

  // ONE BUTTON OF ITS OWN, or a SHARED one passed in by the caller. Both are the same object with
  // the same rules; a lone dropdown is simply a group of one, so there is no second code path.
  const btn = opts.applyButton
    || makeStagedApply(id + "-apply", opts.applyText || "Apply",
                       (pending, b) => opts.onApply(pending[0], b));
  btn.stagedSelects.push(sel);
  sel.addEventListener("change", () => markStaged(sel, btn));
  // THE BUTTON IS REMEMBERED ON THE SELECT, not looked up by id. setStagedValue is called at BUILD
  // time by some callers (mic type), when the row is not in the document yet and getElementById
  // would return null - which left that Apply button un-marked and armed with nothing staged.
  sel.stagedApply = btn;
  row.appendChild(label);
  row.appendChild(sel);
  row.appendChild(hint);
  // A SHARED button is placed by the caller, once, after the last dropdown it serves. Only a
  // button this row owns goes in this row.
  if (!opts.applyButton) row.appendChild(btn);
  markStaged(sel, btn);
  return { row, sel, btn, hint };
}

/* Recompute one staged dropdown's Apply button and its hint. Safe to call at any time. */
function markStaged(sel, btn) {
  if (!sel || !btn) return;
  // THE BUTTON IS ABOUT THE WHOLE GROUP, THE HINT IS ABOUT THIS DROPDOWN. With a shared Apply,
  // deciding the button from `sel` alone would switch it off whenever the dropdown you just moved
  // happens to match the unit, even though the other one is still waiting.
  const group = (btn.stagedSelects && btn.stagedSelects.length) ? btn.stagedSelects : [sel];
  const blocked = btn.dataset.blockedReason || "";
  setAvailable(btn, group.some(isStaged) && !blocked,
    blocked || "That is what the unit already has. Choose something else first.");
  setText(sel.id + "-now",
    (isStaged(sel) && sel.dataset.deviceLabel)
      ? "Not applied yet. The unit still has " + sel.dataset.deviceLabel + "."
      : "");
}

/* WHAT THE UNIT SAYS, written into a staged dropdown: the value, the label to quote back while a
   change is pending, and the button's state. THE ONLY place deviceValue is set, so a staged choice
   can never be mistaken for what the desk is doing. */
function setStagedValue(sel, value, label) {
  if (!sel) return;
  if (value === null || value === undefined) return;
  if (!Array.from(sel.options).some((o) => o.value === String(value))) {
    const o = document.createElement("option");
    o.value = String(value);
    o.textContent = label || ("Value " + value);
    sel.appendChild(o);
  }
  sel.value = String(value);
  sel.dataset.deviceValue = String(value);
  const chosen = sel.options[sel.selectedIndex];
  sel.dataset.deviceLabel = label || (chosen && chosen.textContent) || "";
  markStaged(sel, sel.stagedApply || document.getElementById(sel.id + "-apply"));
}

/* Mark a staged dropdown's Apply as blocked for a REASON (a read-only device, say), which survives
   every later markStaged. Pass "" to unblock. */
function blockStaged(btn, reason) {
  if (!btn) return;
  if (reason) btn.dataset.blockedReason = reason;
  else delete btn.dataset.blockedReason;
}

function makeSelectRow(id, labelText, options, onChange) {
  const row = document.createElement("div");
  row.className = "field";
  const label = document.createElement("label");
  label.setAttribute("for", id);
  label.textContent = labelText;
  const sel = document.createElement("select");
  sel.id = id;
  options.forEach((opt) => {
    const o = document.createElement("option");
    o.value = String(opt[0]);
    o.textContent = opt[1];
    sel.appendChild(o);
  });
  let commit = null;
  sel.addEventListener("change", () => {
    if (commit !== null) window.clearTimeout(commit);
    commit = window.setTimeout(() => { commit = null; onChange(sel); }, SELECT_COMMIT_MS);
  });
  row.appendChild(label);
  row.appendChild(sel);
  return { row, sel };
}

function makeSwitchRow(id, labelText, onChange) {
  const row = document.createElement("p");
  row.className = "field";
  const label = document.createElement("label");
  label.className = "switch";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.id = id;
  input.addEventListener("change", () => onChange(input));
  const span = document.createElement("span");
  span.textContent = labelText;
  label.appendChild(input);
  label.appendChild(span);
  row.appendChild(label);
  return { row, input, span };
}

function clampNum(v, lo, hi) {
  const n = parseFloat(v);
  if (isNaN(n)) return lo;
  return Math.max(lo, Math.min(hi, n));
}

// A labelled native number input that commits on change (not per keystroke), which is
// the most reliable pattern across screen readers.
function makeNumberRow(id, labelText, opts, onCommit) {
  const row = document.createElement("div");
  row.className = "field";
  const label = document.createElement("label");
  label.setAttribute("for", id);
  label.textContent = labelText;
  const input = document.createElement("input");
  input.type = "number";
  input.id = id;
  input.min = String(opts.min);
  input.max = String(opts.max);
  input.step = String(opts.step);
  input.addEventListener("change", () => onCommit(input));
  row.appendChild(label);
  row.appendChild(input);
  return { row, input };
}

async function routingPost(url, body, control) {
  // WHERE THE WHOLE PAGE REFUSES. Every routing write goes through here, so one guard covers the
  // switches, the number boxes, the staged dropdowns and the matrix, and a control added later is
  // covered the moment it uses this function - which a list of per-handler guards cannot promise.
  // aria-disabled leaves the control operable, so the press really does arrive; putting the page
  // back from the last read is what stops a flipped switch reporting a state the unit is not in.
  const no = unavailableReason(control);
  if (no !== null) {
    announce(no, true);
    if (_routingValues) applyRoutingValues(_routingValues);
    return { ok: false, message: no, blocked: true };
  }
  setBusy(control, true);   // aria-disabled, not disabled: keeps focus on the control you changed
  try {
    const res = await postJSON(url, body);
    announce(res.message || "Done.", !res.ok);
    return res;
  } catch (err) {
    announce("Could not reach the device service.", true);
    return { ok: false };
  } finally {
    setBusy(control, false);
  }
}

/* Build the routing page's controls. STRUCTURE ONLY plus the values this one model carries: the
   per-tab live reads (the levels, the monitor mix, the output-mix matrix) are fired by the tab that
   needs them, so nothing is read for a tab nobody has opened. See readRouting. */
async function loadRouting() {
  let data;
  try {
    const resp = await fetch("/api/routing");
    data = await resp.json();
  } catch (err) {
    setText("routing-note", "Could not load the routing controls.");
    return;
  }
  routingModel = data;
  setText("routing-note", data.note || "");

  // Fader assignment: one dropdown per fader (1 to 9), each picking which channel
  // sits on that fader. The fader layout IS the channelInputSource vector.
  const inBox = document.getElementById("routing-inputs");
  inBox.innerHTML = "";
  routingCtl.faders = {};
  const fm = data.fader_model || { faders: [], channels: [] };
  const chanOpts = (fm.channels || []).map((c) => [c[0], c[1]]);
  (fm.faders || []).forEach((f) => {
    // ONE APPLY PER FADER, not one for the page: a swap moves TWO faders at once, so staging
    // several and applying them together would compound into something nobody could predict from
    // the controls. The button names its fader, or nine of them read as "Apply" nine times.
    const made = makeStagedSelect("routing-fader-" + f.index, f.label, chanOpts, {
      applyText: "Apply to " + f.label.replace(/\s*\(.*$/, ""),
      onApply: (sel, btn) => onFaderApply(f.index, parseInt(sel.value, 10), sel, btn),
    });
    routingCtl.faders[f.index] = made.sel;
    inBox.appendChild(made.row);
  });
  // Fader writes are gated per-device (server "fader_write" cap): the Pro II is writable; the Duo
  // is read-only until its fader write model + default layout are confirmed. When read-only, disable
  // the dropdowns and the reset button and add a clear note so nothing is a silent no-op.
  routingCtl.fadersReadOnly = !!fm.read_only;
  if (fm.read_only) {
    // aria-disabled on the dropdowns too. `disabled` takes each one out of the tab order, which
    // flatly contradicts the note below: you cannot in fact see which channel is on each fader,
    // because there is nothing left to arrow onto. Nothing can be
    // written by accident - the Apply button is the write, and it is blocked with the same reason -
    // so a staged choice here is a question the page answers rather than a silent no-op.
    Object.values(routingCtl.faders).forEach((sel) => {
      if (!sel) return;
      setAvailable(sel, false, FADERS_READ_ONLY_OFF);
      const b = sel.stagedApply || document.getElementById(sel.id + "-apply");
      blockStaged(b, FADERS_READ_ONLY_OFF);
      markStaged(sel, b);
    });
    setAvailable(document.getElementById("faders-reset-btn"), false, FADERS_READ_ONLY_OFF);
    const p = document.createElement("p");
    p.className = "note"; p.setAttribute("role", "note");
    p.textContent = "Changing the fader layout is not enabled on this device yet, so the faders "
      + "are read-only here. You can still see which channel is on each fader.";
    inBox.insertBefore(p, inBox.firstChild);
  }

  buildStereoLinks(fm);

  // Multitrack is NOT built here. It decides what a recording CONTAINS, so it belongs on the
  // Recording page: two places called Recording is one too many to tell apart by ear. Only the UI
  // sits there; the endpoint is still Routing's.
  const amBox = document.getElementById("routing-automute");
  amBox.innerHTML = "";
  // THE SWITCH FOLLOWS THE DESK, not the click. The server reads the setting back and returns it,
  // so a change the unit did not take puts the switch back to what the unit really has rather than
  // leaving it claiming a setting that is not set. The failure this prevents is a box reading ON,
  // a desk reading off, an auto-mute nobody has, and feedback through the speakers.
  const autoMute = async (which, input) => {
    const res = await routingPost("/api/routing/automute",
                                  { which, on: input.checked }, input);
    if (res && typeof res.on === "boolean") input.checked = res.on;
    else if (!res || res.ok === false) input.checked = !input.checked;
  };
  const mon = makeSwitchRow("routing-mon-mute", "Monitor auto-mute",
    (i) => autoMute("monitor", i));
  const bt = makeSwitchRow("routing-bt-mute", "Bluetooth auto-mute",
    (i) => autoMute("bt", i));
  routingCtl.monitorMute = mon.input;
  routingCtl.btMute = bt.input;
  amBox.appendChild(mon.row);
  amBox.appendChild(bt.row);

  // USB AUDIO, on its own tab rather than on the Recording page: it is an output, not a take.
  // Structure only; the values arrive with the live read, like the rest of this tab set.
  //
  // THE WARNING IS THE POINT OF THE HINT, not decoration. Changing the mode makes the desk tear
  // down and re-advertise its whole USB audio function, so every RODECaster endpoint on the machine
  // drops for a second or two and Windows can move the default device. Someone monitoring through
  // the unit hears their audio stop, and a screen reader routed through it goes silent - the one
  // thing this app must never do without saying so first. Said again in the write's reply.
  const usbBox = document.getElementById("routing-usb");
  if (usbBox) {
    usbBox.innerHTML = "";
    const modes = data.usb_multitrack_modes || [];
    // STAGED behind Apply: this one does more than change the audio, it tears the USB audio
    // function down and rebuilds it, so an accidental arrow press silences a screen reader routed
    // through the unit. See the hint below, which says the same thing before you press.
    // RODE's own word is in the label because their guides, videos and menus all say "Multitrack";
    // a label that only describes leaves somebody following those instructions nothing to match.
    const um = makeStagedSelect("routing-usb-mode", "What this computer receives (Multitrack)",
      modes, {
      applyText: "Apply what this computer receives",
      onApply: async (sel, btn) => {
        const res = await routingPost("/api/routing/usb-multitrack",
                                      { mode: parseInt(sel.value, 10) }, btn);
        if (res.ok) setStagedValue(sel, parseInt(sel.value, 10), null);
      },
    });
    const umHint = document.createElement("span");
    umHint.className = "hint";
    umHint.id = "routing-usb-mode-hint";
    umHint.textContent = "Changing this disconnects and reconnects the unit's audio devices for a "
      + "couple of seconds, so anything playing through the unit, including a screen reader, goes "
      + "quiet while it happens.";
    um.sel.setAttribute("aria-describedby", "routing-usb-mode-hint");
    um.row.appendChild(umHint);
    routingCtl.usbMode = um.sel;
    usbBox.appendChild(um.row);

    // EXPANDED, the INPUT direction of the same setting. Staged for the same reason as the mode
    // above and one more: turning it OFF takes every extended input off its fader (the server
    // does it, as the unit's own screen does), and that layout does not come back by turning it
    // on again. A select rather than a
    // switch because the two states need naming - "off" does not say what you lose.
    const exp = makeStagedSelect("routing-usb-expanded", "What this computer sends (USB 1 Input)",
      data.usb_expanded_labels || [], {
        applyText: "Apply what this computer sends",
        onApply: async (sel, btn) => {
          const res = await routingPost("/api/routing/usb-expanded",
                                        { on: sel.value === "true" }, btn);
          if (res.ok) setStagedValue(sel, sel.value, null);
        },
      });
    const expHint = document.createElement("span");
    expHint.className = "hint";
    expHint.id = "routing-usb-expanded-hint";
    expHint.textContent = data.usb_expanded_hint || "";
    exp.sel.setAttribute("aria-describedby", "routing-usb-expanded-hint");
    exp.row.appendChild(expHint);
    routingCtl.usbExpanded = exp.sel;
    usbBox.appendChild(exp.row);
    // THE DRIVER'S SIDE OF EXPANDED, read from Windows rather than the unit: installed, running,
    // connected, and how many of its virtual devices are switched on. Without it a desk in
    // Expanded with no driver looks identical to one that is working, and the four inputs are
    // silent for a reason nothing on the page names. Live text, so a mode change updates it.
    const drv = document.createElement("p");
    drv.className = "hint";
    drv.id = "routing-usb-driver";
    drv.setAttribute("aria-live", "polite");
    drv.textContent = (data.usb_driver && data.usb_driver.text) || "";
    routingCtl.usbDriver = drv;
    usbBox.appendChild(drv);

    // GETTING THE DRIVER, offered only while it is MISSING. Expanded needs it and the line above
    // has just said so, which is the whole argument for the buttons being here rather than in the
    // installer: this is where the shortfall is named. Two routes, and the manual one is a button
    // in its own right rather than something that only appears after a failure - nobody should
    // have to make something go wrong to find the way round it.
    // GETTING THE DRIVER. BUILT HERE, LABELLED FROM THE LIVE READ, because `usb_driver` is not in
    // the structure payload at all - it is read from Windows, not from the desk, and arrives with
    // the values. Deciding here whether to create these made them never appear.
    const getBox = document.createElement("div");
    getBox.className = "row";
    const get = document.createElement("button");
    get.type = "button";
    get.id = "routing-driver-install";
    get.textContent = "Download and install RODE's Virtual Devices driver";
    get.setAttribute("aria-describedby", "routing-driver-hint");
    get.addEventListener("click", installDriver);
    get.hidden = true;                  // shown only when the driver is missing; see applyDriverButtons
    const page = document.createElement("button");
    page.type = "button";
    page.id = "routing-driver-page";
    page.textContent = "Open RODE's software page";
    page.setAttribute("aria-describedby", "routing-driver-hint");
    page.addEventListener("click", async () => {
      const res = await postJSON("/api/driver/page", {});
      announce(res.message || "", !res.ok);
    });
    const dhint = document.createElement("span");
    dhint.className = "hint";
    dhint.id = "routing-driver-hint";
    getBox.appendChild(get);
    getBox.appendChild(page);
    getBox.appendChild(dhint);
    routingCtl.driverInstall = get;
    routingCtl.driverPage = page;
    routingCtl.driverHint = dhint;
    usbBox.appendChild(getBox);

    const ub = makeSwitchRow("routing-usb-bypass", "Send the separate tracks raw", (input) => {
      sendBypass("/api/routing/usb-bypass", input, (msg) => announce(msg, false));
    });
    const ubHint = document.createElement("span");
    ubHint.className = "hint";
    ubHint.id = "routing-usb-bypass-hint";
    ub.input.setAttribute("aria-describedby", "routing-usb-bypass-hint");
    ub.row.appendChild(ubHint);
    routingCtl.usbBypass = ub.input;
    routingCtl.usbBypassHint = ubHint;
    usbBox.appendChild(ub.row);
  }

  // Headphone type: one select per addressable headphone output, both the outputs and the amp-type
  // value list come from the device model (GAP-6), so the Duo/Pro II expose the right controls.
  const hpBox = document.getElementById("routing-headphones");
  hpBox.innerHTML = "";
  routingCtl.headphones = [];
  const hpData = data.headphones || { outputs: [], values: [] };
  hpData.outputs.forEach((out) => {
    const hp = makeStagedSelect("routing-hp-type-" + out.index, out.label + " type",
      hpData.values, {
        applyText: "Apply " + out.label.toLowerCase() + " type",
        onApply: async (sel, btn) => {
          const res = await routingPost("/api/routing/headphone",
            { value: parseInt(sel.value, 10), output: out.index }, btn);
          if (res.ok) setStagedValue(sel, parseInt(sel.value, 10), null);
        },
      });
    routingCtl.headphones[out.index] = hp.sel;
    hpBox.appendChild(hp.row);
  });

  // Master output processing: Compellor (on/off + 4 params) and Output delay (on/off + ms).
  const masterBox = document.getElementById("routing-master");
  masterBox.innerHTML = "";
  const comp = makeSwitchRow("routing-compellor-on", "Master Compellor",
    (i) => routingPost("/api/routing/compellor", { on: i.checked }, i));
  routingCtl.compellorOn = comp.input;
  masterBox.appendChild(comp.row);

  routingCtl.compellor = {};
  [["threshold", "Compellor threshold"], ["attack", "Compellor attack"],
   ["release", "Compellor release"], ["gain", "Compellor make-up gain"]].forEach(([key, lab]) => {
    const made = makeNumberRow("routing-compellor-" + key, lab + " (percent)",
      { min: 0, max: 100, step: 1 },
      (inp) => routingPost("/api/routing/compellor-param",
        { param: key, value: clampNum(inp.value, 0, 100) / 100 }, inp));
    routingCtl.compellor[key] = made.input;
    masterBox.appendChild(made.row);
  });

  const dly = makeSwitchRow("routing-delay-on", "Output delay",
    (i) => routingPost("/api/routing/delay",
      { on: i.checked, ms: routingCtl.delayMs ? clampNum(routingCtl.delayMs.value, 0, 2000) : 0 }, i));
  routingCtl.delayOn = dly.input;
  masterBox.appendChild(dly.row);

  const dms = makeNumberRow("routing-delay-ms", "Output delay (milliseconds)",
    { min: 0, max: 2000, step: 10 },
    (inp) => routingPost("/api/routing/delay",
      { on: routingCtl.delayOn ? routingCtl.delayOn.checked : true,
        ms: clampNum(inp.value, 0, 2000) }, inp));
  routingCtl.delayMs = dms.input;
  masterBox.appendChild(dms.row);

  // Master/global output writes (multitrack, auto-mute, headphone type, Compellor, delay) are
  // gated per-device: verified on the Pro II, but read-only on a desk whose selectors are not
  // captured. When read-only they are marked unavailable IN PLACE - aria-disabled, never the
  // attribute - so a screen-reader user still meets every control where
  // they expect it and hears why it cannot be moved, instead of finding four sections that are
  // simply shorter than they are on the other desk. routingPost refuses the write itself.
  if (data.output_write_read_only) {
    [routingCtl.monitorMute, routingCtl.btMute, routingCtl.usbMode, routingCtl.usbBypass,
     routingCtl.compellorOn, routingCtl.delayOn, routingCtl.delayMs]
      .concat(routingCtl.headphones || [])
      .concat(Object.values(routingCtl.compellor || {}))
      .forEach((el) => setAvailable(el, false, OUTPUTS_READ_ONLY_OFF));
    // The staged dropdowns' Apply buttons carry the same reason, so pressing one says why.
    [routingCtl.usbMode].concat(routingCtl.headphones || []).forEach((sel) => {
      if (!sel) return;
      const b = sel.stagedApply || document.getElementById(sel.id + "-apply");
      blockStaged(b, OUTPUTS_READ_ONLY_OFF);
      markStaged(sel, b);
    });
    const roMsg = "Changing this is not enabled on this device yet, so it is read-only here.";
    ["routing-automute", "routing-master", "routing-headphones", "routing-usb"]
      .forEach((id) => {
        const box = document.getElementById(id);
        if (!box) return;
        const p = document.createElement("p");
        p.className = "note"; p.setAttribute("role", "note"); p.textContent = roMsg;
        box.insertBefore(p, box.firstChild);
      });
  }

  buildMatrix(data.matrix);
  buildMonitorMix(data.monitor_mix);
}

/* ---- Monitor mix levels (per-output source levels) ------------------- */

let _monmixModel = null;
const _monmixDebounce = {};

function buildMonitorMix(model) {
  if (!model) return;
  _monmixModel = model;
  setText("monmix-note", model.note || "");
  // No writes on a read-only device, but the button stays where it is and says so when pressed:
  // a button that vanishes from the tab order is indistinguishable from one that was never there.
  setAvailable(document.getElementById("monmix-reset-btn"), !model.read_only, MONMIX_READ_ONLY_OFF);
  const sel = document.getElementById("monmix-output");
  if (sel && !sel.options.length) {
    (model.outputs || []).forEach((o) => {
      const opt = document.createElement("option");
      opt.value = String(o.index); opt.textContent = o.label;
      sel.appendChild(opt);
    });
  }
  // The sliders themselves are NOT rendered here: they are a live device read, and the Levels tab
  // fires it when it is opened (readRoutingTab), so the values are never the stale ones from
  // whenever this page happened to be built.
}

async function loadMonmixOutput() {
  const sel = document.getElementById("monmix-output");
  const host = document.getElementById("monmix-sliders");
  if (!sel || !host || !_monmixModel) return;
  const oi = sel.value;
  setText("monmix-mode", "Reading...");
  let data;
  try {
    const resp = await fetch("/api/monitormix/live");
    data = await resp.json();
  } catch (e) {
    setText("monmix-mode", ""); setStatus("monmix-status", "Could not read the mix levels.");
    markLevelsUnread(host); return;
  }
  if (!data.ok) {
    setText("monmix-mode", ""); setStatus("monmix-status", data.message || "Mix levels unavailable.");
    markLevelsUnread(host); return;
  }
  const out = (data.outputs || {})[oi];
  if (!out) { markLevelsUnread(host); return; }
  const modeNote = out.mode === 2 ? "This output is a custom mix."
    : out.mode === 1 ? "This output is a mix-minus. Adjusting a level switches it to a custom mix."
    : "Following the faders. Adjusting a level switches it to a custom mix.";
  setText("monmix-mode", "(" + (out.mode_label || "") + ") " + modeNote);
  // Structure once, values every time: this runs every time the Levels tab is opened, so the
  // sliders are only rebuilt when the OUTPUT or the source list really changes (a deliberate
  // context change), never merely because the levels were read again.
  const sources = _monmixModel.sources || [];
  const key = JSON.stringify([oi, !!(_monmixModel && _monmixModel.read_only),
    sources.map((s) => [s.index, s.label])]);
  rebuildIfChanged(host, key, () => {
    sources.forEach((src) => { host.appendChild(buildMonmixSlider(oi, src)); });
  });
  sources.forEach((src) => {
    const cell = (out.sources || {})[String(src.index)] || {};
    setLevelValue("monmix-" + oi + "-" + src.index, cell.percent);
  });
}

/* ---- Virtual fader levels + monitor speaker level -------------------------
   The two levels the desk otherwise keeps to itself. The virtual faders (7-9) have no physical
   fader, and the monitor level has no control in RODE's own app, so both were reachable only from
   the unit - encoder or touchscreen - and the encoder cannot be used without sight (its target is
   chosen by touch and it silently unbinds). Writes are proven on hardware; see
   RODECASTER_PROTOCOL.txt 6.1. */
const _levelDebounce = {};

async function loadFaderLevels(announceResult) {
  const host = document.getElementById("fader-levels");
  if (!host) return;
  try {
    const resp = await fetch("/api/faders/levels");
    const d = await resp.json();
    if (!d.ok) {
      setStatus("fader-levels-status", d.message || "Could not read the levels.");
      markLevelsUnread(host);
      markLevelsUnread(document.getElementById("routing-outmutes"));
      return;
    }
    // Structure once, values every time. The Levels tab re-reads the device every time it is
    // opened, so rebuilding these sliders here would replace them under a screen reader that has
    // just arrived on them. They are rebuilt only if the device reports a different set of faders.
    const faders = d.faders || [];
    const labelOf = (f) => (f.channel ? `${f.label}: ${f.channel}` : f.label);
    const key = JSON.stringify([!!d.read_only, faders.map((f) => [f.index, labelOf(f)])]);
    rebuildIfChanged(host, key, () => {
      faders.forEach((f) => {
        host.appendChild(buildLevelSlider("fader-level-" + f.index, labelOf(f), d.read_only,
          (pct) => postJSON("/api/faders/level", { fader: f.index, percent: pct })));
        // MUTE BESIDE THE LEVEL, and only here. A virtual fader's MUTE and SOLO have no control on
        // the unit at all, so software is the only way to reach them; keeping them on two pages
        // made the strip the one thing you could not operate from one place. (Its LEVEL is a
        // different case: the encoder cycles onto these, so that half does have hardware.) Named
        // with the channel, because a virtual fader only means whatever is on it right now.
        const mute = makeSwitchRow("fader-mute-" + f.index, "Mute " + labelOf(f),
                                   (input) => onFaderMute(f.index, input));
        host.appendChild(mute.row);
        // Solo belongs here for the same reason, and it is the control that most needs to sit next
        // to the level: isolating a channel is how you set its level by ear. The danger a solo
        // carries (headphones on one channel, rest of the desk apparently dead) is answered by the
        // reply naming everything soloed and by the Clear all solos button below.
        const solo = makeSwitchRow("fader-solo-" + f.index, "Solo " + labelOf(f),
                                   (input) => onFaderSolo(f.index, input));
        host.appendChild(solo.row);
      });
      host.appendChild(buildLevelSlider("monitor-level", "Monitor speaker", d.read_only,
        (pct) => postJSON("/api/monitor/level", { percent: pct })));
    });
    faders.forEach((f) => {
      setLevelValue("fader-level-" + f.index, f.percent);
      // muted/soloed === null means the fader is empty, so there is nothing to act on. Say that
      // rather than offer a switch that would only refuse.
      const empty = (v) => v === null || v === undefined;
      // NOT d.read_only: that is the LEVEL gate, and a device can have unverified fader levels
      // while its mute and solo are fine. Using it here would take a working control away and give
      // the wrong reason for it.
      const off = d.toggles_read_only;
      const m = document.getElementById("fader-mute-" + f.index);
      if (m) {
        m.checked = !!f.muted;
        setAvailable(m, !empty(f.muted) && !off,
                     off ? (d.toggles_message || "Muting is not enabled on this device yet.")
                         : "This fader has no channel on it, so there is nothing to mute.");
      }
      const s = document.getElementById("fader-solo-" + f.index);
      if (s) {
        s.checked = !!f.soloed;
        setAvailable(s, !empty(f.soloed) && !off,
                     off ? (d.toggles_message || "Solo is not enabled on this device yet.")
                         : "This fader has no channel on it, so there is nothing to solo.");
      }
    });
    setLevelValue("monitor-level", (d.monitor || {}).percent);
    // The way out of a solo follows the same gate as the solos themselves, so the tab never offers
    // an escape it cannot perform.
    setAvailable(document.getElementById("fader-solos-clear"), !d.toggles_read_only,
                 d.toggles_message || "Solo is not enabled on this device yet.");
    fillOutputMutes(d);
    // ONE VOICE FOR THIS TAB, which is why the arrival message goes to the status line rather
    // than to announce(): on a read-only desk the line above also has something to say, and two
    // polite regions changing together is the fault this section exists to have fixed.
    if (d.read_only && d.message) setStatus("fader-levels-status", d.message);
    else if (announceResult) setStatus("fader-levels-status", "Levels read from the device.");
  } catch (err) {
    setStatus("fader-levels-status", "Could not reach the app service.");
    markLevelsUnread(host);
    markLevelsUnread(document.getElementById("routing-outmutes"));
  }
}

/* THE SWITCH FOLLOWS THE DESK, not the click: on a refusal it goes back to what the unit really
   has, the same rule the auto-mute and stereo-link switches follow. The server answers with the
   mute state it ended up with, so there is one authority. */
async function onFaderMute(index, input) {
  const no = unavailableReason(input);
  if (no !== null) {
    input.checked = !input.checked;
    setStatus("fader-levels-status", no);
    return;
  }
  const wanted = input.checked;
  let res;
  try {
    res = await postJSON("/api/faders/mute", { fader: index, on: wanted });
  } catch (e) {
    res = { ok: false, message: "Could not reach the device service." };
  }
  if (typeof res.muted === "boolean") input.checked = res.muted;
  else if (!res.ok) input.checked = !wanted;
  setStatus("fader-levels-status", res.message || "Done.");
}

/* Same contract as onFaderMute. The message is longer on purpose: it names everything the
   headphones now carry, which is the difference between a solo you can undo and one you cannot
   find. */
async function onFaderSolo(index, input) {
  const no = unavailableReason(input);
  if (no !== null) {
    input.checked = !input.checked;
    setStatus("fader-levels-status", no);
    return;
  }
  const wanted = input.checked;
  let res;
  try {
    res = await postJSON("/api/faders/solo", { fader: index, on: wanted });
  } catch (e) {
    res = { ok: false, message: "Could not reach the device service." };
  }
  if (typeof res.soloed === "boolean") input.checked = res.soloed;
  else if (!res.ok) input.checked = !wanted;
  setStatus("fader-levels-status", res.message || "Done.");
}

async function onClearFaderSolos() {
  const btn = document.getElementById("fader-solos-clear");
  const no = unavailableReason(btn);
  if (no !== null) { setStatus("fader-levels-status", no); return; }
  setBusy(btn, true);
  let res;
  try {
    res = await postJSON("/api/channels/solo/clear", {});
  } catch (e) {
    res = { ok: false, message: "Could not reach the device service." };
  }
  setStatus("fader-levels-status", res.message || "Done.");
  setBusy(btn, false);
  await loadFaderLevels(false);      // every solo switch on the tab has just changed
}

/* The two output MUTES and the Bluetooth OUTPUT level, filled from the same read as the sliders
   above (outputMonMute / outputBTMute / outputBTLevel, all proven writable).

   Same rule as everything else on this tab: the tab RE-READS the device every time it is opened,
   so this must never rebuild controls a screen
   reader may be sitting in. Structure once, keyed on read_only (the only thing that changes their
   shape), then values in place. */
function fillOutputMutes(d) {
  const host = document.getElementById("routing-outmutes");
  if (!host) return;
  rebuildIfChanged(host, JSON.stringify(["outmutes", !!d.read_only]), () => {
    host.appendChild(buildSwitch("out-mute-monitor", "Mute the monitor speakers", false,
      (on, input) => sendToggle("/api/output/mute", { which: "monitor", on }, input, on)));
    host.appendChild(buildSwitch("out-mute-bluetooth", "Mute the Bluetooth output", false,
      (on, input) => sendToggle("/api/output/mute", { which: "bluetooth", on }, input, on)));
    host.appendChild(buildLevelSlider("bt-out-level", "Bluetooth output level", d.read_only,
      (pct) => postJSON("/api/output/bt-level", { percent: pct })));
  });
  const mon = document.getElementById("out-mute-monitor");
  const bt = document.getElementById("out-mute-bluetooth");
  // A mute the unit did not report stays as it was rather than silently reading "off": showing an
  // unmuted switch for a state we do not know is the one thing that would be worse than no control.
  if (mon && (d.monitor || {}).muted !== null && (d.monitor || {}).muted !== undefined) {
    mon.checked = !!d.monitor.muted;
  }
  const bto = d.bluetooth_out || {};
  if (bt && bto.muted !== null && bto.muted !== undefined) bt.checked = !!bto.muted;
  setLevelValue("bt-out-level", bto.percent);
}

/* A read failed, and the sliders are no longer thrown away when it does (they stay so a screen
   reader keeps the node it is on). So say what they are: each one goes back to "not read yet"
   rather than leaving an old number sitting there looking live. */
function markLevelsUnread(host) {
  if (!host) return;
  host.querySelectorAll("input[type=range]").forEach((input) => {
    delete input.dataset.known;
    delete input.dataset.touched;
    showLevelValue(input);
  });
}

/* Show a level slider's own value on itself: aria-valuetext for the screen reader, the sighted
   span beside it (aria-hidden, so it never speaks on top of the slider). Whether the value is real
   lives on the ELEMENT (dataset.known), not in a build-time closure, because a re-read updates the
   value in place and a closure would go stale - the same trap as the Device page's dataset.prev. */
function showLevelValue(input) {
  const known = input.dataset.known === "1" || input.dataset.touched === "1";
  input.setAttribute("aria-valuetext", known ? input.value + " percent" : "not read yet");
  const out = input.parentNode && input.parentNode.querySelector(".fx-ctl-value");
  if (out) out.textContent = input.value + "%";
}

/* Put a freshly-read percent onto an existing slider. A null percent (nothing reported) leaves the
   slider alone rather than pretending the device said zero. */
function setLevelValue(id, percent) {
  const input = document.getElementById(id);
  if (!input) return;
  if (percent !== null && percent !== undefined) {
    input.dataset.known = "1";
    input.value = String(percent);
    input.dataset.deviceValue = String(percent);   // where a refused slider goes back to
  }
  showLevelValue(input);
}

/* One labelled 0-100 slider that writes on change, debounced. `send` returns the POST promise.
   Built with no value: the value arrives through setLevelValue, so a re-read updates the slider
   instead of replacing it. */
function buildLevelSlider(id, labelText, readOnly, send) {
  const row = document.createElement("div");
  row.className = "field fx-ctl";
  const label = document.createElement("label");
  label.setAttribute("for", id);
  label.textContent = labelText;
  const input = document.createElement("input");
  input.type = "range";
  input.id = id;
  input.min = "0";
  input.max = "100";
  input.step = "1";
  input.value = "0";
  const out = document.createElement("span");
  out.className = "fx-ctl-value";
  out.setAttribute("aria-hidden", "true");
  if (readOnly) {
    // Device whose level writes are not verified: show the live value, do not offer to change it.
    // aria-disabled, so the slider keeps its place and its label and can still be READ; moving it
    // puts it straight back to the unit's own number and says why, once for a run of arrow keys.
    setAvailable(input, false, MONMIX_READ_ONLY_OFF);
    input.addEventListener("input", () => {
      if (refuseChange(input) === null) return;
      if (input.dataset.deviceValue !== undefined) input.value = input.dataset.deviceValue;
      showLevelValue(input);
    });
  } else {
    input.addEventListener("input", () => {
      input.dataset.touched = "1";
      showLevelValue(input);
      clearTimeout(_levelDebounce[id]);
      _levelDebounce[id] = setTimeout(async () => {
        try {
          const res = await send(parseInt(input.value, 10));
          setStatus("fader-levels-status", res.message || "Done.");
        } catch (e) {
          setStatus("fader-levels-status", "Could not reach the device service.");
        }
      }, WRITE_DEBOUNCE_MS);
    });
  }
  row.appendChild(label);
  row.appendChild(input);
  row.appendChild(out);
  showLevelValue(input);
  return row;
}

function buildMonmixSlider(outputIndex, src) {
  const row = document.createElement("div");
  row.className = "field fx-ctl";
  const id = "monmix-" + outputIndex + "-" + src.index;
  const label = document.createElement("label");
  label.setAttribute("for", id);
  label.textContent = src.label + " level";
  const input = document.createElement("input");
  input.type = "range"; input.id = id; input.min = "0"; input.max = "100"; input.step = "1";
  input.value = "70";
  const out = document.createElement("span");
  out.className = "fx-ctl-value"; out.setAttribute("aria-hidden", "true");
  const readOnly = _monmixModel && _monmixModel.read_only;
  if (readOnly) {
    // Read-only device: show the live level, and keep the slider readable and in the tab order
    // rather than dropping it out with the attribute. Moving it goes straight back and says why.
    setAvailable(input, false, MONMIX_READ_ONLY_OFF);
    input.addEventListener("input", () => {
      if (refuseChange(input) === null) return;
      if (input.dataset.deviceValue !== undefined) input.value = input.dataset.deviceValue;
      showLevelValue(input);
    });
    row.appendChild(label); row.appendChild(input); row.appendChild(out);
    showLevelValue(input);
    return row;
  }
  input.addEventListener("input", () => {
    input.dataset.touched = "1"; showLevelValue(input);
    clearTimeout(_monmixDebounce[id]);
    _monmixDebounce[id] = setTimeout(async () => {
      try {
        const res = await postJSON("/api/monitormix/set",
          { output: parseInt(outputIndex, 10), source: src.index, value: parseInt(input.value, 10) / 100 });
        setStatus("monmix-status", res.message || "Could not set the level.");
      } catch (e) { setStatus("monmix-status", "Could not reach the device service."); }
    }, WRITE_DEBOUNCE_MS);
  });
  row.appendChild(label); row.appendChild(input); row.appendChild(out);
  showLevelValue(input);
  return row;
}

async function onMonmixReset() {
  const sel = document.getElementById("monmix-output");
  const btn = document.getElementById("monmix-reset-btn");
  if (!sel) return;
  const no = unavailableReason(btn);
  if (no !== null) { setStatus("monmix-status", no); return; }
  setBusy(btn, true);
  setStatus("monmix-status", "Resetting, please wait.");
  try {
    const res = await postJSON("/api/monitormix/reset", { output: parseInt(sel.value, 10) });
    setStatus("monmix-status", res.message || "Done.");
    await keepingFocus(loadMonmixOutput);
  } catch (e) {
    setStatus("monmix-status", "Could not reach the device service.");
  } finally { setBusy(btn, false); }
}

/* ---- Output mix matrix (Routing > Custom cross-points) --------------- */

function buildMatrix(matrix) {
  if (!matrix || routingCtl.matrixBuilt) return;
  routingCtl.matrix = matrix;
  // Some devices (e.g. the Duo) expose the matrix for reading but not writing yet. When
  // read-only, the switches, mode dropdown and Chat presets stay disabled so a control is
  // never a silent no-op the user cannot hear.
  routingCtl.matrixReadOnly = !!matrix.read_only;
  // Let the server's own wording win when it sends one. The static markup describes a WRITABLE
  // matrix, and on a read-only device (the Duo) it would otherwise still invite you to "include or
  // exclude each source" while every switch is disabled.
  const mxNote = document.getElementById("routing-matrix-note");
  if (mxNote && matrix.note) mxNote.textContent = matrix.note;
  routingCtl.matrixSources = {};
  routingCtl.matrixSourceSpans = {}; // label span per source key, so we can retarget its text
  routingCtl.matrixSourceLabels = {}; // base source label per key (e.g. "Mic 1")
  routingCtl.matrixState = null;    // cache of live excluded state {output: {srcKey: bool}}

  const outSel = document.getElementById("matrix-output");
  outSel.innerHTML = "";
  matrix.outputs.forEach((o) => {
    const opt = document.createElement("option");
    opt.value = String(o.index);
    opt.textContent = o.label;
    outSel.appendChild(opt);
  });
  outSel.addEventListener("change", onMatrixOutputChange);
  // The mode dropdown STAGES; its own Apply writes. "Output to configure" beside it is not staged,
  // because it only changes which output you are looking at.
  document.getElementById("matrix-mode").addEventListener("change", () => {
    markStaged(document.getElementById("matrix-mode"),
               document.getElementById("matrix-mode-apply"));
    updateMatrixApplyLabel();
  });
  document.getElementById("matrix-mode-apply").addEventListener("click", onMatrixModeApply);

  const list = document.getElementById("matrix-sources-list");
  list.innerHTML = "";
  matrix.sources.forEach((s) => {
    // Placeholder text only; onMatrixOutputChange names the output in it. See there for why the
    // source comes first.
    const made = makeSwitchRow("matrix-src-" + s.key, s.label,
      (i) => onMatrixSourceToggle(s, i));
    routingCtl.matrixSources[s.key] = made.input;
    routingCtl.matrixSourceSpans[s.key] = made.span;
    routingCtl.matrixSourceLabels[s.key] = s.label;
    list.appendChild(made.row);
  });

  const resetOutBtn = document.getElementById("reset-outputs-btn");
  resetOutBtn.addEventListener("click", onResetAllOutputs);
  if (routingCtl.matrixReadOnly) {
    // Both carry the reason rather than the raw attribute, so they keep their place in the tab
    // order and say why when pressed.
    setAvailable(resetOutBtn, false, MATRIX_READ_ONLY_OFF);
    blockStaged(document.getElementById("matrix-mode-apply"), MATRIX_READ_ONLY_OFF);
    setStatus("reset-outputs-status", MATRIX_READ_ONLY_OFF);
  }
  wireSchemes();

  onMatrixOutputChange();
  routingCtl.matrixBuilt = true;
}

function currentMatrixOutput() {
  const idx = parseInt(document.getElementById("matrix-output").value, 10);
  return routingCtl.matrix.outputs.find((o) => o.index === idx);
}

/* Fetch the device's real per-cross-point excluded state and cache it. */
async function loadMatrixState() {
  try {
    const r = await fetch("/api/routing/matrix/live");
    const data = await r.json();
    routingCtl.matrixState = data.ok ? data.excluded : null;
    routingCtl.matrixModes = data.ok ? data.modes : null;
    routingCtl.matrixStateMsg = data.ok ? "" : (data.message || "State unavailable.");
  } catch (err) {
    routingCtl.matrixState = null;
    routingCtl.matrixModes = null;
    routingCtl.matrixStateMsg = "Could not read the device.";
  }
  applyMatrixForOutput();
}

/* THE APPLY BUTTON NAMES WHAT IT WILL DO, never a generic "Apply mix for this output". A generic
   label reads as covering the whole section - dropdown, Apply and sources together - so the source
   switches look like something you set BEFORE pressing it. They are not: the mode dropdown is
   staged behind this button and the source switches write immediately. "Set this output to Custom"
   cannot be read the wrong way, and it lets the hints underneath stay short instead of explaining
   the staging in prose. */
function updateMatrixApplyLabel() {
  const sel = document.getElementById("matrix-mode");
  const btn = document.getElementById("matrix-mode-apply");
  if (!sel || !btn) return;
  const chosen = sel.options[sel.selectedIndex];
  const label = (chosen && chosen.textContent || "").trim();
  btn.textContent = label ? "Set this output to " + label : "Apply mix for this output";
}

/* Reflect the cached live state (mode + per-source) for the selected output. */
function applyMatrixForOutput() {
  const o = currentMatrixOutput();
  const modes = routingCtl.matrixModes;
  const modeSel = document.getElementById("matrix-mode");
  if (modes && modes[String(o.index)] !== undefined) {
    setStagedValue(modeSel, modes[String(o.index)], null);
  }
  // EVERYTHING BELOW READS THE DEVICE VALUE, not the dropdown: while a change is staged the
  // dropdown shows what you have chosen, and the source switches must keep describing the mix the
  // output actually carries until Apply is pressed.
  const deviceMode = modeSel.dataset.deviceValue || modeSel.value;
  const isCustom = deviceMode === "2";
  const modeLabel = (o.modes.find((m) => String(m[0]) === deviceMode) || [, "this mode"])[1];
  const state = routingCtl.matrixState && routingCtl.matrixState[String(o.index)];
  const known = !!state;
  const readOnly = routingCtl.matrixReadOnly;
  // ARIA-DISABLED THROUGHOUT, never the attribute. Natively disabling takes a control OUT OF THE
  // TAB ORDER, so on any output not set to Custom every source switch simply vanishes as you tab,
  // with nothing said. They keep their place and their handlers refuse with the reason.
  setAvailable(modeSel, !readOnly,
    "Changing output mixes is not enabled on this device yet.");
  const editable = known && isCustom && !readOnly;
  const whyNot = readOnly
    ? "Changing output mixes is not enabled on this device yet."
    : (!known ? "The unit's current mix could not be read, so this cannot be changed safely."
              : "Sources can only be picked on a Custom mix. This output is on " + modeLabel + ".");
  Object.keys(routingCtl.matrixSources).forEach((key) => {
    const input = routingCtl.matrixSources[key];
    setAvailable(input, editable, whyNot);
    // excluded == true means NOT included, so the box is unticked.
    input.checked = known ? !state[key] : false;
  });
  // THE SOURCE LIST IS HIDDEN ON ANYTHING BUT CUSTOM, because on Main Mix or Mix Minus it does not
  // describe what the output carries - it shows the cross-points the desk has STORED, which the
  // engine ignores in those modes. Measured on Headphone 4: set it to Custom, exclude Mic 1, set it
  // back to Main Mix, and the switch still reads Mic 1 excluded while every source is audible, so
  // the list tells a blind operator that a mic is out of a mix it is in. Hiding it here is NOT the
  // vanishing-switches mistake elsewhere on this page: that is a control with something true to say
  // being taken out of the tab order, this is one with nothing true to say at all. The line above
  // it says what the mix is instead.
  const showSources = known && isCustom;
  document.getElementById("matrix-sources").hidden = !showSources;
  let summary;
  if (!known) {
    summary = routingCtl.matrixStateMsg || "Current state could not be read.";
  } else if (deviceMode === "1") {
    summary = modeLabel + ": every source except this output's own.";
  } else if (!isCustom) {
    summary = modeLabel + ": every source.";
  } else {
    summary = "";                // the source list itself says what is in a Custom mix
  }
  setText("matrix-mix-summary", summary);
  const note = readOnly
    ? "This shows the unit's live mix. Changing it is not enabled on this device yet."
    : "Ticked sources are included. Each switch takes effect straight away.";
  setText("matrix-sources-note", note);
  updateMatrixApplyLabel();
}

function onMatrixOutputChange(ev) {
  const o = currentMatrixOutput();
  updateSchemeSaveOutputLabel();    // the one-output save button names the output you are on
  // fillOptions rather than a bare rebuild: most outputs offer the SAME mode list, so switching
  // output leaves this dropdown's own nodes alone. The selection is reset here and then set from
  // the device by applyMatrixForOutput below, which a full rebuild would have done implicitly.
  const modeSel = fillOptions("matrix-mode", o.modes.map((m) => ({ value: m[0], label: m[1] })));
  if (modeSel) {
    modeSel.selectedIndex = 0;
    // FORGET THE PREVIOUS OUTPUT'S MODE. Without this, Apply would be comparing this output's
    // dropdown against the mode of the output you were looking at a moment ago.
    delete modeSel.dataset.deviceValue;
    delete modeSel.dataset.deviceLabel;
  }
  // Whether the source list is shown at all is applyMatrixForOutput's decision (it depends on the
  // APPLIED mix), so nothing is revealed here.
  // THE SOURCE THAT MATCHES THE OUTPUT IS SHOWN LIKE ANY OTHER. Do not hide it on the reasoning
  // that "a channel cannot be a source of itself": that is wrong for the four keys on both lists -
  // USB 1, USB 2, Chat and Bluetooth - whose cross-point cell is an ordinary one the desk exposes.
  // Taking USB 2 out of the USB 2 output is how you hear a second computer's screen reader locally
  // without sending it back to that computer, so hiding the switch makes a hand-built mix minus
  // impossible, which is the one mode the desk does not build for you. Mix Minus MODE, where the
  // desk drops the output's own source itself, is a different thing and is handled above.
  // Each switch names the output explicitly ("Mic 1 in Chat") so its accessible name is always
  // correct when focused, even though a screen reader caches and will not re-read a changed
  // <legend> group name.
  Object.keys(routingCtl.matrixSourceSpans).forEach((key) => {
    // SOURCE NAME FIRST: "Mic 1 in Chat", not "Include Mic 1 in the Chat output". A leading verb
    // delays the one fact you need, which is the source you are focused on. The verb is dropped
    // rather than moved: a checkbox announces its own checked state, so a label carrying the word
    // "included" would read
    // "Mic 1 included in Chat, not checked" and contradict itself on every unticked source.
    // The self-source is worded "back into itself" rather than "USB 2 in USB 2", which reads as a
    // stutter and hides the one thing about that switch worth knowing.
    routingCtl.matrixSourceSpans[key].textContent = (key === o.key)
      ? routingCtl.matrixSourceLabels[key] + " back into itself"
      : routingCtl.matrixSourceLabels[key] + " in " + o.label;
  });
  applyMatrixForOutput();
  setText("matrix-status", "Showing the " + o.label
    + " output. The mix and source list reflect the unit's current state.");
  // Announce via the live region too, but only on a real user change (not the initial
  // build): a mutated <legend> is not reliably re-read by screen readers, so the group
  // name can lag the dropdown. This keeps the announced output in sync with the control.
  if (ev) announce("Now configuring the " + o.label + " output mix.");
}

async function onMatrixModeApply() {
  const modeSel = document.getElementById("matrix-mode");
  const btn = document.getElementById("matrix-mode-apply");
  const blocked = btn.dataset.blockedReason || "";
  if (blocked) { announce(blocked, true); return; }
  if (modeSel.value === (modeSel.dataset.deviceValue || "")) {
    announce("That is the mix this output already carries. Choose another one first.", true);
    modeSel.focus();
    return;
  }
  const o = currentMatrixOutput();
  const mode = parseInt(modeSel.value, 10);
  const res = await routingPost("/api/routing/outputmode",
    { output: o.index, mode: mode }, btn);
  setText("matrix-status", res.message || "");
  if (routingCtl.matrixModes && res.ok) routingCtl.matrixModes[String(o.index)] = mode;
  // Either way, repaint from what the unit now has: on success that is the new mode and the source
  // switches that go with it, and on a refusal it puts the dropdown back rather than leaving it
  // showing a mix the output does not carry.
  applyMatrixForOutput();
}

async function onMatrixSourceToggle(source, input) {
  // A switch that cannot act must SAY so and put itself back: a flipped switch with no write behind
  // it reports a state the unit is not in.
  const no = unavailableReason(input);
  if (no !== null) { input.checked = !input.checked; announce(no, true); return; }
  const o = currentMatrixOutput();
  const excluded = !input.checked;
  const res = await routingPost("/api/routing/crosspoint",
    { output: o.index, source: source.index, excluded: excluded }, input);
  setText("matrix-status", res.message || "");
  if (!res.ok) {
    input.checked = !input.checked;             // revert the box if the write failed
  } else if (routingCtl.matrixState && routingCtl.matrixState[String(o.index)]) {
    routingCtl.matrixState[String(o.index)][source.key] = excluded;  // keep cache in sync
  }
}

// Sweep every output back to its default mix. Destructive to custom mixes, so it confirms
// first through the same true alertdialog the pad screen uses (confirmAction is generic; only
// its element ids are pad-flavoured).
async function onResetAllOutputs() {
  // Before the confirmation, not after: being asked to confirm something that cannot happen is
  // worse than being told plainly that it cannot.
  const no = unavailableReason(document.getElementById("reset-outputs-btn"));
  if (no !== null) { setStatus("reset-outputs-status", no); return; }
  const ok = await confirmAction(
    "Reset all output mixes?",
    "This puts every output back to its normal mix and puts every source back in each output, " +
    "following the faders. Any custom mixes you have set are wiped, including 'Chat = my mic " +
    "only'. It is reversible by setting them again, or by restoring a saved profile.",
    "Reset outputs");
  if (!ok) { announce("Cancelled. The output mixes were not changed."); return; }
  const btn = document.getElementById("reset-outputs-btn");
  setBusy(btn, true);
  setStatus("reset-outputs-status", "Resetting every output mix, please wait.");
  try {
    const res = await postJSON("/api/routing/reset-outputs", {});
    setStatus("reset-outputs-status", res.message || "Done.");
    if (res.ok) await loadMatrixState();   // refresh the per-output panel to the new live state
  } catch (err) {
    setStatus("reset-outputs-status", "Could not reach the device service.");
  } finally {
    setBusy(btn, false);
  }
}

/* ---- Mix schemes (Routing > Output mixes) ----------------------------
   The saved output-mix matrix, plus the built-ins the server derives from the connected desk. The
   dropdown carries BOTH: a built-in option's value is "builtin:<id>", a saved one's is
   "saved:<name>", so the two name spaces cannot collide (a saved scheme really can be called
   "Everything normal"). Each option's description is held here rather than in the DOM so selecting
   one can speak it without a round trip. */

let schemesData = null;      // the last /api/routing/schemes payload
const schemeInfo = {};       // option value -> {label, description, destructive, builtin, name}

function wireSchemes() {
  const sel = document.getElementById("scheme-select");
  if (!sel || sel.dataset.wired) return;
  sel.addEventListener("change", onSchemeChange);
  document.getElementById("scheme-apply-btn").addEventListener("click", onSchemeApply);
  document.getElementById("scheme-delete-btn").addEventListener("click", onSchemeDelete);
  document.getElementById("scheme-save-btn").addEventListener("click", () => onSchemeSave(false));
  document.getElementById("scheme-save-output-btn")
    .addEventListener("click", () => onSchemeSave(true));
  sel.dataset.wired = "1";
}

/* Fetch the list and (re)build the dropdown. Called when the Output mixes tab is opened and after
   any change to the store, so a scheme saved on another window's copy of the page still shows up.
   The rebuild is guarded the same way every other option list is: an unchanged list keeps its own
   nodes, so a screen reader sitting in the dropdown is not pulled out from under. */
async function loadSchemes() {
  let data;
  try {
    const r = await fetch("/api/routing/schemes");
    data = await r.json();
  } catch (err) {
    setText("scheme-status", "Could not reach the device service.");
    return;
  }
  if (!data || !data.ok) return;
  schemesData = data;
  const sel = document.getElementById("scheme-select");
  if (!sel) return;
  if (data.note) setText("schemes-note", data.note);
  Object.keys(schemeInfo).forEach((k) => delete schemeInfo[k]);
  const items = [];
  // EVERY OPTION CARRIES ITS SCOPE, from the server's `scope_text`, because which outputs a preset
  // rewrites is the thing you need before you choose it and the description is only read once you
  // reach Apply. It is never typed by anyone: a preset that
  // names an output this desk lacks reports the scope for THIS desk.
  const scope = (s) => (s.scope ? " (" + s.scope + ")" : "");
  (data.builtins || []).forEach((b) => {
    const value = "builtin:" + b.id;
    schemeInfo[value] = { label: b.name, description: b.description,
                          destructive: !!b.destructive, builtin: true, name: b.id };
    items.push({ value: value, label: b.name + scope(b) });
  });
  (data.saved || []).forEach((s) => {
    const value = "saved:" + s.name;
    schemeInfo[value] = { label: s.name, description: s.description || "",
                          destructive: false, builtin: false, name: s.name };
    items.push({ value: value, label: s.name + " (saved" + (s.scope ? ", " + s.scope : "") + ")" });
  });
  fillOptions("scheme-select", items);
  // A read-only device still gets to READ the list, so the dropdown and the name box stay live and
  // keep their place in the tab order; only the two writes are marked unavailable, with the reason.
  // Delete is decided per selection by onSchemeChange.
  const readOnly = !!data.read_only;
  const why = "Changing output mixes is not enabled on this device yet.";
  setAvailable(document.getElementById("scheme-apply-btn"), !readOnly, why);
  setAvailable(document.getElementById("scheme-save-btn"), !readOnly, why);
  setAvailable(document.getElementById("scheme-save-output-btn"), !readOnly, why);
  updateSchemeSaveOutputLabel();
  onSchemeChange();
}

/* The one-output save button NAMES the output it would save, so the label alone says what pressing
   it does and there is no second control to read first. Called when the list loads and whenever the
   output chooser above changes. */
function updateSchemeSaveOutputLabel() {
  const btn = document.getElementById("scheme-save-output-btn");
  if (!btn) return;
  const out = (routingCtl && routingCtl.matrix) ? currentMatrixOutput() : null;
  btn.textContent = out ? `Save only the ${out.label} mix as a preset`
                        : "Save only this output's mix as a preset";
}

function currentScheme() {
  const sel = document.getElementById("scheme-select");
  return sel ? schemeInfo[sel.value] || null : null;
}

/* Update the line that says what the selected scheme would do.

   IT MUST NOT SPEAK. A COLLAPSED select fires `change` on EVERY arrow key (the same trap that makes
   a preset apply per keypress), so announcing here talks over the option name: arrowing down the
   list you hear the description instead of the scheme you landed on.
   The description is the Apply button's aria-describedby as well, so the way to hear what you are
   about to do is to Tab to Apply - which is also the moment it matters. */
function onSchemeChange() {
  const s = currentScheme();
  const saved = !!(s && !s.builtin);
  const delBtn = document.getElementById("scheme-delete-btn");
  // aria-disabled, never the disabled attribute: a natively disabled button LEAVES THE TAB ORDER,
  // so Delete would vanish and reappear as you arrow the list, and pressing it would explain
  // nothing. Unavailable, in place, with the reason attached.
  setAvailable(delBtn, saved && !(schemesData && schemesData.read_only),
    schemesData && schemesData.read_only
      ? "Changing output mixes is not enabled on this device yet."
      : "Built-in presets cannot be deleted. Choose a saved one.");
  const text = s ? s.description : "No mix presets on this device.";
  const line = document.getElementById("scheme-description");
  if (line && line.textContent !== text) line.textContent = text;
}

async function onSchemeApply() {
  const btn = document.getElementById("scheme-apply-btn");
  const no = unavailableReason(btn);
  if (no !== null) { setText("scheme-status", no); announce(no, true); return; }
  const s = currentScheme();
  if (!s) return;
  if (s.destructive) {
    const ok = await confirmAction(
      "Apply " + s.label + "?",
      s.description + " It is reversible by setting the mixes again, by applying another preset, "
      + "or by restoring a saved profile.",
      "Apply preset");
    if (!ok) { announce("Cancelled. The output mixes were not changed."); return; }
  }
  setBusy(btn, true);
  const busy = "Applying " + s.label + ", please wait.";
  setText("scheme-status", busy);
  announce(busy);
  // THE RESULT IS SPOKEN LAST, after the busy state is cleared and the matrix has been re-read.
  // Announce it earlier and the focused button's aria-disabled coming back off, plus the read that
  // follows, land on top of the live region: the outcome goes unspoken and all you hear is the
  // button's own label again.
  let msg, bad = false;
  try {
    const res = await postJSON("/api/routing/schemes/apply",
      { name: s.name, builtin: s.builtin });
    msg = res.message || "Done.";
    bad = !res.ok;
    setText("scheme-status", msg);
    if (res.ok) await loadMatrixState();   // the read-back: refresh the per-output panel
  } catch (err) {
    msg = "Could not reach the device service.";
    bad = true;
    setText("scheme-status", msg);
  } finally { setBusy(btn, false); }
  announce(msg, bad);
}

/* `oneOutput` saves the mix of the output chosen at the top of the tab and nothing else, which is
   the shape of the built-in Chat presets: applying it later leaves every other mix alone. False
   saves the whole matrix, which is what an older preset holds. */
async function onSchemeSave(oneOutput) {
  const btn = document.getElementById(oneOutput ? "scheme-save-output-btn" : "scheme-save-btn");
  const no = unavailableReason(btn);
  if (no !== null) { setText("scheme-status", no); announce(no, true); return; }
  const out = oneOutput ? currentMatrixOutput() : null;
  if (oneOutput && !out) {
    announce("Choose an output at the top of this tab first.", true); return;
  }
  const input = document.getElementById("scheme-name");
  const name = (input.value || "").trim();
  if (!name) { announce("Type a name for the preset first.", true); input.focus(); return; }
  const exists = (schemesData && (schemesData.saved || []).some((s) => s.name === name));
  if (exists) {
    const ok = await confirmAction("Replace the mix preset " + name + "?",
      "A saved preset of that name already exists. Saving replaces it with "
      + (out ? "the " + out.label + " mix" : "the unit's mixes") + " as it is now.",
      "Replace preset");
    if (!ok) { announce("Cancelled. The saved preset was not changed."); return; }
  }
  setBusy(btn, true);
  const doing = out ? `Reading the ${out.label} mix, please wait.`
                    : "Reading the unit's output mixes, please wait.";
  setText("scheme-status", doing);
  announce(doing);
  let msg, bad = false;
  try {
    const res = await postJSON("/api/routing/schemes/save",
                               { name: name, output: out ? out.key : "" });
    msg = res.message || "Done.";
    bad = !res.ok;
    setText("scheme-status", msg);
    if (res.ok) {
      input.value = "";
      await keepingFocus(loadSchemes);
      const sel = document.getElementById("scheme-select");
      if (sel) { sel.value = "saved:" + res.name; onSchemeChange(); }
    }
  } catch (err) {
    msg = "Could not reach the device service.";
    bad = true;
    setText("scheme-status", msg);
  } finally { setBusy(btn, false); }
  announce(msg, bad);        // last, after the list rebuild - see onSchemeApply
}

async function onSchemeDelete() {
  const btn = document.getElementById("scheme-delete-btn");
  // Pressing an unavailable control SAYS why. Silence is indistinguishable from a missed press.
  const no = unavailableReason(btn);
  if (no !== null) { setText("scheme-status", no); announce(no, true); return; }
  const s = currentScheme();
  if (!s || s.builtin) return;
  const ok = await confirmAction("Delete the mix preset " + s.label + "?",
    "This removes the saved preset from this computer. The unit's mixes are not changed.",
    "Delete preset");
  if (!ok) { announce("Cancelled. The preset was not deleted."); return; }
  setBusy(btn, true);
  let msg, bad = false;
  try {
    const res = await postJSON("/api/routing/schemes/delete", { name: s.name });
    msg = res.message || "Done.";
    bad = !res.ok;
    setText("scheme-status", msg);
    if (res.ok) await keepingFocus(loadSchemes);
  } catch (err) {
    msg = "Could not reach the device service.";
    bad = true;
    setText("scheme-status", msg);
  } finally { setBusy(btn, false); }
  announce(msg, bad);        // last, after the list rebuild - see onSchemeApply
}

/* STEREO LINKED INPUTS - one switch per legal pair, built from the device's own pair table so a
   device with no proven pairs simply gets no section rather than three dead switches.

   A switch and not a staged Apply, unlike the fader dropdowns above. The Apply pattern exists
   because arrowing a closed SELECT fires change on every option, so browsing the list performed the
   swap. A checkbox has no such reading mode: you land on it, hear its state, and it only changes
   when you deliberately activate it. So immediate is safe here, and it matches the auto-mute
   switches on the same page. */
function buildStereoLinks(fm) {
  const box = document.getElementById("stereo-links");
  if (!box) return;
  box.innerHTML = "";
  routingCtl.stereo = {};
  const pairs = (fm && fm.stereo_pairs) || [];
  const head = document.getElementById("rt-h-stereo");
  const note = document.getElementById("stereo-note");
  // No pairs means the unit does not do this (or its codes are unproven), so the heading and the
  // explanation go too. A heading over an empty region is a dead end for heading navigation.
  const show = pairs.length > 0;
  if (head) head.hidden = !show;
  if (note) note.hidden = !show;
  if (!show) return;
  pairs.forEach((p) => {
    const made = makeSwitchRow("stereo-link-" + p.code, "Link " + p.label,
                               (input) => onStereoLink(p, input));
    routingCtl.stereo[p.code] = made.input;
    box.appendChild(made.row);
  });
  if (fm.stereo_read_only) {
    Object.values(routingCtl.stereo).forEach((input) => setAvailable(
      input, false, "Linking two inputs is not enabled on this device yet."));
    const p = document.createElement("p");
    p.className = "note"; p.setAttribute("role", "note");
    p.textContent = "Linking two inputs is not enabled on this device yet, so these switches are "
      + "read-only. They still show whether a pair is linked.";
    box.insertBefore(p, box.firstChild);
  }
}

/* The switch shows the DESK's answer, never the click: a refused or failed link puts it straight
   back, the same rule the auto-mute switches follow. Getting this wrong is how a control ends up
   claiming a state the unit does not have. */
async function onStereoLink(pair, input) {
  const no = unavailableReason(input);
  if (no !== null) {
    input.checked = !input.checked;      // aria-disabled, so the change still arrived here
    setStatus("stereo-status", no);
    return;
  }
  const wanted = input.checked;
  setStatus("stereo-status",
            (wanted ? "Linking " : "Unlinking ") + pair.label + ", please wait.");
  let res;
  try {
    res = await postJSON("/api/faders/stereo-link", { pair: pair.code, on: wanted });
  } catch (e) {
    res = { ok: false, message: "Could not reach the device service." };
  }
  setStatus("stereo-status", res.message || "Done.");
  // A link rewrites the fader layout, so the dropdowns above have to move with it.
  if (res.faders) applyFaderValues(res.faders, res.labels);
  if (res.stereo) applyStereoValues(res.stereo);
  else input.checked = !wanted;
}

function applyStereoValues(state) {
  (state || []).forEach((p) => {
    const input = routingCtl.stereo && routingCtl.stereo[p.code];
    if (input) input.checked = !!p.linked;
  });
}

// Set each fader dropdown to the channel currently on it. Unknown codes (e.g. a
// stereo-linked mic) get a fallback option so the control still shows the truth.
/* THE UNIT'S ANSWER, written into every row: the dropdown, the "now on this fader" hint, the
   remembered device value that Apply compares against, and the button's own availability. This is
   the ONLY place the device value is recorded, so a staged dropdown can never be mistaken for what
   the desk is doing. */
function applyFaderValues(faders, labels) {
  (faders || []).forEach((code, i) => {
    const sel = routingCtl.faders && routingCtl.faders[i];
    if (!sel || code === null || code === undefined) return;
    if (!Array.from(sel.options).some((o) => o.value === String(code))) {
      const o = document.createElement("option");
      o.value = String(code);
      // The SERVER names it. A stereo-linked pair is a real source code but is deliberately not in
      // the assignable list, so it has no option of its own; naming it here would mean the browser
      // keeping its own copy of the device's channel names, and it would read "Channel code 6".
      o.textContent = (labels && labels[i]) || ("Channel code " + code);
      sel.appendChild(o);
    }
    setStagedValue(sel, code, null);
  });
}

async function onFaderApply(index, code, sel, btn) {
  if (isBusy(btn)) {
    // aria-disabled, so the press still arrives here: say why rather than doing nothing.
    announce("This fader is already set to that. Choose a different channel first.", true);
    return;
  }
  const res = await routingPost("/api/faders/set", { fader: index, code: code }, btn);
  // A swap can change a second fader too, so re-apply the whole returned layout - which also puts
  // every row's hint and Apply button back in step with the unit.
  if (res.ok && res.faders) applyFaderValues(res.faders, res.labels);
  else if (!res.ok) applyFaderValues(lastFaderLayout());
  // A swap can also seat a channel on top of a linked pair, which unlinks it.
  if (res.stereo) applyStereoValues(res.stereo);
}

/* What the unit last told us, so a REFUSED apply can put the dropdown back instead of leaving it
   showing a change that did not happen. */
function lastFaderLayout() {
  const out = [];
  Object.keys(routingCtl.faders || {}).forEach((i) => {
    const sel = routingCtl.faders[i];
    out[i] = sel && sel.dataset.deviceValue !== undefined
      ? parseInt(sel.dataset.deviceValue, 10) : null;
  });
  return out;
}

async function onResetFaders() {
  const btn = document.getElementById("faders-reset-btn");
  const no = unavailableReason(btn);
  if (no !== null) { setStatus("faders-reset-status", no); return; }
  setBusy(btn, true);
  setStatus("faders-reset-status", "Resetting faders, please wait.");
  try {
    const res = await postJSON("/api/faders/reset", {});
    setStatus("faders-reset-status", res.message || "Done.");
    if (res.ok && res.faders) applyFaderValues(res.faders, res.labels);
    if (res.stereo) applyStereoValues(res.stereo);   // the default layout carries no linked pair
  } catch (e) {
    setStatus("faders-reset-status", "Could not reach the device service.");
  } finally { setBusy(btn, false); }
}

function applyRoutingValues(v) {
  // Kept so a REFUSED write can put every control back without going to the device for it: see
  // routingPost. It is what the unit last said, which is exactly what a refused control must show.
  _routingValues = v;
  if (v.faders) applyFaderValues(v.faders, v.fader_labels);
  if (v.stereo) applyStereoValues(v.stereo);
  if (routingCtl.usbMode && v.usb_multitrack !== null && v.usb_multitrack !== undefined)
    setStagedValue(routingCtl.usbMode, v.usb_multitrack, null);
  // String, because the option values came from JSON booleans and a select's value always is one.
  if (routingCtl.usbExpanded && v.usb_expanded !== null && v.usb_expanded !== undefined)
    setStagedValue(routingCtl.usbExpanded, String(!!v.usb_expanded), null);
  if (routingCtl.usbDriver && v.usb_driver && typeof v.usb_driver.text === "string"
      && routingCtl.usbDriver.textContent !== v.usb_driver.text)
    routingCtl.usbDriver.textContent = v.usb_driver.text;
  applyDriverButtons(v.usb_driver);
  applyBypass(routingCtl.usbBypass, routingCtl.usbBypassHint, v,
              v.usb_processing_bypass, v.usb_multitrack);
  // v.multitrack is still sent by /api/routing and is deliberately not applied here any more:
  // the control moved to the Recording page, which reads its own value.
  if (routingCtl.monitorMute && v.monitor_auto_mute !== null && v.monitor_auto_mute !== undefined)
    routingCtl.monitorMute.checked = !!v.monitor_auto_mute;
  if (routingCtl.btMute && v.bt_auto_mute !== null && v.bt_auto_mute !== undefined)
    routingCtl.btMute.checked = !!v.bt_auto_mute;
  // Per-output headphone types (GAP-6): headphone_types[i] lines up with routingCtl.headphones[i].
  if (Array.isArray(v.headphone_types)) {
    v.headphone_types.forEach((hpVal, i) => {
      const sel = routingCtl.headphones[i];
      if (sel && hpVal !== null && hpVal !== undefined) setStagedValue(sel, hpVal, null);
    });
  }
  if (routingCtl.compellorOn && v.compellor_on !== null && v.compellor_on !== undefined)
    routingCtl.compellorOn.checked = !!v.compellor_on;
  if (routingCtl.compellor) {
    [["threshold", v.compellor_threshold], ["attack", v.compellor_attack],
     ["release", v.compellor_release], ["gain", v.compellor_gain]].forEach(([k, val]) => {
      if (routingCtl.compellor[k] && val !== null && val !== undefined)
        routingCtl.compellor[k].value = String(Math.round(val * 100));
    });
  }
  if (routingCtl.delayOn && v.delay_on !== null && v.delay_on !== undefined)
    routingCtl.delayOn.checked = !!v.delay_on;
  if (routingCtl.delayMs && v.delay_seconds !== null && v.delay_seconds !== undefined)
    routingCtl.delayMs.value = String(Math.round(v.delay_seconds * 1000));
}

/* ---- Routing: the tab strip, and reading per tab -------------------------
   THE RULE FOR THIS PAGE: every property is read at the moment somebody looks at it. Reading
   everything on page ENTRY is weaker, and it has a failure mode - anything read once per page LOAD
   shows stale values after a profile restore while everything around it is live.
   Tabs make the rule cheaper AND stronger: each tab reads its own
   settings when it is opened, so a value is never older than the moment you arrived at it, and
   nothing is read for a tab nobody is on. Automatic activation means arrowing along the strip
   selects as it goes, so the reads are DEBOUNCED - arrowing from Output mixes to Recording must not
   fire five device reads. */
let routingTabs = null;
let _routingTabReadTimer = null;

// Faders and Master Mix are filled by the SAME single read, /api/routing/live, so they share one
// reader and page entry does not pay for it twice. USB audio and Auto-mute are sections of Master
// Mix and Levels rather than tabs of their own, so the strip is four.
// LEVELS MUST NOT JOIN THAT LIST even though it needs the same read: the forEach below OVERWRITES
// a key's entry with the plain live read, so adding it there silently throws away its own
// fader/monitor/output-mute reads and the tab comes up with no mute switches at all. Its entry
// does both reads itself.
const ROUTING_TABS_ON_LIVE = ["faders", "master"];
const ROUTING_TAB_READ = {
  // The mixes tab reads the live matrix AND refreshes the scheme list: a scheme saved in another
  // window (or on the phone) should be there when you come back to this one.
  mixes: async () => { await loadMatrixState(); await loadSchemes(); },
  levels: async () => {
    await refreshRoutingLive();          // the auto-mute switches, which moved onto this tab
    await loadFaderLevels(false);
    await loadMonmixOutput();
  },
};
ROUTING_TABS_ON_LIVE.forEach((k) => { ROUTING_TAB_READ[k] = () => refreshRoutingLive(); });

function wireRoutingTabs() {
  routingTabs = buildStaticTabs("routing", "routing-panel", { onSelect: (key) => {
      // A user tab change: read after the selection settles, so arrowing across the strip does not
      // queue a device read per tab.
      clearTimeout(_routingTabReadTimer);
      _routingTabReadTimer = window.setTimeout(() => readRoutingTab(key), 350);
    },
  });
}

/* Read the settings that belong to one tab. Errors report through that tab's own status line where
   it has one, so nothing is announced twice. */
async function readRoutingTab(key) {
  if (!routingModel) return;                  // page not built yet; entry will do the read
  const read = ROUTING_TAB_READ[key];
  if (!read) return;
  try { await read(); } catch (e) { /* each loader reports its own failure */ }
}

/* The one live read that covers the faders, the master processing, the recording mode, the
   auto-mutes and the headphone types: they all come out of /api/routing/live in one go. */
async function refreshRoutingLive() {
  try {
    const resp = await fetch("/api/routing/live");
    const data = await resp.json();
    if (!data.ok) {
      setText("routing-status", data.message || "Could not read the device.");
      announce(data.message || "Could not read the device.", true);
      return false;
    }
    applyRoutingValues(data);
    setText("routing-status", "");
    return true;
  } catch (err) {
    setText("routing-status", "Could not reach the device service.");
    announce("Could not reach the device service.", true);
    return false;
  }
}

/* Page entry: build the controls once, take the single live read that covers three of the five tabs
   (and doubles as the page's "is the device there" check), then read whatever the tab you arrived on
   needs on top of that. */
async function readRouting() {
  clearTimeout(_routingTabReadTimer);          // a pending tab read from the last visit is stale
  if (!routingModel) await loadRouting();
  if (!(await refreshRoutingLive())) return;
  const key = routingTabs ? routingTabs.current() : "mixes";
  if (ROUTING_TABS_ON_LIVE.indexOf(key) < 0) await readRoutingTab(key);
}

/* ---- Profiles: save / restore settings ------------------------------ */

/* The page shows two buttons; each opens a modal with two panes, the details form and then a
   progress pane. Both jobs run on the server (a save reads the whole desk, a restore writes it
   back, either one optionally moving pad audio over transfer mode) and NEITHER can be abandoned
   half way, so while a job runs the dialog has no dismiss path at all: Escape is swallowed and
   Close stays hidden until the server reports done. Progress text lives in a polite live region
   INSIDE the pane and is the only thing that speaks, so nothing is announced twice. */

let _profileJobRunning = false;    // gates Escape and Close on both dialogs
let _profileReturnFocus = null;    // the page button that opened the dialog
let _profileLastResult = "";       // outcome text, left on the page when the dialog closes
let _profileHandingOver = false;   // true only while a form dialog hands over to the progress one
let _baselineDeleteArmed = false;  // the as-found backup's deliberate second press (onProfileDelete)

// The page's dropdown is the single anchor for every action on an existing profile. preferName
// selects a specific entry afterwards (the profile just saved, or the new name after a rename);
// without it the current selection is kept where it still exists.
const PROFILE_ACTION_BTNS = ["profile-restore-open", "profile-rename-open", "profile-delete-open"];

/* ---- The ONE backups list -------------------------------------------------
   /api/backups returns every backup this desk can be put back from, whatever holds it: a profile
   on this computer, the unit's own show on the drive, or both paired under one name. The kind is
   this page's business and is never made the user's problem.
   What they DO get told, on the detail line under the list, is what the selected one can still do,
   because "restores in seconds" and "takes a few minutes" is a difference they can feel. */

let backupsData = null;          // the last /api/backups payload, for the detail line and the kinds

function backupEntry(key) {
  return ((backupsData && backupsData.backups) || []).find((b) => b.key === key) || null;
}

/* One backup as the dropdown reads it. The KIND is deliberately not spelled out as "profile" or
   "show": "made on the unit" is the only one worth saying, because it is the only one that behaves
   differently (the desk named it, so it cannot be renamed, and there is no copy on this computer). */
function backupLabel(b) {
  if (b.kind === "unit") return `${b.name} (made on the unit${b.modified ? ", " + b.modified : ""})`;
  return b.baseline ? `${b.name} (as found)` : b.name;
}

async function loadProfiles(preferName) {
  let data;
  try {
    const r = await fetch("/api/backups");
    data = await r.json();
  } catch (e) {
    return;
  }
  if (!data || !data.ok) return;
  backupsData = data;
  const sel = document.getElementById("profile-select");
  // The detail line follows the SELECTION, so it is wired here, once, on the element that survives
  // every rebuild (fillOptions replaces options, never the select itself).
  if (!sel.dataset.detailWired) {
    sel.addEventListener("change", updateBackupDetail);
    sel.dataset.detailWired = "1";
  }
  const want = preferName || sel.value;
  const list = data.backups || [];
  // Same rebuild rule as every other option list: an unchanged list keeps its own nodes so a reader
  // sitting in the dropdown is not pulled out from under.
  fillOptions("profile-select", list.map((b) => ({ value: b.key, label: backupLabel(b) })));
  if (want && list.some((b) => b.key === want)) sel.value = want;
  else if (want && list.some((b) => b.name === want)) {
    sel.value = (list.find((b) => b.name === want) || {}).key;    // a name from a save or rename
  }
  PROFILE_ACTION_BTNS.forEach((id) => {
    // Unavailable in place, with the reason, rather than three buttons that vanish from the tab
    // order until a backup exists. Each opener re-checks before it opens its dialog.
    setAvailable(document.getElementById(id), !!list.length,
      "There are no backups yet. Back the unit up first.");
  });
  // ONLY WHEN THERE IS NOTHING, which is the one case this line has news. A screen reader already
  // announces "1 of 13" on the combo box itself, so a count here is the same number twice, read
  // between the name you just landed on and the line explaining what it does.
  setText("profile-list-hint", list.length ? "" : "No backups yet. Back the unit up first.");
  // THE STATE LINE IS ABOUT THE UNIT'S HALF ONLY, because that is the half the user can change by
  // plugging a drive in. The profile half always works and saying so every visit is noise.
  setText("backup-state",
          !data.shows_capable
            ? "Backups are saved on this computer."
            : (data.storage
                 ? "Backups are saved on this computer and on the unit's own drive, so a restore "
                   + "takes seconds."
                 : "There is no card or drive in the unit, so backups are saved on this computer "
                   + "only. Put one in and the unit keeps its own copy too, which restores far "
                   + "faster."));
  if (data.note) setText("backup-state", data.note);
  updateBackupDetail();
}

/* What the SELECTED backup holds. Written in terms of what it can do, not which mechanism it is. */
function updateBackupDetail() {
  const b = backupEntry(selectedProfile());
  if (!b) { setText("backup-detail", ""); return; }
  let text;
  if (b.kind === "unit") {
    text = "Made on the unit itself, so it holds everything and puts the whole desk back in "
         + "seconds. There is no copy on this computer, and it cannot be renamed.";
  } else if (b.kind === "paired") {
    text = "The unit still has its own copy of this one, so restoring it takes seconds and puts "
         + "back everything, including what this app cannot write.";
  } else {
    const pads = b.pads || 0;
    text = "Held on this computer" + (pads ? `, with ${pads} SMART pad${pads === 1 ? "" : "s"}` : "")
         + ". The unit's own copy is not on the drive, so a restore writes your settings back one "
         + "at a time and takes longer.";
  }
  if (b.baseline) text += " This is the as-found backup: how the unit was before anything changed it.";
  setText("backup-detail", text);
}

// The backup currently picked on the page, or "" when the list is empty. The value is the KEY
// ("profile:<name>" or "show:<snapshot>"), which is also what every backup endpoint takes.
function selectedProfile() {
  return document.getElementById("profile-select").value || "";
}

// The NAME the server knows a backup by: a profile name, or the show key for a unit-made one.
function selectedBackupName() {
  const b = backupEntry(selectedProfile());
  if (!b) return "";
  return b.kind === "unit" ? b.key : b.name;
}

/* Hand over from a form dialog to the shared progress dialog. The form dialog is CLOSED first and
   the progress dialog opened fresh, with its text already in place, so a screen reader builds its
   buffer for content that is actually there. (Revealing a hidden pane inside the same dialog does
   not: the reader has already read the dialog, so a whole restore runs with nothing announced but
   the heading. See the comment on #progress-dialog in index.html.) */
function openProgressDialog(closeDialogId, title, firstMessage) {
  // The form dialog's close handler would put focus back on the page button, a step on the way to
  // nowhere that a screen reader would read out. Suppress it: focus is going to the progress
  // dialog, and the page button gets it back when THAT closes.
  _profileHandingOver = true;
  closeProfileDialog(closeDialogId);
  _profileHandingOver = false;
  setText("progress-dialog-title", title);
  setText("progress-dialog-step", firstMessage);
  document.getElementById("progress-close-btn").hidden = true;
  const dlg = document.getElementById("progress-dialog");
  dlg.showModal();
  document.getElementById("progress-dialog-title").focus();
}

/* Job over: retitle (not announced, since focus is already on the heading, so it is just there
   for anyone who navigates back to it), report the result through the live region, and reveal
   Close. Close is NOT auto-focused: focus is on the heading with Close the only focusable thing
   left, so one Tab reaches it, and nothing interrupts the result being read out. */
function finishProgressDialog(title, message) {
  _profileJobRunning = false;
  setText("progress-dialog-title", title);
  setText("progress-dialog-step", message);
  setText("progress-dialog-hint", "");
  document.getElementById("progress-close-btn").hidden = false;
  _profileLastResult = message;
}

/* How long a restore of this profile should take, from the ONE thing that dominates it: the pad
   count. Pads move their audio over transfer mode at roughly 15 seconds each (measured), so the
   old "a minute or more" was badly wrong for a full grid. Numbers come from the server so there is
   one source for them. */
function describeRestoreTime(info) {
  const perPad = info.seconds_per_pad || 15;
  const base = info.settings_seconds || 25;
  const pads = info.pads || 0;
  if (!pads) return "This profile has no SMART pads, so it should take about half a minute.";
  const total = base + pads * perPad;
  const mins = Math.round(total / 60);
  const roughly = total < 90 ? "about a minute"
                             : (mins === 1 ? "about a minute" : `roughly ${mins} minutes`);
  return `This profile has ${pads} SMART ${pads === 1 ? "pad" : "pads"}. Pads are restored one at a `
       + `time at about ${perPad} seconds each, so allow ${roughly} in total. The dialog reports `
       + `each pad as it goes.`;
}

/* Poll a background profile job until it reports done. Only a CHANGED message is written, so the
   live region does not re-announce the same step every tick.

   TWO FAILSAFES, because this dialog deliberately has no dismiss path while a job runs and a
   silent server would otherwise trap you in it with nothing to press:
     - the service stops answering at all (it died, or the port went away), or
     - it answers but reports no new step for minutes on end (a device read wedged holding the
       HID lock, which no amount of waiting in the browser will clear).
   Either way the poll gives up and reports a failure, which reveals Close and frees Escape. The
   wording says the job MAY still be running, because from here we genuinely cannot tell. */
const POLL_TICK_MS = 1200;
const POLL_MAX_DEAD_TICKS = 8;      // ~10s of unanswered polls
const POLL_MAX_QUIET_TICKS = 125;   // ~2.5 min with no new step

function pollProfileJob(url, statusId) {
  return new Promise((resolve) => {
    let last = "";
    let dead = 0;
    let quiet = 0;
    const iv = setInterval(async () => {
      let s;
      try {
        s = await (await fetch(url)).json();
        dead = 0;
      } catch (e) {
        if (++dead < POLL_MAX_DEAD_TICKS) return;      // transient; retry next tick
        clearInterval(iv);
        resolve({ done: true, ok: false,
                  message: "Lost contact with the device service, so this can no longer be "
                         + "followed. The job may still be running. Close this, then reopen the "
                         + "Backups page to check." });
        return;
      }
      if (!s.done && s.message && s.message !== last) {
        last = s.message;
        quiet = 0;
        setText(statusId, s.message);
      } else if (!s.done) {
        quiet++;
      }
      if (s.done) {
        clearInterval(iv);
        resolve(s);
        return;
      }
      if (quiet >= POLL_MAX_QUIET_TICKS) {
        clearInterval(iv);
        resolve({ done: true, ok: false,
                  message: "The unit has not reported a new step for over two minutes, so this "
                         + "has stopped waiting. The job may still be running, or the unit may "
                         + "need unplugging and plugging back in. Close this, then reopen the "
                         + "Backups page to check." });
      }
    }, POLL_TICK_MS);
  });
}

/* ---- Save profile dialog ---- */

function openSaveDialog() {
  _profileReturnFocus = document.getElementById("profile-save-open");
  _profileJobRunning = false;
  document.getElementById("save-dialog").showModal();
  const input = document.getElementById("profile-name");
  input.focus();
  input.select();      // last name kept, but typing replaces it
}

async function onProfileSave(ev) {
  ev.preventDefault();
  const input = document.getElementById("profile-name");
  const name = input.value.trim();
  if (!name) {
    announce("Enter a profile name first.", true);
    input.focus();
    return;
  }
  // PADS ARE ALWAYS IN A BACKUP, with nothing asking first. The unit's own half carries them
  // whether we like it or not, so a profile without them would make the pair disagree about what
  // the backup holds; and pads cost about a second each to take (measured), which is not a
  // decision worth interrupting anyone for.
  _profileJobRunning = true;
  openProgressDialog("save-dialog", "Backing up",
                     "Backing the unit up: its own copy if there is a drive in, then the settings "
                     + "and each SMART pad on this computer.");
  try {
    const start = await postJSON("/api/backup", { name });
    if (!start.ok) {
      finishProgressDialog("Backup failed", start.message || "Could not start the backup.");
      return;
    }
    const s = await pollProfileJob("/api/profiles/save/status", "progress-dialog-step");
    finishProgressDialog(s.ok ? "Backed up" : "Backup failed", s.message || "Done.");
    await loadProfiles(name);            // leave the backup just made selected on the page
  } catch (e) {
    finishProgressDialog("Backup failed", "Could not reach the device service.");
  }
}

/* ---- Restore profile dialog ---- */

async function openRestoreDialog() {
  const entry = backupEntry(selectedProfile());
  const name = selectedBackupName();
  if (!name) {
    announce("No backup selected.", true);
    return;
  }
  _profileReturnFocus = document.getElementById("profile-restore-open");
  _profileJobRunning = false;
  const label = entry ? entry.name : name;
  // WHICH HALF WILL DO THE WORK decides everything else in this dialog, so it is asked first and
  // once. A backup the unit still holds goes back in seconds and completely, so there is no time to
  // quote and nothing for "reset to factory first" to clear out; one held only on this computer is
  // the older, slower path and keeps both.
  let viaShow = false;
  // WHAT THIS BACKUP WILL LEAVE YOU UNABLE TO HEAR, and it goes FIRST in the description rather
  // than last. Everything the app says after a restore is spoken through the desk, so a restore
  // that silences an output silences its own explanation: lose Main Stereo and the announcement
  // saying so goes with it. This dialog is read while the audio still works and can still be
  // cancelled, which makes it the only place the warning is any use.
  let planWarnings = [];
  try {
    const plan = await postJSON("/api/backup/plan", { name });
    viaShow = plan.ok && plan.via === "show";
    planWarnings = (plan && plan.warnings) || [];
  } catch (e) { /* fall through to the profile wording, which is the cautious one */ }
  // Leads with the word so it is unmistakable, and is part of the DOM before showModal: a screen
  // reader reads a dialog once, on open, so anything arriving later is heard by nobody.
  const warnText = planWarnings.length ? ("Warning. " + planWarnings.join(" ") + " ") : "";
  // NO "RESET TO FACTORY FIRST" IN THIS DIALOG. It runs showControlNewFromDefault, which crashes
  // the desk about one time in five and needs its MAINS pulled, and here that risk would be taken
  // on by somebody who believes they are restoring a backup, not resetting a desk. The most
  // dangerous command in the app has no business being a modifier on a routine one. The standalone
  // factory reset keeps it, where choosing it is the entire point.
  const modeField = document.getElementById("restore-mode-field");
  if (modeField) modeField.hidden = true;
  if (viaShow) {
    setText("restore-form-desc",
            warnText
            + `This puts the whole unit back from its own copy of "${label}": every setting, the `
            + "routing, the fader layout and all your SMART pads, exactly as they were. It takes a "
            + "few seconds, and it moves the monitor speaker level, so turn speakers down first.");
    // THE WARNING IS HERE BECAUSE THIS IS WHERE THE DECISION IS MADE, before the Restore button is
    // reachable. It says power rather than replug because a replug does not recover a locked-up
    // desk, and it names the screen-reader consequence, which is the part that matters most to the
    // people this app is for: if the desk is your audio device, a desk that stops responding takes
    // your speech with it.
    setText("restore-time-estimate",
            "One thing to know first. The unit sometimes stops responding after restoring its own "
            + "copy, and has to be unplugged from the USB socket nearest the power inlet, or have "
            + "its power pulled, before it comes back. Nothing is lost when that happens: the "
            + "restore itself goes through. But if your screen reader plays through the "
            + "RODECaster, you will lose speech until it is back, so it is worth knowing before "
            + "you press Restore.");
    document.getElementById("restore-dialog").showModal();
    document.getElementById("restore-cancel-btn").focus();
    return;
  }
  // The slower path, which has never hung a desk. Same sentence shape, different promises.
  setText("restore-form-desc",
          warnText
          + `This puts your saved settings back onto the unit from this computer's copy of `
          + `"${label}". Anything the unit does not accept is skipped and reported afterwards. `
          + "The SMART pads saved in it are put back and any other pad on the unit is left alone; "
          + "to make the grid match exactly, clear all pads first. It cannot be undone except by "
          + "restoring another backup.");
  // Ask what is IN this profile before quoting a time, since its pad count decides it. Done before
  // showModal so the estimate is part of the dialog from the moment it opens (a screen reader reads
  // the dialog once, on open; text arriving afterwards is text nobody hears).
  let estimate = "";
  // TWO THINGS COME BACK FROM THIS CALL, and the second decides which options exist: `can_clean`
  // says whether there is anywhere to save the backup a clean restore must take first. Asked fresh
  // here rather than carried over from the Shows page, because a drive can be pulled in between.
  let canClean = true;
  let cleanReason = "";
  try {
    const info = await postJSON("/api/profiles/info", { name });
    if (info.ok) {
      estimate = describeRestoreTime(info);
      canClean = info.can_clean !== false;
      cleanReason = info.clean_blocked_reason || "";
    }
  } catch (e) { /* no estimate is better than a wrong one */ }
  setText("restore-time-estimate", estimate);
  // Always reopen on the SAFE option. A destructive choice must be made afresh each time, not
  // inherited from whatever the last restore happened to use.
  document.getElementById("restore-mode-merge").checked = true;
  // A clean restore resets the unit first AND backs it up first, so it needs the CAPABILITY and
  // somewhere to WRITE. Gating on the capability alone offers it on a Duo, which has the capability
  // and no removable storage at all, so the option is offered and then refused.
  setAvailable(document.getElementById("restore-mode-clean"), _showsWritable && canClean,
    !_showsWritable
      ? "Resetting first is not available on this unit, so its settings can only be written over."
      : cleanReason || "Resetting first needs somewhere to save a backup first.");
  document.getElementById("restore-dialog").showModal();
  // Focus Cancel, not Restore: this dialog exists to be read before an overwrite that cannot be
  // undone, so the safe action is the one under your hands when it opens.
  document.getElementById("restore-cancel-btn").focus();
}

async function doRestore() {
  const entry = backupEntry(selectedProfile());
  const name = selectedBackupName();
  if (!name) {
    announce("No backup selected.", true);
    return;
  }
  // Read the mode BEFORE the dialog closes, since closing it takes the radios with it. A hidden
  // mode field means the unit's own copy is doing the work, where the mode has no meaning.
  // Always false: the mode field is hidden above and the option is retired. Read from nothing
  // rather than from a radio that can no longer be reached.
  const clean = false;
  const label = entry ? entry.name : name;
  _profileJobRunning = true;
  openProgressDialog("restore-dialog", "Restoring backup",
                     clean
                       ? `Backing the unit up, resetting it, then restoring "${label}".`
                       : `Restoring "${label}" to the unit.`);
  try {
    // ONE VERB FOR A LIST OF TWO KINDS: the server picks the unit's own copy when it is still on
    // the drive and this computer's when it is not, and "reset first" forces the profile path
    // because that mode exists to clear what a profile cannot carry.
    const start = await postJSON("/api/backup/restore", { name, clean });
    if (!start.ok) {
      finishProgressDialog("Restore failed", start.message || "Could not start the restore.");
      return;
    }
    // POLL THE JOB THAT IS ACTUALLY RUNNING. The two halves report through different job states,
    // and the server says which one it started; guessing would mean watching a finished restore in
    // silence.
    const statusUrl = start.via === "show" ? "/api/shows/status" : "/api/profiles/restore/status";
    const s = await pollProfileJob(statusUrl, "progress-dialog-step");
    finishProgressDialog(s.ok ? "Backup restored" : "Restore failed", s.message || "Done.");
    await loadProfiles(selectedProfile());
  } catch (e) {
    finishProgressDialog("Restore failed", "Could not reach the device service.");
  }
}

/* ---- The unit's own Shows: export, restore, delete, factory reset ----

   Kept apart from the profile code above rather than merged into it, because they are different
   operations on different storage and the one thing that must never happen is a user pressing the
   wrong one. Shared where sharing is honest: the progress dialog and the job poller.

   THE SNAPSHOT INDEX IS THE HANDLE, never the uid and never the uuid. A uid is a POSITION in the
   unit's list and every export renumbers the lot; a uuid names the SHOW rather than the file, so
   several backups of one desk state share one. Only the snapshot index stays put and stays unique
   (measured on hardware). The dropdown's option values are snapshot indexes and
   the server resolves them to a uid at the moment of the write. */

let _showConfirmAction = null;      // which action the shared confirm dialog is armed for
let _showsWritable = true;
let _showsCanBackup = true;
let _showsBackupBlockedReason = "";

/* What the FACTORY RESET needs to know, and all this endpoint is read for: whether this desk can
   be reset from here at all, and whether it can back itself up first. The list, the export, the
   restore and the delete all belong to the one Backups list above.

   THE UNDO NOTE IS THE POINT. A reset on a unit with no card or drive in cannot take the backup
   that makes it undoable, and saying so before the dialog opens is the difference between an
   informed press and a surprise. */
async function loadShows() {
  let data;
  try {
    data = await (await fetch("/api/shows")).json();
  } catch (e) {
    return;
  }
  _showsWritable = data.writable !== false;
  _showsCanBackup = data.can_backup !== false;
  _showsBackupBlockedReason = data.backup_blocked_reason || "";
  setAvailable(document.getElementById("show-reset-btn"), _showsWritable,
    "This unit cannot be reset from AccessCaster.");
  setText("reset-undo-note", _showsCanBackup
    ? "The unit backs itself up first, so this can be undone by restoring that backup."
    : "There is no memory card or USB drive in the unit, so it cannot back itself up first, which "
      + "means this cannot be undone.");
}

/* THE FACTORY RESET'S CONFIRMATION, and only that: restore and delete belong to the one Backups
   list, so the branches here are the reset's. Focus lands on Cancel, because this is the most
   destructive control in the app. */
function openShowConfirm(action) {
  const label = "";
  _showConfirmAction = action;
  _profileReturnFocus = document.getElementById("show-reset-btn");
  _profileJobRunning = false;
  const copy = {
    restore: {
      title: "Restore show",
      go: "Restore",
      desc: `This replaces everything on the unit with the backup "${label}": every setting, the `
          + `routing, the fader layout and all your SMART pads. It takes a few seconds. It also `
          + `moves the monitor speaker level, so turn speakers down before you start. What is on `
          + `the unit now is not kept unless you back it up first.`,
    },
    delete: {
      title: "Delete show",
      go: "Delete",
      desc: `This permanently removes the backup "${label}" from the unit's recordings drive and `
          + `gives the space back. It does not change any setting on the unit. It cannot be undone.`,
    },
    reset: {
      title: "Start again from factory settings",
      go: "Reset the unit",
      // THE DROPOUT IS NAMED HERE, WITHOUT A NUMBER. Resetting is when the unit rebuilds its USB
      // connection, and sometimes it does not come back until the cable is replugged. No rate is
      // quoted on purpose: the one we have is from a handful of trials, and a confident-sounding
      // figure is what sent this project the wrong way when 45 clean trials on the other desk were
      // read as "it does not happen". What the user needs is that it can happen and what to do.
      desc: `This puts every setting and every SMART pad back to how the unit left the factory, `
          + `including RODE's eight factory pad sounds, which the unit rebuilds from its own `
          + `firmware. That is the only way to get those sounds back, so if what you actually `
          + `want is your own settings returned, restore a backup instead: it is quicker and it `
          + `does not need the unit to reconnect. `
          + `Your wifi, the language and the analytics answer are not part of it and are left `
          + `alone, and the recordings on the drive are untouched. The monitor speaker level goes `
          + `back to its default, so turn speakers down before you start. One thing to expect: `
          + `resetting rebuilds the unit's USB connection, and it sometimes does not come back on `
          + `its own. If the app stops finding the unit afterwards, unplug the USB cable in the `
          + `socket nearest the power inlet, wait a few seconds and plug it back in. The reset `
          + `itself will still have happened and nothing is lost.`,
    },
  }[action];
  setText("show-confirm-title", copy.title);
  setText("show-confirm-go", copy.go);

  // The backup tick-box belongs to the reset alone: it is the reset's only undo.
  const row = document.getElementById("show-backup-row");
  const box = document.getElementById("show-backup-first");
  row.hidden = action !== "reset";

  // AN OPTION THAT CANNOT WORK IS NOT OFFERED. With no card or drive in the desk there is nowhere
  // to write a backup, and leaving the tick on means the reset runs, the backup fails, and the user
  // lands in a failure dialog. The tick is cleared and disabled instead.
  //
  // THE REASON GOES IN THE DESCRIPTION, NOT JUST THE HINT, and that is the whole accessibility of
  // it: a disabled checkbox is out of the tab order, so its hint would never be reached and the
  // control would simply seem to be missing. show-confirm-desc is the alertdialog's
  // aria-describedby, so it is announced when the dialog opens - which is the one moment this has
  // to be heard, because it changes what the reset means: it cannot be undone.
  const blocked = action === "reset" && !_showsCanBackup;
  box.disabled = blocked;
  box.checked = !blocked;
  setText("show-backup-first-desc", blocked
    ? (_showsBackupBlockedReason || "There is nowhere to save a backup.")
    : "Takes about a second and about 24 megabytes on the recordings drive. Without it there is no "
      + "way back to how the unit is now.");
  setText("show-confirm-desc", copy.desc + (blocked
    ? " " + (_showsBackupBlockedReason || "There is nowhere to save a backup.")
      + " So this cannot be backed up first, and it cannot be undone. Put a memory card or a USB "
      + "drive in the unit first if you want a way back."
    : ""));

  document.getElementById("show-confirm-dialog").showModal();
  document.getElementById("show-confirm-cancel").focus();
}

async function doShowConfirmed() {
  const action = _showConfirmAction;
  if (!action) return;
  const snapshot = null, label = "";
  const backupFirst = document.getElementById("show-backup-first").checked;
  const spec = {
    restore: { url: "/api/shows/import", body: { snapshot },
               title: "Restoring show", first: `Restoring "${label}" to the unit.`,
               okTitle: "Show restored", failTitle: "Restore failed" },
    delete: { url: "/api/shows/delete", body: { snapshot },
              title: "Deleting backup", first: `Deleting "${label}" from the unit.`,
              okTitle: "Backup deleted", failTitle: "Delete failed" },
    reset: { url: "/api/shows/reset", body: { backup_first: backupFirst },
             title: "Resetting the unit",
             first: backupFirst ? "Backing the unit up, then resetting it."
                                : "Resetting the unit to factory settings.",
             okTitle: "Unit reset", failTitle: "Reset failed" },
  }[action];
  await runShowJob("show-confirm-dialog", spec);
}

/* ---- This app's own settings: export and import ---------------------------
   Not part of a profile, and the reason is in index.html beside the controls. These are quick,
   local, non-device operations - no HID, no progress dialog - so they follow the plain
   press-and-report shape rather than the show/profile job machinery.

   THE REPLY IS THE WHOLE FEATURE. Every one of these says exactly what happened in the live region:
   how many shortcuts travelled, where the file went, and above all what was NOT applied. A setting
   silently dropped for naming an audio device this computer has not got is precisely the kind of
   thing somebody would otherwise spend an evening hunting. */
async function onAppSettingsExport() {
  const btn = document.getElementById("appsettings-export-btn");
  setBusy(btn, true);
  let msg, bad = false;
  try {
    const res = await postJSON("/api/appsettings/export", {});
    msg = res.message || "Done.";
    bad = !res.ok;
  } catch (err) {
    msg = "Could not reach the device service.";
    bad = true;
  } finally { setBusy(btn, false); }
  setText("appsettings-state", msg);
  announce(msg, bad);        // last, after setBusy: see the outcome-announced-last rule
}

/* START AT SIGN-IN. The switch reflects the HKCU Run value autostart.py owns, which is the same one
   installer.iss writes, so ticking the installer box and flicking this switch are the same act.

   `supported` FALSE MEANS REFUSE, NOT HIDE: a source run cannot write a startup entry that would
   still work (it would name python.exe plus a script path), so the switch stays where it is,
   announces itself as unavailable and carries the reason. Same rule as every other inert control
   here - a vanishing switch tells a screen reader user nothing at all. */
async function loadAutostart() {
  const sw = document.getElementById("autostart-switch");
  if (!sw) return;
  try {
    const d = await (await fetch("/api/appsettings/autostart")).json();
    sw.checked = !!d.enabled;
    setAvailable(sw, !!d.supported, d.message || "This cannot be changed on this copy.");
    setText("autostart-hint", d.message
      || "It starts in the system tray without opening this page.");
  } catch (err) {
    setAvailable(sw, false, "Could not reach the device service.");
  }
}

async function onAutostartToggle(on, input) {
  const no = unavailableReason(input);
  if (no !== null) { input.checked = !input.checked; announce(no, true); return; }
  let msg, bad = false;
  try {
    const res = await postJSON("/api/appsettings/autostart", { on: on });
    msg = res.message || "Done.";
    bad = !res.ok;
    if (!res.ok) input.checked = !!res.enabled;   // a refusal must not leave the switch lying
  } catch (err) {
    msg = "Could not reach the device service.";
    bad = true;
    input.checked = !input.checked;
  }
  setText("autostart-hint", msg);
  announce(msg, bad);
}

async function onAppSettingsOpenFolder() {
  const btn = document.getElementById("appsettings-open-btn");
  setBusy(btn, true);
  let msg, bad = false;
  try {
    const res = await postJSON("/api/appsettings/open", {});
    msg = res.message || "Done.";
    bad = !res.ok;
  } catch (err) {
    msg = "Could not reach the device service.";
    bad = true;
  } finally { setBusy(btn, false); }
  setText("appsettings-state", msg);
  announce(msg, bad);
}

/* The file is read HERE and posted as the body, so the server never deals with an upload. It also
   means a file that is not even JSON is caught before anything is sent. */
async function onAppSettingsImport() {
  const btn = document.getElementById("appsettings-import-btn");
  const picker = document.getElementById("appsettings-file");
  const file = picker && picker.files && picker.files[0];
  if (!file) {
    const no = "Choose a settings file first.";
    setText("appsettings-state", no);
    announce(no, true);
    if (picker) picker.focus();
    return;
  }
  // Restoring shortcuts REPLACES the ones in use, so it is confirmed first. It is reversible only
  // if the user happens to have an export of what they had, which is exactly what they may not.
  const ok = await confirmAction(
    "Restore this app's settings?",
    "This replaces your keyboard shortcuts and your announcement and level-warning preferences "
    + "with the ones in that file. The unit itself is not touched at all.",
    "Restore settings");
  if (!ok) { announce("Cancelled. Your settings were not changed."); return; }

  setBusy(btn, true);
  let msg, bad = false;
  try {
    const text = await file.text();
    let payload;
    try {
      payload = JSON.parse(text);
    } catch (e) {
      throw new Error("That file is not readable as settings. Choose the file AccessCaster saved.");
    }
    const res = await postJSON("/api/appsettings/import", payload);
    msg = res.message || "Done.";
    bad = !res.ok;
    if (res.ok) await refreshAfterAppSettingsImport();
  } catch (err) {
    msg = (err && err.message) || "Could not restore those settings.";
    bad = true;
  } finally { setBusy(btn, false); }
  setText("appsettings-state", msg);
  announce(msg, bad);
}

/* The shortcuts page and the announcement settings are both now out of step with the app, so they
   are re-read rather than left showing what was there before the import. */
async function refreshAfterAppSettingsImport() {
  try { if (typeof loadHotkeys === "function") await loadHotkeys(); } catch (e) { /* not built */ }
  try { if (typeof loadAnnounceState === "function") await loadAnnounceState(); }
  catch (e) { /* not built */ }
}

/* Every show command follows the same shape: start it, follow the one job status, refresh the list.
   The list refresh is NOT optional - an export renumbers every uid on the unit, so a page holding
   the old list is holding stale positions. */
/* Which driver buttons make sense right now, from the LIVE read.

   THE DOWNLOAD BUTTON GOES WHEN THE DRIVER IS IN, because installing the same version again tells
   the user nothing and costs them a UAC prompt. The PAGE button stays either way: repairing or
   reinstalling is exactly what somebody with a half-working driver needs, and the server's refusal
   message names that button, so a state where it is missing would point at something that is not
   there. Hidden rather than removed, so the row never changes length under a screen reader
   mid-read. */
function applyDriverButtons(drv) {
  if (!routingCtl.driverInstall) return;
  const known = drv && typeof drv.installed === "boolean";
  const missing = known && drv.installed === false;
  routingCtl.driverInstall.hidden = !missing;
  routingCtl.driverPage.textContent = missing
    ? "Open RODE's software page instead"
    : "Open RODE's software page, to reinstall or repair the driver";
  // SAID BEFORE THE PRESS, not buried in Help: this is the one thing in the app that reaches the
  // internet, and somebody who chose it partly for what it does not do should hear that here.
  routingCtl.driverHint.textContent = !known
    ? "Whether RODE's driver is installed could not be read."
    : (missing
        ? "Expanded needs this driver. Downloading it is the only time AccessCaster connects to "
          + "the internet, and it only ever happens when you press that button. The file comes "
          + "from RODE, is checked to be signed by them, and their own installer then runs and "
          + "asks Windows for permission in the usual way."
        : "The driver is installed. If it is not working, RODE's own installer can repair it.");
}

/* Fetch RODE's driver and hand it over, behind a confirmation and then the progress dialog.
   CONFIRMED FIRST because this is the app's only outbound connection and the one place it installs
   anything; the dialog says so in as many words, so agreeing to it is a decision rather than a
   side effect of pressing a button labelled "download". */
async function installDriver() {
  const ok = await confirmAction(
    "Download RODE's driver?",
    "AccessCaster will connect to RODE's website, download their Virtual Devices driver, check it "
      + "is signed by RODE, and then run their installer. Windows will ask your permission, and the "
      + "installer may want to restart the computer. This is the only time this app uses the "
      + "internet.",
    "Download and install");
  if (!ok) return;
  _profileJobRunning = true;
  openProgressDialog(null, "Installing RODE's driver", "Starting.");
  try {
    const start = await postJSON("/api/driver/install", {});
    if (!start.ok) {
      finishProgressDialog("Driver not installed", start.message || "Could not start.");
      return;
    }
    const s = await pollProfileJob("/api/driver/status", "progress-dialog-step");
    finishProgressDialog(s.ok ? "RODE's installer is running" : "Driver not installed",
                         s.message || "Done.");
  } catch (e) {
    finishProgressDialog("Driver not installed", "Could not reach the device service.");
  }
  // The driver's own line, and the buttons, come off the next read of this page rather than being
  // guessed at here: the install is RODE's from the moment it is handed over, and it may need a
  // restart before Windows reports it at all.
  await loadRouting();
}

async function runShowJob(closeDialogId, spec) {
  _profileJobRunning = true;
  openProgressDialog(closeDialogId, spec.title, spec.first);
  try {
    const start = await postJSON(spec.url, spec.body);
    if (!start.ok) {
      finishProgressDialog(spec.failTitle, start.message || "Could not start.");
      await loadShows();
      return;
    }
    const s = await pollProfileJob("/api/shows/status", "progress-dialog-step");
    finishProgressDialog(s.ok ? spec.okTitle : spec.failTitle, s.message || "Done.");
  } catch (e) {
    finishProgressDialog(spec.failTitle, "Could not reach the device service.");
  }
  await loadShows();
}

/* ---- Rename profile dialog ---- */

/* Rename and delete are instant file operations on this computer, so neither gets a progress
   pane. Both report through the page's result line plus one announcement, and both keep their
   dialog OPEN on failure with the reason shown in it, so a rejected name never closes the dialog
   and leaves you guessing. */

function openRenameDialog() {
  const entry = backupEntry(selectedProfile());
  // THE DESK NAMES ITS OWN, AFTER WHICHEVER SHOW WAS LOADED, and offers no rename, so there is
  // nothing here to change. Said out loud rather than silently ignored.
  if (entry && !entry.renameable) {
    announce("This backup was made on the unit, which names its own backups, so it cannot be "
             + "renamed here.", true);
    return;
  }
  const name = selectedBackupName();
  if (!name) {
    announce("No profile selected.", true);
    return;
  }
  _profileReturnFocus = document.getElementById("profile-rename-open");
  setText("rename-dialog-old", name);
  setText("rename-error", "");
  const input = document.getElementById("profile-new-name");
  input.value = name;                          // start from the old name; typing replaces it
  input.removeAttribute("aria-invalid");
  document.getElementById("rename-dialog").showModal();
  input.focus();
  input.select();
}

async function onProfileRename(ev) {
  ev.preventDefault();
  const oldName = selectedProfile();
  const input = document.getElementById("profile-new-name");
  const newName = input.value.trim();
  const fail = (msg) => {
    setText("rename-error", msg);             // role="alert", so it is announced where it appears
    input.setAttribute("aria-invalid", "true");
    input.focus();
  };
  if (!newName) return fail("Enter a new name.");
  try {
    const res = await postJSON("/api/profiles/rename", { name: oldName, new_name: newName });
    if (!res.ok) return fail(res.message || "Could not rename the backup.");
    setText("rename-error", "");
    input.removeAttribute("aria-invalid");
    _profileLastResult = res.message || "Renamed.";
    await loadProfiles(newName);              // keep the SAME profile selected under its new name
    closeProfileDialog("rename-dialog");
    announce(_profileLastResult);
  } catch (e) {
    fail("Could not reach the device service.");
  }
}

/* ---- Delete profile dialog ---- */

function openDeleteDialog() {
  const entry = backupEntry(selectedProfile());
  const name = entry ? entry.name : "";
  if (!name) {
    announce("No backup selected.", true);
    return;
  }
  _profileReturnFocus = document.getElementById("profile-delete-open");
  _baselineDeleteArmed = false;        // a fresh dialog always asks again (see onProfileDelete)
  setText("delete-dialog-name", name);
  setText("delete-error", "");
  document.getElementById("delete-dialog").showModal();
  // Cancel first: deleting cannot be undone, so the safe action is the one already focused.
  document.getElementById("delete-cancel-btn").focus();
}

async function onProfileDelete() {
  const name = selectedProfile();
  try {
    // _baselineDeleteArmed: the as-found backup does not go on the same click as any other profile.
    // The server refuses the first attempt and says why; that reason goes on the dialog's error
    // line (which is where a failure is already reported and read), and pressing Delete again is
    // the deliberate second step. Cancelling, or opening the dialog again, disarms it.
    // ONE DELETE FOR ONE BACKUP: the server takes the unit's own copy off the drive as well when
    // there is one, because a user who deleted "Tuesday" and still has 24 MB of it on the drive was
    // not told the truth.
    const res = await postJSON("/api/backup/delete", { name, force: _baselineDeleteArmed });
    if (!res.ok) {
      _baselineDeleteArmed = !!res.baseline;
      setText("delete-error", res.message || "Could not delete the backup.");
      document.getElementById("delete-cancel-btn").focus();
      return;
    }
    _baselineDeleteArmed = false;
    if (res.via === "show") {
      // The unit's half runs as a job, so the outcome arrives through the progress dialog rather
      // than from this reply, which only says the job started.
      _profileJobRunning = true;
      openProgressDialog("delete-dialog", "Deleting backup", "Deleting the backup.");
      const s = await pollProfileJob("/api/shows/status", "progress-dialog-step");
      finishProgressDialog(s.ok ? "Backup deleted" : "Delete failed", s.message || "Done.");
      await loadProfiles();
      return;
    }
    _profileLastResult = res.message || "Deleted.";
    await loadProfiles();
    closeProfileDialog("delete-dialog");
    announce(_profileLastResult);
  } catch (e) {
    setText("delete-error", "Could not reach the device service.");
  }
}

/* ---- First run: back up the unit as it was found ----

   The single most valuable thing this app can do on the day it is installed, because the unit has
   no software factory reset and the touchscreen wizard is not reachable by a screen reader: once we
   write anything, how the desk came is gone. Offered once per unit (server.baseline_status), never
   nagged, and declining still leaves the Profiles page able to capture one later. */

async function getBaselineStatus() {
  try {
    return await (await fetch("/api/baseline/status")).json();
  } catch (e) {
    return null;                       // no service, no offer; nothing to report and nothing lost
  }
}

async function maybeOfferBaseline() {
  let s = await getBaselineStatus();
  // The offer is tagged with the unit's HARDWARE ID, and only a status read publishes that (the
  // server side deliberately does no device I/O of its own). Landing on the Device page has already
  // done it; landing anywhere else has not, so pay for one read here rather than let a first run go
  // unasked because of which page the app happened to open on.
  if (s && !s.connected) {
    await loadStatus(false);
    s = await getBaselineStatus();
  }
  if (s && s.offer) openBaselineDialog(s);
}

function openBaselineDialog(s) {
  // This one is not opened by a page button, so send focus back to the heading of whatever route is
  // showing. returnProfileFocus's usual fallbacks are all on the Profiles page, which may well be
  // hidden right now, and focusing a hidden element drops focus to the document body - which
  // strands a screen reader at the top of the page with no idea what just happened.
  _profileReturnFocus = document.querySelector("section:not([hidden]) > h1[tabindex='-1']");
  _profileJobRunning = false;
  setText("baseline-device-name", s.device_name || "unit");
  setText("baseline-suggested-name", s.suggested_name || "");
  // The fallback is the BACKUP rate (measured), not the restore rate: a backup
  // only reads a mounted volume, a restore writes over transfer mode, and they differ by 30x.
  setText("baseline-pad-seconds", String(s.seconds_per_pad || 1));
  document.getElementById("baseline-include-pads").checked = true;
  document.getElementById("baseline-dialog").showModal();
  // The recommended action, and the safe one: it only reads the unit and writes a file here.
  document.getElementById("baseline-save-btn").focus();
}

async function onBaselineSave() {
  const includePads = document.getElementById("baseline-include-pads").checked;
  _profileJobRunning = true;
  openProgressDialog("baseline-dialog", "Backing up the unit as found",
                     includePads
                       ? "Saving the unit's settings, then each SMART pad in turn."
                       : "Saving the unit's settings.");
  try {
    const start = await postJSON("/api/baseline/save", { include_pads: includePads });
    if (!start.ok) {
      finishProgressDialog("Backup failed", start.message || "Could not start the backup.");
      return;
    }
    const s = await pollProfileJob("/api/profiles/save/status", "progress-dialog-step");
    finishProgressDialog(s.ok ? "Unit backed up" : "Backup failed", s.message || "Done.");
    await loadProfiles();
  } catch (e) {
    finishProgressDialog("Backup failed", "Could not reach the device service.");
  }
}

async function onBaselineSkip() {
  // Recorded server-side so it is asked ONCE and never becomes a nag at every launch.
  try { await postJSON("/api/baseline/skip", {}); } catch (e) { /* nothing to undo */ }
  closeProfileDialog("baseline-dialog");
  announce("No backup was taken. You can still take one from the Profiles page, though it will "
           + "then record the unit as it is now rather than as you found it.");
}

/* Add the pads to a baseline whose settings were captured without them. NOT a re-save: the server
   attaches the pads to the existing profile, so its settings stay the as-found ones. */
async function onBaselineAddPads() {
  _profileReturnFocus = document.getElementById("baseline-add-pads");
  _profileJobRunning = true;
  openProgressDialog("", "Adding SMART pads to the backup",
                     "Copying each pad's sound off the unit in turn.");
  try {
    const start = await postJSON("/api/baseline/add-pads", {});
    if (!start.ok) {
      finishProgressDialog("Could not add the pads", start.message || "Nothing was changed.");
      return;
    }
    const s = await pollProfileJob("/api/profiles/save/status", "progress-dialog-step");
    finishProgressDialog(s.ok ? "Pads added to the backup" : "Could not add the pads",
                         s.message || "Done.");
    await loadProfiles();
  } catch (e) {
    finishProgressDialog("Could not add the pads", "Could not reach the device service.");
  }
}

/* The Profiles page's one line about the as-found backup: whether there is one, what is in it, and
   the button to finish it off if the pads were skipped. Read on every visit to that page, because
   the answer changes when a backup is taken or deleted. */
async function refreshBaselinePanel() {
  const line = document.getElementById("baseline-state");
  const addBtn = document.getElementById("baseline-add-pads");
  if (!line || !addBtn) return;
  const s = await getBaselineStatus();
  const b = s && s.baseline;
  // WARNING ONLY WHEN SOMETHING IS MISSING, and it is the absence that is worth flagging: with no
  // as-found backup there is no way back at all, and with one that skipped its pads the original
  // pad sounds are backed up nowhere. A complete baseline is just a fact and reads as a plain hint.
  line.classList.remove("warn-text");
  if (!b) {
    line.classList.add("warn-text");
    line.textContent = s && s.connected
      ? "No as-found backup of this unit has been taken on this computer, so there is no way back "
        + "if a change goes wrong. Save one from Create a profile above."
      : "No as-found backup, and no unit connected to take one from.";
    addBtn.hidden = true;
    return;
  }
  // SHORTER THAN IT WAS, on purpose. The old line re-explained what a baseline IS on every visit
  // ("the record of how the unit was before this app changed anything, and it cannot be replaced by
  // an ordinary save"). That belongs in the page note and the Help glossary, which both carry it,
  // not in a status line read out every time the page is opened.
  const when = b.saved_at ? `, taken ${b.saved_at}` : "";
  if (b.pads) {
    line.textContent = `As-found backup: "${b.name}"${when}, with ${b.pads} SMART `
      + `${b.pads === 1 ? "pad" : "pads"}.`;
  } else {
    line.classList.add("warn-text");
    line.textContent = `As-found backup: "${b.name}"${when}, settings only. Its SMART pads were `
      + "not copied, so the pad sounds as they came are not backed up anywhere.";
  }
  addBtn.hidden = !!b.pads;
}

/* ---- Shared dialog plumbing (Escape gating + focus return) ---- */

// Wire one profile dialog: Escape is refused while its job runs, and on close focus goes back to
// the page button that opened it with the outcome left on the page as plain (unspoken) text.
function wireProfileDialog(id) {
  const dlg = document.getElementById(id);
  dlg.addEventListener("cancel", (ev) => {
    if (_profileJobRunning) ev.preventDefault();   // no dismiss path mid-job
  });
  dlg.addEventListener("close", () => {
    if (_profileHandingOver) return;         // the progress dialog is taking over; leave focus to it
    if (_profileLastResult) setText("profile-last-result", _profileLastResult);
    _profileLastResult = "";
    returnProfileFocus();
  });
}

/* Put focus back where it came from. Deleting the last profile disables the very button that
   opened the dialog, and focusing a disabled control silently drops focus to the document, which
   strands a screen reader at the top of the page. So fall back along the page: the dropdown, then
   Save, then the heading. */
function returnProfileFocus() {
  const fallbacks = [_profileReturnFocus,
                     document.getElementById("profile-select"),
                     document.getElementById("profile-save-open"),
                     document.getElementById("h-profiles")];
  for (const el of fallbacks) {
    if (el && !el.disabled) { el.focus(); return; }
  }
}

function closeProfileDialog(id) {
  // Tolerates "no dialog to close": a job started straight from a page button (adding pads to the
  // as-found backup) hands over to the progress dialog with nothing to hand over FROM.
  const dlg = id ? document.getElementById(id) : null;
  if (dlg && dlg.open) dlg.close();
}

/* ---- Recording -------------------------------------------------------
   THE ONE LIVE REGION RULE, and this page is where it matters most. Everything spoken here goes
   through `rec-state` and nothing else. An earlier draft had a second status line for the result of
   a button press, which meant pressing Start fired TWO regions at once (the outcome and the new
   state) and a screen reader read one over the other. `announce()` REPLACES the global live region
   rather than queueing, so that is a trap the whole app shares; here the answer is that there is
   only ever one thing being said.

   `setRecState` clears the region before writing, so pressing "Check the recorder" twice speaks
   twice even though the words did not change - a status region that only fires on a DIFFERENT
   string would leave that button silently doing nothing.

   THE POLL is the cheap endpoint (`/api/recorder/live`), answered from the push listener with no
   device read at all, so it can run every couple of seconds while the page is open. It speaks only
   when the STATE CHANGES, never on a tick: this is the page you might leave open through a whole
   session, and a status line that talks every two seconds would be turned off within the minute.
   A change we did not make (someone pressed the record button on the unit) also refreshes the page
   properly, since a finished recording has to appear in the list. */

const REC_POLL_MS = 2000;
let recordingModel = null;
let recordingPoll = null;
let recordingSeenState = null;
const recordingCtl = { multitrack: null, bypass: null, bypassHint: null,
                       usbMultitrack: null, usbBypass: null, usbBypassHint: null };

function setRecState(text) {
  const el = document.getElementById("rec-state");
  if (!el) return;
  el.textContent = "";
  window.setTimeout(() => { el.textContent = text; }, 50);
}

async function loadRecording() {
  setText("rec-state", "Reading the unit...");
  let d;
  try {
    const resp = await fetch("/api/recorder");
    d = await resp.json();
  } catch (e) {
    setRecState("Could not reach the AccessCaster service.");
    return;
  }
  if (!d.ok) {
    setRecState(d.message || "Could not read the recorder.");
    return;
  }
  loadRecordingFrom(d);
  setRecState(d.message || "");
  startRecordingPoll();
}

/* Draw the whole page from one payload: the page read's, or a command's reply, which carries the
   same shape (server._recorder_payload). Speaks nothing; the caller words the outcome. */
function loadRecordingFrom(d) {
  recordingModel = d;
  recordingSeenState = d.state;
  fillRecordingDest(d);
  fillRecordingList(d);
  applyTransportLabels(d.state);
  applyRecordingWritable(d);
  applyRecordingUsable(d);
  applyAnnouncerWarning(d);
}

/* The record button's label follows the state, the way a transport control normally does:
   Start recording, then Pause, then Resume. Two buttons rather than three.
   The label is the only thing that changes - the button always posts the action it currently
   names, so what you heard is what you get. A label change is never the only signal that
   something happened: the live region above announces every outcome. */
// THE LABELS ARE THE BUTTON'S ONLY NAME. Drop a key from this table and
// `btn.textContent = spec.label` assigns undefined, so the one button you may need in a hurry has
// no accessible name at all, and nothing on screen looks wrong. If a label needs cutting, cut the
// WORDS, never the key.
const REC_TRANSPORT = {
  0: { label: "Start recording", command: "start" },      // ready
  1: { label: "Resume recording", command: "start" },     // paused
  2: { label: "Pause", command: "pause" },                // recording
  3: { label: "Start recording", command: "start" },      // no destination: it will say so
};

/* A control that is not usable right now.
   aria-disabled, NOT the `disabled` attribute, and that is deliberate: a natively disabled button
   leaves the tab order entirely, so Stop would VANISH between takes and reappear later, which is
   far more disorienting than hearing "unavailable" in the place you expect it. It also keeps the
   reason reachable - pressing it says why rather than doing nothing at all. */
function setAvailable(el, on, why) {
  if (!el) return;
  if (on) {
    el.removeAttribute("aria-disabled");
    delete el.dataset.offReason;
  } else {
    el.setAttribute("aria-disabled", "true");
    el.dataset.offReason = why || "";
  }
}

function unavailableReason(el) {
  if (!el || !el.hasAttribute("aria-disabled")) return null;
  return el.dataset.offReason || "That is not available at the moment.";
}

/* Refuse a CHANGE on an unavailable control: say why, once for a burst. Returns the reason, or null
   when the control is available and the write should go ahead.

   ONCE FOR A BURST is the whole point, and it is why this is not just a call to announce. A range
   slider and a closed dropdown each fire their change event on EVERY arrow key, so announcing every
   one would talk over the line before it and bury the value the screen reader is reading back. A
   button, which fires once, is unaffected by the wait. */
const _refuseTimers = {};
function refuseChange(el) {
  const why = unavailableReason(el);
  if (why === null) return null;
  const key = (el && el.id) || "anon";
  window.clearTimeout(_refuseTimers[key]);
  _refuseTimers[key] = window.setTimeout(() => announce(why, true), 400);
  return why;
}

function applyTransportLabels(state) {
  const btn = document.getElementById("rec-start");
  if (!btn) return;
  const spec = REC_TRANSPORT[state] || REC_TRANSPORT[0];
  btn.textContent = spec.label;
  btn.dataset.command = spec.command;
  // Stop is only meaningful once something is running. One function decides both the label and
  // what is usable, so the two can never disagree about which state the page thinks it is in.
  const running = state === 1 || state === 2;
  setAvailable(document.getElementById("rec-stop"), running, "Nothing is recording.");
  // A marker needs a POSITION, so it is only offered while something is actually running. Paused
  // counts as not running here: nothing is being recorded, so there is nowhere to put one.
  setAvailable(document.getElementById("rec-marker"), state === 2,
    state === 1 ? "Nothing is being recorded while it is paused."
                : "Markers can only be dropped while something is recording.");
}

/* The wording is built SERVER-SIDE, in one place, for both the page read and the cheap live read
   (server._recorder_sentence). Do NOT compose sentences here: bolting the elapsed time onto the end
   of the server's sentence puts the parts in the wrong order the moment that sentence grows a
   second clause - "Recording to the USB drive. There is room for 88 hours, for 4 minutes." */

/* THE STORAGE DETAIL IS NOT ON THIS PAGE. Listing every volume here means the unit's pad storage
   and an empty card slot, which is the last reading anyone wants while working. Where a recording
   goes is in the status sentence and in the reply when you press record; the full picture is on
   Device > Hardware, which is where you go to look at storage deliberately. */
function fillRecordingDest(d) {
  fillRecordingSettings(d);
}

/* THE TWO PRE-TAKE DECISIONS, AND ONE APPLY BETWEEN THEM. What a take CONTAINS (multitrack) and
   what it is SAVED AS (WAV or MP3) are really one decision - neither is fixable after the take -
   so they read as a pair and share one button named for the pair. Two Apply buttons a Tab apart
   say less and cost more stops.

   THE RAW SWITCH STAYS OUTSIDE IT, because it is INSTANT: a checkbox cannot be changed by arrowing
   past it, so it needs no staging. It sits after the Apply, which also means it becomes available
   exactly when you reach it.

   Multitrack's write goes to /api/routing/multitrack, which owns the selector and its device gate;
   the format's goes to /api/recorder/format. Both are gated on the same "output_write" cap. */
const REC_SETTING_WRITE = {
  "rec-multitrack-mode": {
    path: "/api/routing/multitrack",
    body: (v) => ({ mode: v }),
    // WHAT DEPENDS ON IT, re-run at the WRITE. The raw switch is available only while a multitrack
    // mode is chosen, so without this it stays keyed to the mode the page was last READ with.
    after: (v) => {
      if (recordingModel) recordingModel.multitrack = v;
      applyBypass(recordingCtl.bypass, recordingCtl.bypassHint, recordingModel || {}, null, v);
    },
  },
  "rec-format-mode": {
    path: "/api/recorder/format",
    body: (v) => ({ value: v }),
  },
};

/* Apply every dropdown that is actually waiting, in page order, and report what happened ONCE.
   Stops at the first failure rather than carrying on: if the desk refused one write there is no
   reason to trust the next, and two failures spoken together say nothing useful. */
async function onApplyRecordingSettings(pending, btn) {
  setBusy(btn, true);
  const said = [];
  try {
    for (const sel of pending) {
      const spec = REC_SETTING_WRITE[sel.id];
      if (!spec) continue;
      const value = parseInt(sel.value, 10);
      const res = await postJSON(spec.path, spec.body(value));
      said.push(res.message || "Done.");
      if (!res.ok) {
        setRecState(said.join(" "));
        announce(said.join(" "), true);
        return;
      }
      setStagedValue(sel, value, null);
      if (spec.after) spec.after(value);
    }
    setRecState(said.join(" "));
    announce(said.join(" ") || "Nothing to apply.");
  } catch (err) {
    announce("Could not reach the device service.", true);
  } finally {
    setBusy(btn, false);
  }
}

function fillRecordingSettings(d) {
  const host = document.getElementById("rec-multitrack");
  if (!host) return;
  const modes = d.multitrack_modes || [];
  const formats = d.format_options || [];
  // Keyed on BOTH option lists. Either is only ever missing if a caller passed a partial payload,
  // is what empties these controls on every transport press.
  if (!modes.length || !formats.length) return;
  rebuildIfChanged(host, JSON.stringify([modes, formats]), (box) => {
    const apply = makeStagedApply("rec-settings-apply", "Apply recording settings",
                                  onApplyRecordingSettings);

    const mt = makeStagedSelect("rec-multitrack-mode", "What to record", modes,
                                { applyButton: apply });
    const mtHint = document.createElement("span");
    mtHint.className = "hint";
    mtHint.id = "rec-multitrack-hint";
    // THE STANDING NOTE IS IN THE STATIC HINT, not in the changing one, or nothing says an Apply
    // is still needed. A static description is read when you arrive at the control and never again
    // as you arrow, so it costs nothing per option; the "Not applied yet" line changes and is
    // deliberately NOT described-by, for exactly that reason.
    mtHint.textContent = (d.multitrack_hint || "")
      + " Nothing is sent until you press Apply recording settings.";
    mt.sel.setAttribute("aria-describedby", "rec-multitrack-hint");
    mt.row.appendChild(mtHint);
    recordingCtl.multitrack = mt.sel;
    box.appendChild(mt.row);

    const fmt = makeStagedSelect("rec-format-mode", "Save recordings as", formats,
                                 { applyButton: apply });
    const fmtHint = document.createElement("span");
    fmtHint.className = "hint";
    fmtHint.id = "rec-format-hint";
    fmtHint.textContent = (d.format_hint || "")
      + " Nothing is sent until you press Apply recording settings.";
    fmt.sel.setAttribute("aria-describedby", "rec-format-hint");
    fmt.row.appendChild(fmtHint);
    recordingCtl.format = fmt.sel;
    box.appendChild(fmt.row);

    const applyRow = document.createElement("p");
    applyRow.appendChild(apply);
    box.appendChild(applyRow);
    recordingCtl.settingsApply = apply;

    // THE RAW SWITCH, instant and last. It only ever means anything alongside a multitrack choice,
    // and reading them one after the other is how the decision is actually made.
    const bp = makeSwitchRow("rec-bypass", "Record the separate tracks raw", (input) => {
      sendBypass("/api/recorder/processing-bypass", input, setRecState);
    });
    const bpHint = document.createElement("span");
    bpHint.className = "hint";
    bpHint.id = "rec-bypass-hint";
    bp.input.setAttribute("aria-describedby", "rec-bypass-hint");
    bp.row.appendChild(bpHint);
    recordingCtl.bypass = bp.input;
    recordingCtl.bypassHint = bpHint;
    box.appendChild(bp.row);
  });

  if (recordingCtl.multitrack && d.multitrack !== null && d.multitrack !== undefined)
    setStagedValue(recordingCtl.multitrack, d.multitrack, null);
  if (recordingCtl.format && d.format !== null && d.format !== undefined)
    setStagedValue(recordingCtl.format, d.format, null);

  // ONE GATE FOR THE PAIR: both writes need the same "output_write" cap, so a device without it
  // blocks the shared button once rather than each dropdown separately.
  if (recordingCtl.settingsApply) {
    if (d.multitrack_writable === false) {
      setBusy(recordingCtl.multitrack, true);
      setBusy(recordingCtl.format, true);
      blockStaged(recordingCtl.settingsApply,
                  "Changing the recording settings is not enabled on this device yet.");
    } else {
      blockStaged(recordingCtl.settingsApply, "");
    }
    markStaged(recordingCtl.multitrack, recordingCtl.settingsApply);
  }

  applyBypass(recordingCtl.bypass, recordingCtl.bypassHint, d,
              d.processing_bypass, d.multitrack);
}


/* WHAT HAPPENS TO SPOKEN AUDIO DURING A TAKE.
   Our own announcements are switched off for the duration and switched back on afterwards, in the
   server, so this is only a statement of fact. A button that moved the speech to the computer's
   speakers instead would not help: that only takes it off the USB return, and anything audible in
   the room reaches the mics.

   What is left is the part the app CANNOT silence: the screen reader, and anything else the
   computer plays, while the unit is the default playback device. Said only when that is actually
   true, so it does not become a warning nobody hears. */
function applyAnnouncerWarning(d) {
  const warn = document.getElementById("rec-announcer-warn");
  if (!warn) return;
  const a = d.announcer || {};
  // ONE LINE, and only when it applies. The screen reader half of this belongs in Help, not here:
  // anyone recording with a screen reader already runs a second pair of headphones for it and sets
  // the announcement output to match, on the Device page. Repeating that here every time would be
  // a lecture, not a warning.
  warn.textContent = (a.announcing && a.mute_while_recording)
    ? "Announcements are switched off while recording and back on when you stop." : "";
  warn.hidden = !warn.textContent;
  const box = document.getElementById("rec-mute-announcer");
  if (box) box.checked = a.mute_while_recording !== false;
}

/* A unit with nowhere to record.
   NEVER HIDE THE WHOLE PAGE, and the reason only a screen reader shows: with every control gone
   the page has NO focusable content at all, so Tab from the heading leaves the page entirely and it
   reads as broken. Same fault as an empty tab panel, in a different costume.
   So nothing is hidden. The transport is marked unavailable WITH THE REASON, which is what the
   page already does for Stop, the settings stay usable because they are worth setting before the
   drive goes in, and the state line says what to do about it. */
function applyRecordingUsable(d) {
  if (d.state !== 3) return;
  const why = "The unit has nowhere to record to. Put a memory card in, or a USB drive in the "
    + "second USB port.";
  ["rec-start", "rec-stop", "rec-marker"].forEach((id) => {
    setAvailable(document.getElementById(id), false, why);
  });
}


/* THE SWITCH GOES UNAVAILABLE UNTIL MULTITRACK IS CHOSEN. Leaving it live because the write lands
   either way and the unit honours it later is the wrong call: an operable switch that changes
   nothing you can hear is indistinguishable from one that is broken, and the person flipping it
   has no way to tell which they have.

   `setAvailable`, NOT the `disabled` attribute. A disabled control leaves the tab order, so the
   switch would vanish and reappear as the mode changed, and anyone sitting on it would be thrown
   somewhere else. aria-disabled keeps it in place, announced as unavailable, and carrying the
   reason - which `onBypassToggle` then speaks when it is pressed anyway. */
function applyBypass(input, hint, d, value, mode) {
  if (!input) return;
  if (value !== null && value !== undefined) input.checked = Boolean(value);
  if (d.multitrack_writable === false) setBusy(input, true);
  // `!mode` covers 0, null and undefined together: off, unknown and not-read-yet all mean the
  // separate tracks are not being made, which is the only thing this switch is about.
  const inert = !mode;
  setAvailable(input, !inert, d.bypass_needs_multitrack || "");
  if (hint) hint.textContent = inert ? (d.bypass_needs_multitrack || "") : (d.bypass_hint || "");
}

/* Shared by BOTH raw switches, which sit on different pages: the recorder's on Recording and
   the USB stream's on Routing > USB audio. Everything page-specific is passed in - where to post,
   the mode the caveat depends on, the caveat's own words, and how that page speaks - so neither
   page owns the other's wording. */
async function sendBypass(path, input, say) {
  // PRESSING AN UNAVAILABLE SWITCH SAYS WHY, and puts the switch back. Silence would be
  // indistinguishable from the app having missed the press, and a checkbox that stays flipped
  // while nothing was written is worse still: it would report a state the unit is not in. Same
  // rule the transport buttons follow.
  const why = unavailableReason(input);
  if (why !== null) {
    input.checked = !input.checked;
    say(why);
    return;
  }
  const res = await postJSON(path, { on: input.checked });
  say(res.message || "Setting sent.");
}

function fillRecordingList(d) {
  const list = document.getElementById("rec-list");
  if (!list) return;
  const items = d.recordings || [];
  const key = JSON.stringify(items.map((r) => [r.uid, r.name, r.when, r.duration]));
  rebuildIfChanged(list, key, (ul) => {
    if (!items.length) {
      const li = document.createElement("li");
      li.textContent = d.state === 3
        ? "Nothing to list: there is no card or drive in the unit."
        : "No recordings on the unit.";
      ul.appendChild(li);
      return;
    }
    items.forEach((r) => {
      const li = document.createElement("li");
      li.textContent = r.name + ", " + r.duration + (r.when ? ", recorded " + r.when : "");
      ul.appendChild(li);
    });
  });
  fillRecordingDeleteChoices(d);
  fillStorageChoices(d);
}

/* The card slot and the USB drive, never the internal pad storage (`can_record` is false for it).
   The option text carries the state, so the choice can be made from the dropdown alone. */
function storageVolumes(d) {
  return ((d && d.volumes) || []).filter((v) => v.can_record && !v.live_view);
}

function storageOptionText(v) {
  if (v.ejected) return v.label + ": ejected";
  if (!v.inserted) return v.label + ": empty";
  return v.label + ": " + v.free_mb + " MB free of " + v.capacity_mb + " MB";
}

function fillStorageChoices(d) {
  const sel = document.getElementById("rec-vol-select");
  if (!sel) return;
  const vols = storageVolumes(d);
  const keep = sel.value;
  fillOptions("rec-vol-select", vols.map((v) => ({ value: v.index, label: storageOptionText(v) })));
  // Default to the one with something in it, which is nearly always the one wanted.
  const inUse = vols.find((v) => v.inserted || v.ejected);
  if (keep !== "" && vols.some((v) => String(v.index) === keep)) sel.value = keep;
  else if (inUse) sel.value = String(inUse.index);
  applyStorageButtons(d);
  // Nothing in either slot and nothing ejected: one line instead of a dropdown of "empty" and two
  // buttons that can do nothing. Focus that was in the block goes to the section's heading.
  const box = document.getElementById("rec-vol-box");
  const none = document.getElementById("rec-vol-none");
  if (box && none) {
    const hadFocus = box.contains(document.activeElement);
    box.hidden = !inUse;
    none.hidden = !!inUse;
    if (!inUse && hadFocus) {
      const h = document.getElementById("rec-h-storage");
      if (h) h.focus();
    }
  }
}

function selectedStorage(d) {
  const sel = document.getElementById("rec-vol-select");
  return storageVolumes(d).find((v) => sel && String(v.index) === sel.value) || null;
}

function applyStorageButtons(d) {
  const v = selectedStorage(d);
  const eject = document.getElementById("rec-vol-eject");
  const erase = document.getElementById("rec-vol-erase");
  const cannot = d && d.storage_writable === false
    ? "AccessCaster cannot eject or erase storage on this unit." : null;
  const running = d && (d.state === 1 || d.state === 2);
  if (eject) {
    // THE LABEL IS THE BUTTON'S ONLY NAME, so it is always set.
    eject.textContent = v && v.ejected ? "Reconnect" : "Eject";
    eject.dataset.on = v && v.ejected ? "0" : "1";
    setAvailable(eject, !cannot && !!v && (v.ejected || (v.inserted && !running)),
      cannot || (!v ? "Choose a card or drive first." : running ? "Stop the recording first."
        : "Nothing is in it."));
  }
  if (erase) {
    setAvailable(erase, !cannot && !!v && v.inserted && v.mounted && !running,
      cannot || (!v ? "Choose a card or drive first." : running ? "Stop the recording first."
        : v.ejected ? "Reconnect it before erasing." : "Nothing is in it."));
  }
}

async function onStorageEject(btn) {
  const why = unavailableReason(btn);
  if (why !== null) { setRecState(why); return; }
  const v = selectedStorage(recordingModel);
  if (!v) return;
  const on = btn.dataset.on !== "0";
  setBusy(btn, true);
  setRecState(on ? "Ejecting, please wait." : "Reconnecting, please wait.");
  try {
    const res = await postJSON("/api/storage/eject", { index: v.index, on: on });
    setRecState(res.message || "Done.");
    if (res.recorder) { recordingModel = res.recorder; loadRecordingFrom(res.recorder); }
  } catch (e) {
    setRecState("Could not reach the AccessCaster service.");
  } finally { setBusy(btn, false); applyStorageButtons(recordingModel); }
}

async function onStorageErase(btn) {
  const why = unavailableReason(btn);
  if (why !== null) { setRecState(why); return; }
  const v = selectedStorage(recordingModel);
  if (!v) return;
  const ok = await confirmAction("Erase the " + v.spoken + "?",
    "Everything on it is deleted, recordings and any other files. It cannot be undone, and "
    + "AccessCaster has no copy.", "Erase");
  if (!ok) { setRecState("Cancelled. Nothing was erased."); return; }
  setBusy(btn, true);
  setRecState("Erasing the " + v.spoken + ". This takes up to a minute.");
  try {
    const res = await postJSON("/api/storage/erase", { index: v.index });
    setRecState(res.message || "Done.");
    if (res.recorder) { recordingModel = res.recorder; loadRecordingFrom(res.recorder); }
  } catch (e) {
    setRecState("Could not reach the AccessCaster service.");
  } finally { setBusy(btn, false); applyStorageButtons(recordingModel); }
}

/* The delete dropdown. Same list, but a CONTROL: a list item is not one and must not become one.
   The option text repeats the name, length and date so the choice can be made from the dropdown
   alone, without reading the list above it first. */
function fillRecordingDeleteChoices(d) {
  const sel = document.getElementById("rec-delete-select");
  if (!sel) return;
  const items = d.recordings || [];
  fillOptions("rec-delete-select", items.map((r) => ({
    value: r.uid,
    label: r.name + ", " + r.duration + (r.when ? ", recorded " + r.when : ""),
  })));
  const btn = document.getElementById("rec-delete-btn");
  const none = !items.length;
  // Two separate reasons, and the page must not merge them: nothing to delete is a passing state,
  // while "this device cannot" or "not proven yet" is a standing one the server explains.
  setAvailable(btn, !none && d.can_delete !== false,
    none ? "There are no recordings on the unit." : (d.delete_note || "Deleting is not available."));
  // NOTHING TO DELETE HIDES THE WHOLE BLOCK; the list above already says why. If focus was in it
  // (the last recording has just gone), it moves to the list's heading rather than being dropped
  // to the top of the document.
  const box = document.getElementById("rec-delete-box");
  if (box) {
    const hadFocus = box.contains(document.activeElement);
    box.hidden = none;
    if (none && hadFocus) {
      const h = document.getElementById("rec-h-list");
      if (h) h.focus();
    }
  }
}

/* Everything here reports through setRecState, the page's ONE live region. No second region and no
   announce(): this page speaks in one voice, which is why a delete has no status line of its own. */
async function onDeleteRecording() {
  const btn = document.getElementById("rec-delete-btn");
  const why = unavailableReason(btn);
  if (why !== null) { setRecState(why); return; }
  const sel = document.getElementById("rec-delete-select");
  const chosen = sel.options[sel.selectedIndex];
  if (!chosen) return;
  const ok = await confirmAction("Delete " + chosen.textContent + "?",
    "This removes the recording from the unit's card or drive. It cannot be undone, and "
    + "AccessCaster has no copy of it.", "Delete recording");
  if (!ok) { setRecState("Cancelled. The recording was not deleted."); return; }
  setBusy(btn, true);
  setRecState("Deleting, please wait.");
  try {
    const res = await postJSON("/api/recorder/delete", { uid: parseInt(sel.value, 10) });
    setRecState(res.message || "Done.");
    if (res.recorder) { recordingModel = res.recorder; fillRecordingList(res.recorder); }
  } catch (e) {
    setRecState("Could not reach the AccessCaster service.");
  } finally { setBusy(btn, false); }
}

/* A unit we can read but must not drive (the Duo: the record command's address was read off a
   Pro II and has never been seen there). The buttons stay in the page and in the tab order, and
   say why when pressed, rather than vanishing and leaving the page looking broken.
   Runs AFTER applyTransportLabels, since a unit that cannot be driven at all overrules any
   per-state judgement about which button is currently useful. */
function applyRecordingWritable(d) {
  if (d.writable === false) {
    const why = "AccessCaster can read the recorder on this unit but not drive it.";
    ["rec-start", "rec-stop", "rec-marker"].forEach((id) => {
      setAvailable(document.getElementById(id), false, why);
    });
  }
}

function startRecordingPoll() {
  stopRecordingPoll();
  recordingPoll = window.setInterval(pollRecording, REC_POLL_MS);
}

function stopRecordingPoll() {
  if (recordingPoll !== null) {
    window.clearInterval(recordingPoll);
    recordingPoll = null;
  }
}

async function pollRecording() {
  let live;
  try {
    const resp = await fetch("/api/recorder/live");
    live = await resp.json();
  } catch (e) {
    return;                       // a dropped poll is not worth saying anything about
  }
  if (!live.ok || live.state === null || live.state === undefined) return;
  if (live.state === recordingSeenState) return;      // nothing changed: stay quiet
  recordingSeenState = live.state;
  // The button follows the unit even when the change came FROM the unit, so pressing REC on the
  // desk leaves this page offering Pause rather than Start.
  applyTransportLabels(live.state);
  applyRecordingUsable({ state: live.state });
  // Somebody pressed record on the unit itself, or a recording finished. Re-read properly: the
  // list and the free space have both moved, and this is the only way they get updated.
  loadRecording();
}

async function onRecordCommand(cmd, btn) {
  if (isBusy(btn)) return;
  // Pressing an unavailable control SAYS why. Silence would be indistinguishable from the app
  // having missed the press.
  const why = unavailableReason(btn);
  if (why !== null) {
    setRecState(why);
    return;
  }
  setBusy(btn, true);
  setRecState(cmd === "stop" ? "Stopping..." : cmd === "pause" ? "Pausing..." : "Starting...");

  let res;
  try {
    res = await postJSON("/api/recorder/command", { command: cmd });
  } catch (e) {
    setBusy(btn, false);
    setRecState("Could not reach the AccessCaster service.");
    return;
  }
  setBusy(btn, false);
  if (res.recorder) {
    recordingModel = Object.assign({}, res.recorder, { message: res.message });
    recordingSeenState = res.state;
    fillRecordingDest(recordingModel);
    fillRecordingList(recordingModel);
  }
  // From the state the unit REPORTED, not from the command we sent: a refused command must not
  // leave the button offering the next step of something that never started.
  if (res.state !== null && res.state !== undefined) {
    applyTransportLabels(res.state);
    applyRecordingUsable({ state: res.state });
  }
  setRecState(res.message || "Done.");
}

async function onRecordCheck(btn) {
  if (isBusy(btn)) return;
  setBusy(btn, true);
  let live = null;
  try {
    const resp = await fetch("/api/recorder/live");
    live = await resp.json();
  } catch (e) { /* fall back to the last full read below */ }
  setBusy(btn, false);
  // The cheap endpoint carries the whole sentence, elapsed time included, at no device cost. The
  // last full read is only the fallback for the moment before one has happened.
  if (live && live.ok && live.message) {
    setRecState(live.message);
    return;
  }
  setRecState(recordingModel ? (recordingModel.message || "") : "The unit has not been read yet.");
}

async function onDropMarker(btn) {
  if (isBusy(btn)) return;
  const why = unavailableReason(btn);
  if (why !== null) {
    setRecState(why);
    return;
  }
  setBusy(btn, true);
  let res;
  try {
    res = await postJSON("/api/recorder/marker", {});
  } catch (e) {
    setBusy(btn, false);
    setRecState("Could not reach the AccessCaster service.");
    return;
  }
  setBusy(btn, false);
  setRecState(res.message || "Marker sent.");
}

function wireRecording() {
  const start = document.getElementById("rec-start");
  const stop = document.getElementById("rec-stop");
  const marker = document.getElementById("rec-marker");
  const check = document.getElementById("rec-check");
  const mute = document.getElementById("rec-mute-announcer");
  if (marker) marker.addEventListener("click", () => onDropMarker(marker));
  if (mute) mute.addEventListener("change", (ev) => {
    const input = ev.currentTarget;
    sendToggle("/api/recorder/announcer-mute", { on: input.checked }, input, input.checked);
  });
  // Reads the command off the button at press time, so it always sends the action it is currently
  // labelled with rather than whatever the page thought the state was when it was wired.
  if (start) start.addEventListener("click",
    () => onRecordCommand(start.dataset.command || "start", start));
  if (stop) stop.addEventListener("click", () => onRecordCommand("stop", stop));
  if (check) check.addEventListener("click", () => onRecordCheck(check));
  const del = document.getElementById("rec-delete-btn");
  if (del) del.addEventListener("click", onDeleteRecording);
  const volSel = document.getElementById("rec-vol-select");
  if (volSel) volSel.addEventListener("change", () => applyStorageButtons(recordingModel));
  const volEject = document.getElementById("rec-vol-eject");
  if (volEject) volEject.addEventListener("click", () => onStorageEject(volEject));
  const volErase = document.getElementById("rec-vol-erase");
  if (volErase) volErase.addEventListener("click", () => onStorageErase(volErase));
}

/* ---- Help & About page -----------------------------------------------
   The simplest page: two panes of prose (What it can control, Help) with not one control between
   them and nothing read from the device. What the app cannot do is a short list in the page's own
   intro, ABOVE the strip, rather than a third pane.
   So there is no onSelect, no debounce and no loader; the strip is built once at startup and that
   is the whole page.
   Because the panes hold no focusable content, setPanelTabStop() gives the PANEL tabindex="0" and
   Tab from the strip lands inside it rather than sailing past to the next thing in the document.
   That is the Device page's Network/Technical fault, already fixed in the shared helper, and this
   page is the one where it applies to every tab rather than two of them. */
let aboutTabs = null;

/* THE DONATION LINK. Set this to the real page and the Support tab wires itself up; leave it empty
   and the tab says, in as many words, that there is nowhere to send anything yet.

   It is deliberately ONE constant rather than a setting or a fetch: it is a fixed fact about the
   project, not something a user configures, and a hardcoded URL that is obviously a placeholder is
   easier to find later than a config key nobody remembers. */
const SUPPORT_URL = "https://www.slightperception.com/support";   // the donation page

/* An anchor with no href is not focusable and not announced as a link, which is exactly what we
   want while there is nowhere to go: no dead control parked in the tab order. Adding the href is
   what turns it into a real link, so this is the only place that decides. */
function wireSupport() {
  const link = document.getElementById("support-link");
  const note = document.getElementById("support-not-yet");
  if (!link) return;
  const url = (SUPPORT_URL || "").trim();
  if (!url) return;                       // markup already says it is not set up
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";       // never hand the opener to an external page
  link.hidden = false;
  if (note) note.hidden = true;
}

function wireAboutTabs() {
  aboutTabs = buildStaticTabs("about", "about-panel");
  wireSupport();
}


/* ---- Auto gain ---------------------------------------------------------
   Talk for half a minute and the gain sets itself. The server does the measuring (autogain.py and
   server._autogain_run); this drives the three dialogs and the polling.

   THREE DIALOGS, NOT ONE, and it is not decoration: NVDA never reads content revealed INSIDE an
   already-open dialog, so a single dialog changing from "about to start" through "talk now" to
   "here is the result" would leave a screen-reader user hearing only the first of the three.

   NOTHING IS SPOKEN WHILE IT LISTENS except the countdown, and even that only at the start and the
   end. The microphone being measured can hear whatever the computer plays, so a running commentary
   would set the gain partly from the app's own voice. */

// Polled often, because the gap between the unit starting to listen and the page saying so is
// dead time the user is not talking through. The status endpoint reads no device state at all, so
// this costs nothing.
const AG_POLL_MS = 400;
let agPoll = null;
let agChannel = null;
let agReturnFocus = null;
let agSaidTalk = null;          // which talking phase has had its instruction said
let agLastCount = null;
let agFinishing = false;        // guards against two polls both finishing the same run

/* ---- A talking level check, on ONE channel -------------------------------
   Not to be confused with the Device > Levels check, which meters a Windows STREAM: every stream
   this desk offers is a mix, so that one answers "something is reaching the computer" and never
   "Mic 2 is right", which is the question you have while setting up. The server isolates the channel on a
   temporary Chat mix and corrects for the fader, so the number is about the channel
   (server.action_channel_level_check).

   NOTHING GOES IN THE PAGE'S LIVE REGION WHILE IT RUNS. The check speaks the level itself every
   second and a half; a second voice saying anything at all lands on top of it. The button's own
   label is the state, and a beep marks the start and the end. */
let chanCheckTimer = null;

function chanCheckSetLabel(name, running) {
  const btn = document.getElementById("levelcheck-" + name);
  if (btn) btn.textContent = running ? "Stop checking" : "Check this channel's level";
}

function buildLevelCheckButton(ch) {
  const p = document.createElement("p");
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "secondary";
  btn.id = "levelcheck-" + ch.name;
  btn.textContent = "Check this channel's level";
  btn.addEventListener("click", () => onLevelCheck(ch));
  p.appendChild(btn);
  // Not attached with aria-describedby: this would then be read in full every time the button took
  // focus, and it is the same two facts every time. On the page to be read once.
  const hint = document.createElement("p");
  hint.className = "hint";
  hint.textContent = "Talk for half a minute and it speaks this channel's level as you go, then "
    + "the loudest it got. The fader position is taken out of the reading.";
  p.appendChild(hint);
  return p;
}

async function onLevelCheck(ch) {
  const btn = document.getElementById("levelcheck-" + ch.name);
  setBusy(btn, true);
  try {
    const res = await postJSON("/api/channels/level-check", { name: ch.name });
    if (chanCheckTimer !== null) window.clearTimeout(chanCheckTimer);
    chanCheckTimer = null;
    chanCheckSetLabel(ch.name, !!res.running);
    if (res.running) {
      // Only what the button does not already say: a Windows mute that had to be cleared. The
      // start itself is a beep, deliberately.
      if (res.message) announce(res.message);
      // The server ends it on its own timer; this only puts the label back a beat later.
      chanCheckTimer = window.setTimeout(() => {
        chanCheckTimer = null;
        chanCheckSetLabel(ch.name, false);
      }, ((res.seconds || 30) + 1) * 1000);
    } else {
      // Stopped early, or refused. Either way it is no longer speaking, so this is safe to say.
      announce(res.message || "Done.", !res.ok);
    }
  } catch (e) {
    announce("Could not reach the app service.", true);
  } finally {
    setBusy(btn, false);
  }
}

function buildAutoGainButton(ch) {
  const p = document.createElement("p");
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "secondary";
  btn.id = "autogain-" + ch.name;
  btn.textContent = "Set gain automatically";
  btn.addEventListener("click", () => onAutoGain(ch));
  p.appendChild(btn);
  return p;
}

function agDialog(id) { return document.getElementById(id); }

function agClose(id) {
  const d = agDialog(id);
  if (d && d.open) d.close();
}

async function onAutoGain(ch) {
  agChannel = ch.name;
  agReturnFocus = document.activeElement;
  setText("ag-confirm-desc",
    "You will be asked to talk normally, as you would on air, for 30 seconds, then for ten "
    + "seconds more so it can check its work. This switches " + ch.label + " processing off while "
    + "it listens and puts it back afterwards, sets the Windows recording level to no change, and "
    + "rewrites the gain. The gain can be undone at the end.");
  const dlg = agDialog("ag-confirm-dialog");
  dlg.showModal();
  document.getElementById("ag-confirm-cancel").focus();
}

async function agStart() {
  // THE HAND-OVER BETWEEN DIALOGS HAPPENS IN ONE TICK, with no await in the middle, and that is
  // the whole point of the ordering here. Closing a modal returns focus to whatever opened it, so
  // an `await` between closing the confirm dialog and opening the progress one leaves focus on the
  // PAGE - and a screen reader then reads the page from the top, so ten seconds of a thirty-second
  // take are gone before "talk now" is heard. The request is sent AFTER both dialogs have changed
  // hands.
  agSaidTalk = null;
  agLastCount = null;
  agFinishing = false;
  setText("ag-progress-desc", "Getting ready. Do not talk yet.");
  agClose("ag-confirm-dialog");
  const dlg = agDialog("ag-progress-dialog");
  dlg.showModal();
  document.getElementById("ag-progress-stop").focus();
  agStopPoll();

  let res;
  try {
    res = await postJSON("/api/channels/autogain", { name: agChannel });
  } catch (e) {
    agClose("ag-progress-dialog");
    announce("Could not reach the app service.", true);
    return;
  }
  if (!res.ok) {
    agClose("ag-progress-dialog");
    announce(res.message || "Auto gain could not start.", true);
    return;
  }
  agPoll = window.setInterval(agTick, AG_POLL_MS);
}

function agStopPoll() {
  if (agPoll !== null) window.clearInterval(agPoll);
  agPoll = null;
}

/* The countdown is deliberately coarse, and it NEVER repeats the instruction.
   Re-sending "Talk now, normally, as you would on air" with every countdown update makes the live
   region read the whole sentence again each time, and the timings then arrive far too late to be
   any use. The instruction is said ONCE when the take starts; after that the
   region carries the countdown alone, and only when the wording actually changes. */
function agCountdown(left) {
  if (left === null || left === undefined) return null;
  if (left > 20) return null;
  if (left > 10) return "20 seconds left.";
  if (left > 5) return "10 seconds left.";
  if (left > 0) return "Nearly done.";
  return null;
}

async function agTick() {
  let st;
  try {
    const resp = await fetch("/api/channels/autogain/status");
    st = await resp.json();
  } catch (e) {
    return;                       // a dropped poll is not worth saying anything about
  }
  // Two talking phases: the take that sets the gain, and the shorter one that checks it. Each
  // says its instruction ONCE and then counts down, for the same reason - repeating the sentence
  // means the live region reads the whole thing again and the timing arrives too late to use.
  if (st.phase === "listening" || st.phase === "verifying") {
    if (agSaidTalk !== st.phase) {
      agSaidTalk = st.phase;
      agLastCount = null;
      setText("ag-progress-desc", st.message || "Talk now.");
      return;
    }
    const words = agCountdown(st.seconds_left);
    if (words && words !== agLastCount) {
      agLastCount = words;
      setText("ag-progress-desc", words);
    }
    return;
  }
  if (st.phase === "preparing" || st.phase === "starting" || st.phase === "working") {
    setText("ag-progress-desc", st.message || "Working.");
    return;
  }
  if (!st.done) return;
  if (agFinishing) return;        // two overlapping polls must not both finish the run
  agFinishing = true;
  agStopPoll();
  agClose("ag-progress-dialog");
  // A FRESH dialog for the result: see the note at the top of this block.
  const failed = st.phase === "failed";
  setText("ag-result-title", failed ? "Gain not changed" : "Gain set");
  const undo = document.getElementById("ag-result-undo");
  // Nothing to undo when nothing was written, and a button that does nothing is worse than none.
  // The cost is that the dialog can be left with ONE focusable control, so Tab appears to do
  // nothing and the dialog feels like it has no buttons at all - which is exactly how it read on
  // the first run. The description therefore NAMES the buttons that are there.
  undo.hidden = failed || st.gain_after === st.gain_before;
  const buttons = undo.hidden ? " Press OK to close." : " Press Undo to put the gain back, or OK to keep it.";
  setText("ag-result-desc", (st.message || "") + buttons);
  const dlg = agDialog("ag-result-dialog");
  // showModal() THROWS on an already-open dialog, and a thrown error here would leave the page
  // with a dialog that is present but not modal - which is a Tab trap that is not trapping.
  if (!dlg.open) dlg.showModal();
  document.getElementById("ag-result-close").focus();
  // THE PAGE BEHIND IS NOT REBUILT WHILE THIS DIALOG IS OPEN. Calling loadChannels() here rebuilds
  // the panel underneath an open modal, which is the "nothing may replace a subtree a screen reader
  // may be sitting in" fault: Tab wanders out of the dialog and the result message is read over and
  // over. The refresh happens when the dialog CLOSES instead.
}

async function agStopEarly() {
  try {
    await postJSON("/api/channels/autogain/stop", {});
  } catch (e) { /* the run ends on its own anyway */ }
}

async function agUndo() {
  let res;
  try {
    res = await postJSON("/api/channels/autogain/undo", {});
  } catch (e) {
    announce("Could not reach the app service.", true);
    return;
  }
  agClose("ag-result-dialog");
  agReturnFocusNow();
  announce(res.message || "Done.", !res.ok);
  loadChannels();
}


/* Closing the result: put focus back on the button that started it, THEN refresh the page so the
   gain slider shows the new value. That order matters - refreshing first would rebuild the very
   control focus is about to be handed to. */
function agFinish() {
  agClose("ag-result-dialog");
  agReturnFocusNow();
  loadChannels();
}

function agReturnFocusNow() {
  const back = (agChannel && document.getElementById("autogain-" + agChannel)) || agReturnFocus;
  if (back) {
    try { back.focus(); } catch (e) { /* it may have been rebuilt away */ }
  }
}

function wireAutoGain() {
  const on = (id, fn) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("click", fn);
  };
  on("ag-confirm-ok", agStart);
  on("ag-confirm-cancel", () => { agClose("ag-confirm-dialog"); agReturnFocusNow(); });
  on("ag-progress-stop", agStopEarly);
  on("ag-result-close", agFinish);
  on("ag-result-undo", agUndo);
  // Escape closes a dialog through the cancel event; make sure the poll and focus follow.
  ["ag-confirm-dialog", "ag-progress-dialog", "ag-result-dialog"].forEach((id) => {
    const d = document.getElementById(id);
    if (!d) return;
    d.addEventListener("cancel", () => {
      if (id === "ag-progress-dialog") agStopEarly();
      // Escape from the RESULT dialog is a close like any other, so it must refresh the page too -
      // otherwise the gain slider is left showing the old value.
      window.setTimeout(id === "ag-result-dialog" ? agFinish : agReturnFocusNow, 0);
    });
  });
}

/* ---- App shell: nav-link router, Alt+digit shortcuts, theme ---------- */

/* Sections are permanent DOM; routing flips the `hidden` attribute and moves
   focus to the visible section's H1. A section's onShow runs EVERY time it is
   entered (see render), so what you read is the unit's state right now. */
/* ---- CallMe ----------------------------------------------------------------
   Entering the page reads the unit once and leaves a cheap poll running; the poll costs no device
   read, because the server's CallMe listener hears every change as the unit pushes it. Every
   sentence on the page comes from the server.

   ONLY THE TEXT IS REDRAWN, never the controls, so a poll landing mid-keystroke cannot move focus.
   The poll redraws only when the server's `version` moves, which during a call is once a second
   (the call quality), so nothing here is a live region except the status line. */
const CALLME_POLL_MS = 1000;
let callmePoll = null;
let callmeSeen = { version: null, incoming: null };

async function loadCallme() {
  setText("callme-state", "Reading the unit...");
  let d;
  try {
    d = await (await fetch("/api/callme")).json();
  } catch (e) {
    setStatus("callme-state", "Could not reach the AccessCaster service.");
    return;
  }
  if (!d.ok) {
    setStatus("callme-state", d.message || "Could not read CallMe.");
    return;
  }
  callmeSeen = { version: null, incoming: d.incoming ? d.incoming.id : null };
  applyCallme(d);
  setStatus("callme-state", d.status || "");
  startCallmePoll();
}

function applyCallme(d) {
  callmeSeen.version = d.version === undefined ? null : d.version;
  const ringing = !!d.incoming;
  const up = (d.slots || []).some((s) => ["Active", "Expiring", "Connecting"].includes(s.state));
  setText("callme-now", d.now || "");
  setAvailable(document.getElementById("callme-answer"), ringing, "No call is ringing.");
  // THE LABEL IS THE BUTTON'S ONLY NAME, so it is set every time and never left empty.
  const end = document.getElementById("callme-end");
  if (end) {
    end.textContent = d.end_label || "Hang up";
    setAvailable(end, ringing || up, "There is no call to end.");
  }
  const auto = document.getElementById("callme-auto-extend");
  if (auto && document.activeElement !== auto) auto.checked = !!d.auto_extend;
  setAvailable(document.getElementById("callme-extend"),
               (d.slots || []).some((s) => s.state === "Expiring"),
               "No call is about to end. Extend is offered five minutes before the hour runs out.");
  const details = (d.details || []).map((x) => x.label + ": " + x.value + ".");
  fillCallmeList("callme-details", details);
  const box = document.getElementById("callme-details");
  if (box) box.hidden = details.length === 0;

  const host = document.getElementById("callme-hosting");
  if (host && document.activeElement !== host) host.checked = !!d.hosting;
  setAvailable(host, !d.hosting_locked, "Hosting cannot be changed while a call is up.");
  const code = document.getElementById("callme-code");
  if (code) code.value = d.code || "";
  const link = document.getElementById("callme-link");
  if (link) link.value = d.link || "";
  setAvailable(document.getElementById("callme-copy"), !!d.link,
               "There is no guest link while hosting is off.");

  fillCallmeList("callme-plan", (d.plan || []).map((p) => p.label + ": " + p.value + "."));
  const note = document.getElementById("callme-fader-note");
  if (note) {
    note.textContent = d.fader_note || "";
    note.hidden = !d.fader_note;
  }
  renderCallmeContacts(d.contacts);
}

/* THE CONTACTS LIST is rebuilt only when it has changed, because a rebuild destroys the button a
   reader may be sitting on. Focus that was inside the list goes back to the button with the same
   id, which survives a rebuild because ids are built from the code. */
let callmeContactsSig = null;
let callmeEditing = null;         // the code of the contact being edited, or null when adding

function renderCallmeContacts(contacts) {
  const list = document.getElementById("callme-contacts");
  if (!list || !Array.isArray(contacts)) return;
  const sig = JSON.stringify(contacts);
  if (sig === callmeContactsSig) return;
  callmeContactsSig = sig;
  const active = document.activeElement;
  const focusId = active && list.contains(active) ? active.id : null;
  if (!contacts.length) {
    const li = document.createElement("li");
    li.textContent = "No contacts yet.";
    list.replaceChildren(li);
  } else {
    list.replaceChildren(...contacts.map((c) => {
      const li = document.createElement("li");
      li.append(c.name + ", code " + c.spoken + ". ",
                callmeContactButton("call", c, "Call " + c.name), " ",
                callmeContactButton("edit", c, "Edit " + c.name), " ",
                callmeContactButton("delete", c, "Delete " + c.name));
      return li;
    }));
  }
  const again = focusId && document.getElementById(focusId);
  if (again) again.focus();
}

function callmeContactButton(act, contact, label) {
  const b = document.createElement("button");
  b.type = "button";
  b.id = "callme-contact-" + act + "-" + contact.code;
  if (act !== "call") b.className = "secondary";
  b.dataset.act = act;
  b.dataset.code = contact.code;
  b.dataset.name = contact.name;
  b.textContent = label;
  return b;
}

function callmeContactFormReset() {
  callmeEditing = null;
  setText("callme-contact-save", "Add contact");
  document.getElementById("callme-contact-cancel").hidden = true;
  document.getElementById("callme-contact-name").value = "";
  document.getElementById("callme-contact-code").value = "";
}

async function onCallmeContactList(e) {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const { act, code, name } = btn.dataset;
  if (act === "call") {
    onCallmeRespond(btn, "/api/callme/dial", { code: code });
  } else if (act === "edit") {
    callmeEditing = code;
    document.getElementById("callme-contact-name").value = name;
    document.getElementById("callme-contact-code").value = code;
    setText("callme-contact-save", "Save changes to " + name);
    document.getElementById("callme-contact-cancel").hidden = false;
    document.getElementById("callme-contact-name").focus();
  } else if (act === "delete") {
    const ok = await confirmAction(
      "Delete " + name + "?",
      "This removes " + name + " from the contacts on this computer. A call that is up is not "
      + "affected.",
      "Delete");
    if (!ok) return;
    const res = await postCallmeContact("/api/callme/contact/delete", { code: code });
    if (res.ok) {
      if (callmeEditing === code) callmeContactFormReset();
      document.getElementById("callme-h-contacts").focus();
    }
  }
}

async function onCallmeContactSave(e) {
  e.preventDefault();
  const btn = document.getElementById("callme-contact-save");
  if (isBusy(btn)) return;
  setBusy(btn, true);
  const res = await postCallmeContact("/api/callme/contact/save", {
    name: document.getElementById("callme-contact-name").value,
    code: document.getElementById("callme-contact-code").value,
    was: callmeEditing || "",
  });
  setBusy(btn, false);
  if (!res.ok) return;
  const wasEditing = callmeEditing;
  callmeContactFormReset();
  // An edit returns to the contact's own Edit button; an addition stays in the empty form, ready
  // for the next one.
  const back = wasEditing && document.getElementById("callme-contact-edit-"
                                                      + (res.code || wasEditing));
  (back || document.getElementById("callme-contact-name")).focus();
}

async function postCallmeContact(path, body) {
  let res;
  try {
    res = await postJSON(path, body);
  } catch (e) {
    res = { ok: false, message: "Could not reach the AccessCaster service." };
  }
  setStatus("callme-state", res.message || "Done.");
  if (res.ok) renderCallmeContacts(res.contacts);
  return res;
}

/* Redraw a list's items only when one has changed, so a reader part way down it is not pulled
   back to the start by an identical rebuild once a second. */
function fillCallmeList(id, texts) {
  const list = document.getElementById(id);
  if (!list) return;
  const now = Array.from(list.children).map((li) => li.textContent);
  if (now.join("\n") === texts.join("\n")) return;
  list.replaceChildren(...texts.map((t) => {
    const li = document.createElement("li");
    li.textContent = t;
    return li;
  }));
}

function startCallmePoll() {
  stopCallmePoll();
  callmePoll = window.setInterval(pollCallme, CALLME_POLL_MS);
}

function stopCallmePoll() {
  if (callmePoll !== null) {
    window.clearInterval(callmePoll);
    callmePoll = null;
  }
}

async function pollCallme() {
  let d;
  try {
    d = await (await fetch("/api/callme/live")).json();
  } catch (e) {
    return;                       // a dropped poll is not worth saying anything about
  }
  if (!d.ok || d.version === callmeSeen.version) return;
  const ringing = d.incoming ? d.incoming.id : null;
  // A new call, said here only when the announcer is not saying it.
  if (ringing !== null && ringing !== callmeSeen.incoming && !d.announcing) {
    setStatus("callme-state", d.incoming.name + " is calling.");
  }
  callmeSeen.incoming = ringing;
  applyCallme(d);
}

async function onCallmeRespond(btn, path, body) {
  if (isBusy(btn)) return;
  const why = unavailableReason(btn);
  if (why !== null) {
    setStatus("callme-state", why);
    return;
  }
  setBusy(btn, true);
  setStatus("callme-state", "Please wait.");
  let res;
  try {
    res = await postJSON(path, body || {});
  } catch (e) {
    res = { ok: false, message: "Could not reach the AccessCaster service." };
  }
  setBusy(btn, false);
  setStatus("callme-state", res.message || "Done.");
  // Busy cleared the button's unavailable mark too, so redraw whatever the version says.
  callmeSeen.version = null;
  pollCallme();
}

async function onCallmeHosting(input) {
  const why = unavailableReason(input);
  if (why !== null) {
    input.checked = !input.checked;
    setStatus("callme-state", why);
    return;
  }
  const wanted = input.checked;
  setStatus("callme-state", wanted ? "Switching hosting on, please wait."
                                   : "Switching hosting off, please wait.");
  let res;
  try {
    res = await postJSON("/api/callme/hosting", { on: wanted });
  } catch (e) {
    res = { ok: false, message: "Could not reach the AccessCaster service." };
  }
  if (res.slots) applyCallme(res);
  else input.checked = !wanted;
  setStatus("callme-state", res.message || "Done.");
}

function wireCallme() {
  const answer = document.getElementById("callme-answer");
  const end = document.getElementById("callme-end");
  const host = document.getElementById("callme-hosting");
  const copy = document.getElementById("callme-copy");
  if (answer) answer.addEventListener("click", () => onCallmeRespond(answer, "/api/callme/answer"));
  // Decline or hang up, decided by the server at the moment of the press rather than by the
  // label, so a call that started ringing a moment ago is declined, not the guest you are with.
  if (end) end.addEventListener("click", () => onCallmeRespond(end, "/api/callme/end"));
  const extend = document.getElementById("callme-extend");
  if (extend) {
    extend.addEventListener("click", () => onCallmeRespond(extend, "/api/callme/extend"));
  }
  const auto = document.getElementById("callme-auto-extend");
  if (auto) {
    auto.addEventListener("change", async () => {
      let res;
      try {
        res = await postJSON("/api/callme/auto-extend", { on: auto.checked });
      } catch (e) {
        res = { ok: false, message: "Could not reach the AccessCaster service." };
      }
      if (!res.ok) auto.checked = !auto.checked;
      setStatus("callme-state", res.message || "Done.");
    });
  }
  const dialForm = document.getElementById("callme-dial-form");
  if (dialForm) {
    dialForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const code = (document.getElementById("callme-dial-code") || {}).value || "";
      onCallmeRespond(document.getElementById("callme-dial"), "/api/callme/dial", { code: code });
    });
  }
  if (host) host.addEventListener("change", () => onCallmeHosting(host));
  const contacts = document.getElementById("callme-contacts");
  if (contacts) contacts.addEventListener("click", onCallmeContactList);
  const contactForm = document.getElementById("callme-contact-form");
  if (contactForm) contactForm.addEventListener("submit", onCallmeContactSave);
  const cancel = document.getElementById("callme-contact-cancel");
  if (cancel) {
    cancel.addEventListener("click", () => {
      const code = callmeEditing;
      callmeContactFormReset();
      setStatus("callme-state", "Editing cancelled.");
      const back = code && document.getElementById("callme-contact-edit-" + code);
      (back || document.getElementById("callme-contact-name")).focus();
    });
  }
  if (copy) {
    copy.addEventListener("click", async () => {
      const why = unavailableReason(copy);
      if (why !== null) {
        setStatus("callme-state", why);
        return;
      }
      const box = document.getElementById("callme-link");
      // SELECTED EITHER WAY: the clipboard can be refused over plain HTTP, and then Control C on
      // the selected text is the route.
      box.focus();
      box.select();
      try {
        await navigator.clipboard.writeText(box.value);
        setStatus("callme-state", "The guest link is on the clipboard.");
      } catch (err) {
        setStatus("callme-state", "The link is selected. Press Control C to copy it.");
      }
    });
  }
}

const ROUTES = {
  // The Device page reads PER TAB (see readDevice): each tab's read is a device dump, and taking all
  // five of them on entry would slow the page the app opens on for things nobody is looking at.
  status:     { title: "Device",             section: "sec-status",     onShow: () => readDevice() },
  channels:   { title: "Channels and Processing", section: "sec-channels", onShow: () => loadChannels() },
  routing:    { title: "Routing",            section: "sec-routing",    onShow: () => readRouting() },
  pads:       { title: "SMART pads",         section: "sec-pads",       onShow: () => readPads() },
  // loadShows costs a device dump, unlike the other two, which read files on this computer. It is
  // still done on entry rather than behind a button: the list is what every show control acts on,
  // and a page offering a Restore with an empty dropdown is a page that cannot be used.
  profiles:   { title: "Backups",            section: "sec-profiles",
                onShow: () => { loadProfiles(); refreshBaselinePanel(); loadShows(); loadAutostart(); } },
  // Entering the page reads the unit once (state, storage, recordings) and then leaves a cheap
  // poll running for the state alone; leaving it stops the poll. See loadRecording.
  recording:  { title: "Recording",          section: "sec-recording",
                onShow: () => loadRecording() },
  // Read once on entry, then the free poll while the page is open. See loadCallme.
  callme:     { title: "CallMe",             section: "sec-callme",
                onShow: () => loadCallme() },
  about:      { title: "Help & About",       section: "sec-about" },
};
// CHANNELS, NOT DEVICE. Opening on Device to get its entry read to speak the desk's state makes
// the opening page nav item 6, a settings page rather than a working one. The state is spoken by
// the startup summary in its own right (see speakStartupSummary), so the app opens where you
// actually work and where the nav order says it should.
const DEFAULT_ROUTE = "channels";
const APP_TITLE = "AccessCaster";

function currentRoute() {
  const raw = (location.hash || "").replace(/^#\/?/, "");
  return ROUTES[raw] ? raw : DEFAULT_ROUTE;
}

/* `focusTo` decides where focus lands once the section is up: "heading" (the default, and what a
   route CHANGE wants, because the heading is what tells you the new page arrived) or "app" for the
   very first render, which starts at the top of the document on the app's own heading. */
function render(route, focusTo = "heading") {
  const info = ROUTES[route] || ROUTES[DEFAULT_ROUTE];
  // Show the active route's section, hide every OTHER distinct section. Done by section id
  // (not per-route) so any routes that share a section don't hide each other.
  const activeSection = info.section;
  // Never leave a live preview engaged on the mic when navigating away from the SMART pads page.
  if (activeSection !== "sec-pads" && previewState.active) stopPreview(true);
  // The recording-state poll belongs to its page. It costs no device read, but a timer that
  // outlives the page it updates is how a stale value gets announced from somewhere else.
  if (activeSection !== "sec-recording") stopRecordingPoll();
  if (activeSection !== "sec-callme") stopCallmePoll();
  new Set(Object.values(ROUTES).map((r) => r.section)).forEach((id) => {
    const sec = document.getElementById(id);
    if (sec) sec.hidden = (id !== activeSection);
  });
  document.title = info.title + " - " + APP_TITLE;
  document.querySelectorAll("[data-route]").forEach((a) => {
    a.removeAttribute("aria-current");
    if (a.dataset.route === route) a.setAttribute("aria-current", "page");
  });
  // OPENING THE APP STARTS ON THE APP'S OWN HEADING, the top of the document: the nav is one Tab
  // below it, the page content one H press away, and Alt with a digit works from anywhere. Starting
  // INSIDE the nav list puts you in the middle of a list with nothing above you. The heading names
  // the app and the build, and the
  // live region beneath it names the desk as soon as the enumeration answers.
  const appTitle = focusTo === "app" ? document.getElementById("app-title") : null;
  const h1 = document.querySelector("#" + info.section + " > h1[tabindex='-1']");
  const target = appTitle || h1;
  if (target) target.focus();
  const ann = document.getElementById("route-announcer");
  if (ann) {
    ann.textContent = "";
    window.setTimeout(() => { ann.textContent = info.title + " loaded"; }, 50);
  }
  // Read live from the device every time a section is entered, so what you see
  // is always the unit's current state (no manual "read" button).
  if (info.onShow) {
    try { info.onShow(); } catch (e) { /* a failed load already announces itself */ }
  }
}

/* "Name N of M" folded into a hidden span so the position announces in focus
   mode too, not only in browse/scan mode. Computed from the live item count. */
function applyItemPositions(listSelector) {
  const list = document.querySelector(listSelector);
  if (!list) return;
  const items = Array.from(list.children).filter((li) => li.tagName === "LI");
  const n = items.length;
  items.forEach((li, i) => {
    const ctl = li.querySelector(":scope > a, :scope > button");
    if (!ctl || ctl.querySelector(".item-pos")) return;
    const span = document.createElement("span");
    span.className = "sr-only item-pos";
    span.textContent = " " + (i + 1) + " of " + n;
    ctl.appendChild(span);
  });
}

/* Only suppress the nav shortcut inside genuine free-text entry, where Alt+digit
   could conceivably matter. A SELECT, checkbox, radio, range slider or button does
   NOT take typed text, so the shortcut must keep working when focus is on one -
   otherwise Alt+digit dies whenever focus lands on a dropdown after tabbing through
   a page (the "shortcuts randomly stop" bug). AltGr (Ctrl+Alt) is already excluded
   by the ctrlKey check, so plain Alt+digit never produces characters here. */
function isTextEntry(t) {
  if (!t) return false;
  if (t.isContentEditable || t.tagName === "TEXTAREA") return true;
  if (t.tagName === "INPUT") {
    // Only true free-text inputs. number/range/checkbox/radio/file/button do not
    // accept typed digits, so Alt+digit must still navigate from them.
    const type = (t.type || "text").toLowerCase();
    return /^(text|search|email|url|tel|password)$/.test(type);
  }
  return false;
}

/* Alt+digit jumps to a nav destination. Matches e.code (physical key) so it
   survives Mac Option remapping, and ignores presses inside text fields only. */
function bindShortcuts() {
  // Must match the aria-keyshortcuts in index.html, and the nav order. The pages worked in during a
  // session come first, and the three you visit deliberately (Backups, Device, Help & About) are
  // the last three. The numbers follow the list.
  const map = {
    Digit1: "channels", Digit2: "routing", Digit3: "pads", Digit4: "recording",
    Digit5: "callme", Digit6: "profiles", Digit7: "status", Digit8: "about",
  };
  document.addEventListener("keydown", (e) => {
    if (!e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    if (isTextEntry(e.target)) return;
    const route = map[e.code];
    if (!route) return;
    e.preventDefault();
    location.hash = "#/" + route;
  });
}

/* THE END OF THE PAGE IS A HARD STOP. Tab from the last control wrapping round to the top gives no
   clue that you have been round the houses: you simply find yourself somewhere else. Tab there does
   NOTHING except play a short tone, so the end of the page is a place you can feel.

   THIS IS A DELIBERATE KEYBOARD STOP, and it is worth writing down why it is not the trap WCAG
   2.1.2 is about. Getting back up the page does not depend on Tab at all: landmarks, headings, the
   nav links, Alt and a digit, and Home all still work, and Shift+Tab still walks back one control
   at a time. Nothing here is reachable ONLY by tabbing forwards, which is the condition that makes
   a trap a trap.

   TWO THINGS IT MUST NOT DO:
     * It must not fire on Shift+Tab. The top of the page is left alone; only the end is bounded.
     * It must not STOP inside a modal, because a modal needs Tab to WRAP rather than halt. Do NOT
       stand down when a dialog is open on the belief that `showModal()` runs its own closed cycle:
       it does not. See wrapDialogTab below, which handles the dialog case first, in both
       directions. */
const TABBABLE_SELECTOR = [
  "a[href]", "button", "input", "select", "textarea", "summary",
  "audio[controls]", "video[controls]", "[tabindex]",
].join(",");

/* Everything the Tab key can reach, in tab order. Document order IS tab order here because the app
   uses no positive tabindex anywhere (a positive one would jump the queue and this would be wrong).
   `root` scopes it to one subtree, which is how the dialog wrap below gets a dialog's own controls. */
function tabbableElements(root) {
  const out = [];
  (root || document).querySelectorAll(TABBABLE_SELECTOR).forEach((el) => {
    if (el.disabled) return;
    if (el.type === "hidden") return;
    const ti = el.getAttribute("tabindex");
    if (ti !== null && Number(ti) < 0) return;      // roving tabindex: only the selected tab is -1-free
    if (el.closest("[hidden]") || el.closest("[inert]")) return;
    // getClientRects() rather than offsetParent: it is empty for display:none, and it still
    // reports a box for a position:fixed element, which offsetParent does not.
    if (!el.getClientRects().length) return;
    out.push(el);
  });
  return out;
}

/* The boundary tone. Built with WebAudio rather than a sound file: no asset to ship, no download,
   and it works with the volume the browser is already using. A keypress is a user gesture, so the
   audio context is allowed to start here. */
let boundaryAudioCtx = null;
function playBoundaryCue() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    if (!boundaryAudioCtx) boundaryAudioCtx = new Ctx();
    if (boundaryAudioCtx.state === "suspended") boundaryAudioCtx.resume();
    const t = boundaryAudioCtx.currentTime;
    const osc = boundaryAudioCtx.createOscillator();
    const gain = boundaryAudioCtx.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(330, t);        // low and short: a wall, not an alarm
    // Ramped in and out. A tone that starts and stops square CLICKS, and a click sounds like a
    // fault rather than a boundary.
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(0.11, t + 0.012);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.11);
    osc.connect(gain).connect(boundaryAudioCtx.destination);
    osc.start(t);
    osc.stop(t + 0.13);
  } catch (_) { /* no audio available: the stop itself still works, which is the point */ }
}

/* A MODAL'S TAB CYCLE HAS TO BE CLOSED BY HAND. `showModal()` does NOT do it: without the wrap
   below you can tab straight out of a dialog into the browser toolbar.

   What showModal() actually guarantees is that the REST OF THE DOCUMENT goes inert, and that is all.
   Tabbing past the dialog's last control does not wrap round to its first: focus leaves the document
   altogether and lands on the browser's own toolbar and address bar. So the dialog is escapable, and
   a screen reader user is stranded outside the decision they were asked to make with no way back to
   it but Shift+Tab.

   THE CHROMIUM --app WINDOW THE APP NORMALLY OPENS IN (server.open_ui) MASKS IT, having no toolbar
   for focus to escape into. That masking is exactly why this must be fixed in
   the page rather than left to the window: the server binds 0.0.0.0 and the page is meant to be
   driven from a phone or another machine over the LAN, in an ordinary browser with full chrome.

   Two-way, unlike the page boundary below: Tab on the last control goes to the first, Shift+Tab on
   the first goes to the last. Wrapping is what the ARIA Authoring Practices require of a modal, and
   it is not the WCAG 2.1.2 trap, because Escape still closes every one of these dialogs. */
/* THE TOPMOST open dialog, which is not the same as the first one in the markup.
   Dialogs stack: a confirm opens the progress dialog, and for a moment both are open. The document
   order of the <dialog> elements has nothing to do with which is on top, so picking the first match
   computes the wrong dialog's controls entirely. Last-opened is last in the open list. */
function topmostOpenDialog() {
  const open = document.querySelectorAll("dialog[open]");
  if (!open.length) return null;
  // If focus is already inside one, that is the one the user is in, whatever the order says.
  const holding = document.activeElement && document.activeElement.closest
    ? document.activeElement.closest("dialog[open]") : null;
  return holding || open[open.length - 1];
}

/* THE DECISION COMES BEFORE THE preventDefault, deliberately. An earlier draft prevented the
   default up front "so Tab can never leave", then tried to step aside for the middle of a dialog -
   which cannot be undone, and would have blocked Tab INSIDE every dialog. Each branch below owns
   its own preventDefault, and the middle case owns none.

   Only the two EDGES can leak (browser chrome is reachable just past the last control), so handling
   both edges plus the "focus is not on a tabbable element" case closes the cycle completely. */
function wrapDialogTab(e) {
  const dlg = topmostOpenDialog();
  if (!dlg) return false;
  const list = tabbableElements(dlg);

  // A DIALOG CAN LEGITIMATELY HAVE NOTHING TABBABLE, and the progress dialog is exactly that while
  // a job runs: it hides its Close button, and focus sits on the heading, which carries
  // tabindex="-1" and so is not in this list. THIS IS THE CASE THAT STRANDS A USER: a reset with
  // "back up first" ticked and no storage in the desk fails its backup, leaving a dialog with
  // nothing to move to. There is nowhere to go, so focus stays put.
  if (!list.length) {
    e.preventDefault();
    if (!dlg.contains(document.activeElement)) {
      const heading = dlg.querySelector("[tabindex='-1']") || dlg;
      if (heading.focus) heading.focus();
    }
    // SAY SOMETHING, because a Tab that does nothing is indistinguishable from a dead keyboard.
    // This is the same tone the end-of-page stop uses, and it means the same thing: that was a wall.
    // ONLY here, never on an ordinary wrap - going round from the last control to the first is
    // normal modal behaviour and a tone on every lap would be noise.
    playBoundaryCue();
    return true;
  }

  const first = list[0];
  const last = list[list.length - 1];
  const inside = list.indexOf(document.activeElement);

  // Focus is on something untabbable INSIDE the dialog (a heading, the usual case just after it
  // opens), or has got out of it altogether. Either way the next Tab belongs on a real control of
  // THIS dialog rather than wherever the browser would have gone.
  if (inside === -1) {
    e.preventDefault();
    (e.shiftKey ? last : first).focus();
    return true;
  }
  if (!e.shiftKey && inside === list.length - 1) {
    e.preventDefault();
    first.focus();
    return true;
  }
  if (e.shiftKey && inside === 0) {
    e.preventDefault();
    last.focus();
    return true;
  }
  return false;        // mid-dialog: the browser moves focus correctly, so leave it alone
}

function bindPageBoundary() {
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Tab" || e.altKey || e.ctrlKey || e.metaKey) return;
    // A modal comes first and handles BOTH directions; see wrapDialogTab.
    if (wrapDialogTab(e)) return;
    if (document.querySelector("dialog[open]")) return;
    if (e.shiftKey) return;              // only the END of the page is bounded, never the top
    const list = tabbableElements();
    if (!list.length) return;
    if (document.activeElement !== list[list.length - 1]) return;
    e.preventDefault();
    playBoundaryCue();
  }, true);   // capture, so a control that handles Tab itself cannot get there first
}

/* Theme toggle: dark is the default. Plain button, verbose label, no aria-pressed;
   a role="status" region announces the new state after activation. */
function initThemeToggle() {
  const toggle = document.getElementById("theme-toggle");
  const status = document.getElementById("theme-toggle-status");
  if (!toggle) return;
  function apply(t) {
    document.documentElement.setAttribute("data-theme", t);
    toggle.querySelector(".theme-toggle-label").textContent =
      (t === "dark") ? "Dark mode currently on. Press to turn off."
                     : "Dark mode currently off. Press to turn on.";
  }
  let stored = null;
  try { stored = localStorage.getItem("theme"); } catch (_) {}
  apply(stored || "dark");
  toggle.addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    apply(next);
    try { localStorage.setItem("theme", next); } catch (_) {}
    status.textContent = "";
    window.setTimeout(() => {
      status.textContent = "Dark mode is now " + (next === "dark" ? "on" : "off") + ".";
    }, 50);
  });
}

/* Single active window. Only one AccessCaster page should be open at a time. Windows of the
   same browser talk over a BroadcastChannel: the one that is already open replies "occupied" to
   any newcomer, and the newcomer quietly bows out - it asks the server to raise the original
   window to the front and then closes itself. No modal, no "already open" message. */
function initSingleInstanceGuard() {
  if (!("BroadcastChannel" in window)) return;   // very old browser: skip the guard
  const ch = new BroadcastChannel("access-caster-instance");
  const myId = Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
  let active = null;          // null = undecided, true = the live window, false = duplicate
  let bowedOut = false;

  function becomeActive() {
    active = true;
    ch.postMessage({ type: "occupied", id: myId });
  }
  function bowOut() {
    if (bowedOut) return;     // only do this once
    bowedOut = true;
    active = false;
    // Bring the window that is already open to the front, then close this duplicate. window.close()
    // works for a window the app opened; if the browser refuses (a hand-opened tab), leave a single
    // quiet spoken note rather than a trapping dialog.
    fetch("/api/focus", { method: "POST", keepalive: true }).catch(() => {});
    try { window.close(); } catch (_) { /* ignore */ }
    setTimeout(() => {
      if (!window.closed) {
        announce("AccessCaster is already open in another window, which has been brought to the " +
                 "front. You can close this one.");
      }
    }, 300);
  }

  ch.onmessage = (ev) => {
    const m = ev.data;
    if (!m || m.id === myId) return;
    if (m.type === "hello") {
      // Reply if we hold it, or (on a simultaneous open) if our id sorts first, so exactly one stays.
      if (active === true || (active === null && myId < m.id))
        ch.postMessage({ type: "occupied", id: myId });
    } else if (m.type === "occupied") {
      if (active !== false) bowOut();
    }
  };

  window.addEventListener("pagehide", () => { try { ch.close(); } catch (_) {} });

  ch.postMessage({ type: "hello", id: myId });
  setTimeout(() => { if (active === null) becomeActive(); }, 400);
}

/* ---- init ----------------------------------------------------------- */

/* THE PAGE WATCHES THE SERVER IT WAS LOADED FROM, through the heartbeat below. Each reply names the
   server's run and version. Without this, a window left open across an upgrade keeps running the
   OLD page against the NEW server, so anything the new version added simply is not there.
     - No answer to SERVER_LOST_AFTER pings in a row: a modal says so, once per outage.
     - A different version answers: say so and reload, so the window runs the new page.
     - The same version on a new run (a restart): say it is back, no reload. */
const SERVER_LOST_AFTER = 3;          // missed pings in a row, so a blip is not an outage
const serverWatch = { seen: null, misses: 0, lostShown: false, reloading: false, returnTo: null };

function noteServer(reply) {
  const w = serverWatch;
  if (w.reloading) return;
  if (reply === null) {
    w.misses += 1;
    if (w.misses >= SERVER_LOST_AFTER && !w.lostShown) openServerLost();
    return;
  }
  const wasLost = w.misses >= SERVER_LOST_AFTER;
  w.misses = 0;
  if (!reply.instance) return;        // a refusal, or a server too old to say which it is
  if (w.seen === null) {
    w.seen = { instance: reply.instance, version: reply.version };
    return;
  }
  if (reply.version !== w.seen.version) {
    w.reloading = true;
    closeServerLost();
    announce("AccessCaster was updated to version " + reply.version + ". Reloading.", true);
    window.setTimeout(() => window.location.reload(), 2500);
    return;
  }
  if (reply.instance !== w.seen.instance) w.seen.instance = reply.instance;
  if (wasLost) {
    w.lostShown = false;
    closeServerLost();
    announce("AccessCaster is back.", true);
  }
}

function openServerLost() {
  const dlg = document.getElementById("server-lost-dialog");
  if (!dlg || dlg.open) return;
  serverWatch.lostShown = true;
  serverWatch.returnTo = document.activeElement;
  dlg.showModal();
  document.getElementById("server-lost-ok").focus();
}

function closeServerLost() {
  const dlg = document.getElementById("server-lost-dialog");
  if (!dlg || !dlg.open) return;
  dlg.close();
  const back = serverWatch.returnTo;
  if (back && back.isConnected && typeof back.focus === "function") back.focus();
}

function initAppLifecycle() {
  // The server lives only while a webpage is open. Heartbeat keeps it alive; closing the page
  // (sendBeacon on pagehide) tells it to stop. A reload also fires pagehide, but the reloaded
  // page's first ping cancels the pending shutdown server-side, so refreshing is safe.
  const ping = async () => {
    let reply = null;
    try {
      const resp = await fetch("/api/ping", { method: "POST", keepalive: true });
      // ANY answer means the server is up, including a refusal: a remote page with a stale token
      // is not looking at a stopped server.
      try { reply = await resp.json(); } catch (e) { reply = {}; }
    } catch (e) {
      reply = null;
    }
    noteServer(reply);
  };
  const ok = document.getElementById("server-lost-ok");
  if (ok) ok.addEventListener("click", closeServerLost);
  ping();
  setInterval(ping, 4000);
  // Browsers throttle (or fully freeze) timers in a hidden/backgrounded tab, so the 4s ping can
  // stall long enough to look like a dead page and let the server's watchdog stop it. Re-ping the
  // instant we become visible / focused / back online so the heartbeat clock resets on return.
  document.addEventListener("visibilitychange", () => { if (!document.hidden) ping(); });
  window.addEventListener("focus", ping);
  window.addEventListener("online", ping);
  // Only quit on a REAL page close. pagehide with persisted === true means the page entered the
  // back/forward cache (e.g. backgrounded) and may be restored, so we must NOT stop the server
  // then. beforeunload is intentionally not wired to quit, as it can fire without a real close.
  const quit = () => { try { navigator.sendBeacon("/api/quit"); } catch (_) {} };
  window.addEventListener("pagehide", (e) => { if (!e.persisted) quit(); });
}

/* An older build stored an open/closed state per collapsible group under this key. There are no
   groups, so the WHOLE store goes: a design with no closed state must not inherit one, and the key
   would otherwise sit in localStorage for ever with nothing left to read it.
   Harmless to run every startup: after the first it finds nothing. */
const RETIRED_GROUP_STATE_KEY = "accesscaster.groups";

function dropRetiredGroupState() {
  try { window.localStorage.removeItem(RETIRED_GROUP_STATE_KEY); } catch (e) { /* ignore */ }
}

async function init() {
  initAppLifecycle();
  initSingleInstanceGuard();
  dropRetiredGroupState();
  wireAboutTabs();      // the About page's tab strip (text only, so no reads and no onSelect)
  wireRoutingTabs();    // the Routing page's tab strip, built over its static panes
  wireRecording();      // the Recording page: three transport buttons and a check
  wireAutoGain();       // Channels: set a mic's gain by measuring it
  document.getElementById("midi-toggle").addEventListener("change", onMidiToggle);
  document.getElementById("announce-toggle").addEventListener("change", onAnnounceToggle);
  document.getElementById("announce-levels-toggle")
          .addEventListener("change", onAnnounceLevelsToggle);
  document.getElementById("announce-mute-tones-toggle")
          .addEventListener("change", onAnnounceMuteTonesToggle);
  document.getElementById("announce-startup-summary-toggle")
          .addEventListener("change", onAnnounceStartupSummaryToggle);
  document.getElementById("announce-volume").addEventListener("input", onAnnounceVolumeInput);
  document.getElementById("announce-voice").addEventListener("change", onAnnounceVoiceChange);
  document.getElementById("announce-output").addEventListener("change", onAnnounceOutputChange);
  document.getElementById("announce-devices-refresh")
          .addEventListener("click", onAnnounceDevicesRefresh);
  wireAccess();         // the remote-access link on Device > Hardware
  wireCallme();         // the CallMe page's buttons, hosting switch and copy
  wireHotkeys();        // the shortcut chooser, its editor, and the Add / Delete dialogs
  document.getElementById("dev-state-btn").addEventListener("click", loadDeviceState);
  const fwCheck = document.getElementById("dev-fw-check");
  if (fwCheck) fwCheck.addEventListener("click", () => onFirmwareCheck(fwCheck));
  // Network: the three writable controls (see the netPost block).
  document.getElementById("net-wifi-toggle").addEventListener("change", onWifiToggle);
  document.getElementById("net-bt-toggle").addEventListener("change", onBtVisibleToggle);
  document.getElementById("net-join-btn").addEventListener("click", onWifiJoin);
  document.getElementById("net-scan-btn").addEventListener("click", onWifiScan);
  document.getElementById("net-scan-list").addEventListener("change", onScanPick);
  wireDeviceTabs();     // the Device page's tab strip, built over its static panes
  document.getElementById("fader-levels-refresh")
          .addEventListener("click", () => loadFaderLevels(true));
  document.getElementById("fader-solos-clear")
          .addEventListener("click", onClearFaderSolos);
  // NO "Free the encoder" button: the desk frees it itself. Pressing the holding channel button a
  // second time puts the encoder back on the levels (measured: systemChannelSelected 0, then -1),
  // and the announcer names both the channel and that way out, so a software write for it would be
  // a control earning nothing.
  // SMART pads: the pad is chosen above the tab strip, then Settings edits it live and Reassign
  // changes what it is behind one Apply.
  wirePadsTabs();
  document.getElementById("pad-ed-select").addEventListener("change", syncPadEditor);
  document.getElementById("pad-ed-type").addEventListener("change", () => updateTypeGroups(true));
  document.getElementById("pad-ed-apply-btn").addEventListener("click", onPadApply);
  document.getElementById("pad-ed-clear-btn").addEventListener("click", onPadClear);
  // The one thing an empty pad's Settings tab can offer: the way to the tab that fills it. Focus
  // lands on the Reassign TAB, which announces itself, rather than jumping into the panel, so the
  // move is never silent.
  document.getElementById("pad-ed-goto-reassign")
          .addEventListener("click", () => { if (padsTabs) padsTabs.selectKey("reassign"); });
  document.getElementById("pad-ed-name-btn").addEventListener("click", onPadRename);
  document.getElementById("clear-all-btn").addEventListener("click", onClearAllPads);
  // DEBOUNCED like every other dropdown: a CLOSED select fires `change` on every arrow press, so
  // walking this list would save each stream as you passed it, and the meter would end up pointed
  // at a stream nobody chose. It is wired by hand here rather than through makeSelectRow, which is
  // why the shared debounce does not cover it.
  let meterStreamCommit = null;
  document.getElementById("meter-stream").addEventListener("change", (ev) => {
    const no = unavailableReason(ev.target);
    if (no !== null) { announce(no, true); return; }
    const value = ev.target.value;
    if (meterStreamCommit !== null) window.clearTimeout(meterStreamCommit);
    meterStreamCommit = window.setTimeout(async () => {
      meterStreamCommit = null;
      try {
        const res = await postJSON("/api/meters/stream", { stream: value });
        announce(res.message || "Done.", !res.ok);
        loadMeters();                 // the level and mute belong to the stream, so re-read them
      } catch (e) {
        announce("Could not reach the app service.", true);
      }
    }, SELECT_COMMIT_MS);
  });
  document.getElementById("restore-fx-btn").addEventListener("click", onRestoreFactoryFx);
  document.getElementById("export-pad-btn").addEventListener("click", onExportPad);
  document.getElementById("open-exports-btn").addEventListener("click", onOpenPadExports);
  document.getElementById("tidy-audio-btn").addEventListener("click", onTidyPadAudio);
  document.getElementById("fx-create-save").addEventListener("click", onFxCreateSave);
  document.getElementById("fx-preview-toggle").addEventListener("change", onPreviewToggle);
  // Safety net: if the page is closed/hidden mid-preview, clear the scratch pad so no effect
  // is left engaged on the live mic.
  window.addEventListener("pagehide", () => {
    if (previewState.active && previewState.grid != null) {
      try {
        navigator.sendBeacon("/api/fx/preview/stop",
          new Blob([JSON.stringify({ grid: previewState.grid })], { type: "application/json" }));
      } catch (_) { /* best effort */ }
    }
  });
  document.getElementById("pad-ed-volume").addEventListener("input", onPadVolumeInput);
  document.getElementById("pad-ed-colour").addEventListener("change", onPadColour);
  document.getElementById("pad-ed-replay").addEventListener("change", (e) => onPadToggle("/api/pad/replay", e.target));
  document.getElementById("pad-ed-loop").addEventListener("change", (e) => onPadToggle("/api/pad/loop", e.target));
  document.getElementById("pad-ed-playmode").addEventListener("change", onPlayMode);
  document.getElementById("pad-ed-mixer-trigger").addEventListener("change", onMixerTrigger);
  document.getElementById("pad-ed-trigger-custom")
          .addEventListener("change", (e) => onTriggerCustom(e.target));
  document.getElementById("pad-ed-fade-host")
          .addEventListener("change", (e) => onPadFlag("fade_exclude_host", e.target));
  document.getElementById("pad-ed-censor-custom")
          .addEventListener("change", (e) => onPadFlag("censor_custom", e.target));
  document.getElementById("pad-confirm-cancel").addEventListener("click", () => resolvePadConfirm(false));
  document.getElementById("pad-confirm-ok").addEventListener("click", () => resolvePadConfirm(true));
  // Escape fires the dialog's 'cancel' event; treat it as Cancel.
  document.getElementById("pad-confirm-dialog").addEventListener("cancel", (e) => { e.preventDefault(); resolvePadConfirm(false); });

  document.getElementById("faders-reset-btn").addEventListener("click", onResetFaders);
  document.getElementById("monmix-output").addEventListener("change", loadMonmixOutput);
  document.getElementById("monmix-reset-btn").addEventListener("click", onMonmixReset);

  document.getElementById("profile-save-open").addEventListener("click", openSaveDialog);
  document.getElementById("profile-restore-open").addEventListener("click", openRestoreDialog);
  document.getElementById("profile-rename-open").addEventListener("click", openRenameDialog);
  document.getElementById("profile-delete-open").addEventListener("click", openDeleteDialog);
  document.getElementById("profile-save-form").addEventListener("submit", onProfileSave);
  document.getElementById("profile-rename-form").addEventListener("submit", onProfileRename);
  document.getElementById("restore-confirm-btn").addEventListener("click", doRestore);
  document.getElementById("delete-confirm-btn").addEventListener("click", onProfileDelete);

  // The unit's own Shows. Export runs straight from the page (it only ADDS a backup, so there is
  // nothing to confirm); the three destructive ones go through the shared confirm dialog.
  // This app's own settings, on the same page because this is where backup lives.
  document.getElementById("appsettings-export-btn")
    .addEventListener("click", onAppSettingsExport);
  document.getElementById("autostart-switch")
          .addEventListener("change", (ev) =>
            onAutostartToggle(ev.currentTarget.checked, ev.currentTarget));
  document.getElementById("appsettings-open-btn")
    .addEventListener("click", onAppSettingsOpenFolder);
  document.getElementById("appsettings-import-btn")
    .addEventListener("click", onAppSettingsImport);

  document.getElementById("show-reset-btn")
    .addEventListener("click", () => openShowConfirm("reset"));
  document.getElementById("show-confirm-go").addEventListener("click", doShowConfirmed);
  document.getElementById("show-confirm-cancel")
    .addEventListener("click", () => closeProfileDialog("show-confirm-dialog"));
  // Every Cancel and Close just dismisses its own dialog.
  document.getElementById("baseline-save-btn").addEventListener("click", onBaselineSave);
  document.getElementById("baseline-skip-btn").addEventListener("click", onBaselineSkip);
  document.getElementById("baseline-add-pads").addEventListener("click", onBaselineAddPads);
  [["save-cancel-btn", "save-dialog"], ["restore-cancel-btn", "restore-dialog"],
   ["rename-cancel-btn", "rename-dialog"], ["delete-cancel-btn", "delete-dialog"],
   ["progress-close-btn", "progress-dialog"]]
    .forEach(([btn, dlg]) => {
      document.getElementById(btn).addEventListener("click", () => closeProfileDialog(dlg));
    });
  ["save-dialog", "restore-dialog", "rename-dialog", "delete-dialog", "progress-dialog",
   "baseline-dialog", "show-confirm-dialog"]
    .forEach(wireProfileDialog);

  // Fast, non-device layout/state loads (no USB open): mic channels + MIDI state,
  // processing layout, pad list. The live device reads are lazy (router onShow / buttons).
  loadChannels();
  loadRouting();
  // Pads are read live from the device, so they load lazily on the pads route's onShow.

  applyItemPositions(".navlist");
  bindShortcuts();
  bindPageBoundary();
  initThemeToggle();
  window.addEventListener("hashchange", () => render(currentRoute()));
  // RAISING THE WINDOW IS NOT ARRIVING IN IT. The global shortcut brings this window to the front,
  // and focus then sits on the document with nothing to announce, so a screen-reader user hears
  // silence until they press Tab and land somewhere by accident. Landing on the section heading is
  // what the app already does when it opens (see render's focusTo), so coming back does the same.
  //

  // AWAITED, unlike every other startup load: the version is part of the heading focus lands on.
  // See loadVersionBeforeRender for the timeout that stops a dead server costing us the focus too.
  await loadVersionBeforeRender();
  render(currentRoute(), "app");
  // THE DESK LINE IN THE HEADER NEEDS A STATUS READ ON EVERY ROUTE, not just the Device page, since
  // it is part of the chrome now. The Device route takes that read itself (readDevice), so asking
  // here as well would open the USB twice on the app's own opening route.
  if (currentRoute() !== "status") loadStatus(false);
  // AFTER the status read, and after a beat: the summary costs a full model dump and takes the HID
  // lock, so firing it with the opening page's own reads makes both slower and delays the page you
  // are looking at for the sake of a sentence. The wait also spaces the two live-region updates, so
  // the connection line is not overwritten before it has been read out.
  window.setTimeout(speakStartupSummary, STARTUP_SUMMARY_DELAY_MS);
  // LAST, and after the route has rendered: the first-run offer to back the unit up as found. It
  // interrupts, so it must not do so while the page is still assembling itself around it.
  maybeOfferBaseline();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
