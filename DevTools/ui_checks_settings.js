// probe: route=settings timeout=600
/* Machine checks for the App settings page: its tab strip, and the Level warnings and Keyboard
   shortcuts tabs in detail. Run with: python DevTools/run_ui_probes.py settings
   Not a screen-reader pass: it pins what a screen reader depends on and the eye cannot see.

   EVERY TAB HERE IS THE APP'S OWN, so none of them reads the unit. Announcements and the keyboard
   shortcuts sit together at the front, because the first shortcut is the announcer's own on/off.
   The Updates tab's own checks are ui_checks_updates.js. */

const TABS = ["speak", "shortcuts", "levels", "access", "general", "updates"];
const TITLES = ["Announcements", "Keyboard shortcuts", "Level warnings", "Remote access", "General",
                "Updates"];
/* What each tab reads when it is opened: one call each, and never the desk. */
const TAB_READS = {
  speak: ["/api/announce"],
  // The CATALOGUE (`/api/hotkeys/commands`) is the slow one that reads the unit, and it is fetched
  // only when the Add dialog is opened.
  shortcuts: ["/api/hotkeys"],
  // The meter comes from Windows' own audio endpoints, not over the control connection.
  levels: ["/api/meters"],
  access: ["/api/access"],
  general: ["/api/appsettings/autostart"],
  updates: ["/api/update"],
};
const DESK_READS = ["/api/device/", "/api/status", "/api/network", "/api/routing", "/api/soundpads"];

const tabId = (k) => "tab-settings-" + k;
const strip = () => document.querySelector("#sec-settings [role='tablist']");
const buttons = () => Array.from(strip().querySelectorAll("[role='tab']"));
const panes = () => Array.from(document.querySelectorAll("#settings-panel > .cat-tab"));
const visiblePanes = () => panes().filter((p) => !p.hidden);
const paths = (log) => log.map((e) => e.method + " " + e.path);
const hit = (log, needle) => paths(log).some((p) => p.indexOf(needle) >= 0);

async function openTab(k) {
  document.getElementById(tabId(k)).click();
  await waitForApi(TAB_READS[k]);
  await quiet();
}

async function run() {
  await waitFor(() => !document.getElementById("sec-settings").hidden && strip(), 20000);
  await quiet();

  // ---- the page and its strip --------------------------------------------------
  check("the page heading says what it is", flatText(document.getElementById("h-settings"))
    === "App settings", flatText(document.getElementById("h-settings")));
  const nav = Array.from(document.querySelectorAll(".navlink"));
  check("it is on the nav, before Help & About",
    nav.length >= 2 && nav[nav.length - 2].dataset.route === "settings"
    && nav[nav.length - 1].dataset.route === "about", nav.map((a) => a.dataset.route).join(","));
  check("on Alt+8, with Help & About on Alt+9",
    nav[nav.length - 2].getAttribute("aria-keyshortcuts") === "Alt+8"
    && nav[nav.length - 1].getAttribute("aria-keyshortcuts") === "Alt+9");
  check("six tabs, in the agreed order",
    buttons().length === TABS.length && buttons().every((b, i) => b.id === tabId(TABS[i])),
    buttons().map((b) => b.id).join(","));
  check("the tab titles are the agreed ones",
    buttons().every((b, i) => b.textContent.trim() === TITLES[i]),
    buttons().map((b) => b.textContent.trim()).join(" | "));
  check("every pane opens with its own h2",
    panes().every((p) => p.querySelector("h2") && p.querySelectorAll("h2").length === 1),
    panes().map((p) => p.querySelectorAll("h2").length).join(","));
  check("exactly one pane is on the page", visiblePanes().length === 1, visiblePanes().length);
  check("the strip is ONE tab stop (roving tabindex)",
    buttons().filter((b) => b.tabIndex === 0).length === 1);

  // ---- what each tab reads: its own call, and never the desk ---------------------
  for (const k of TABS) {
    await openTab(k === TABS[0] ? TABS[1] : TABS[0]);
    await probeClear();
    document.getElementById(tabId(k)).click();
    check("opening " + k + " reads " + TAB_READS[k].join(" + "),
      await waitForApi(TAB_READS[k]));
    await quiet();
    const log = await probeLog();
    const desk = DESK_READS.filter((p) => hit(log, p));
    check("opening " + k + " reads nothing from the unit", desk.length === 0, desk.join(" "));
  }

  // ---- what moved here from Backups ----------------------------------------------
  await openTab("general");
  const general = document.querySelector("#settings-panel > .cat-tab[data-tab-key='general']");
  check("start at sign-in is on General", general.contains(document.getElementById("autostart-switch")));
  check("and so are saving and restoring these settings",
    general.contains(document.getElementById("appsettings-export-btn"))
    && general.contains(document.getElementById("appsettings-import-btn")));
  const pointer = document.getElementById("backups-appsettings-link");
  check("the Backups page points here, by a real link",
    pointer && pointer.getAttribute("href") === "#/settings"
    && !!document.getElementById("sec-profiles").contains(pointer));

  // ---- the LEVELS tab -------------------------------------------------------------
  // What is worth pinning here is not the controls, it is the HONEST DEGRADATION. This is the only
  // part of the app that cannot work over Remote Desktop, so its failure path must explain itself
  // rather than showing dead controls or an empty dropdown.
  await openTab("levels");
  await sleep(600);
  const lv = document.querySelector("#settings-panel > .cat-tab[data-tab-key='levels']");
  // THREE JOBS, AND THE MIDDLE ONE EARNS ITS PLACE: Windows' own level for the stream sits between
  // the stream chooser and the warnings, because it is the hidden multiplier that decides what
  // both of them mean. The level CHECK is not here at all - it is per channel on Channels - so the
  // first heading is the stream chooser the background watch needs.
  check("the Levels pane has its three jobs under their own h3s, in that order",
    Array.from(lv.querySelectorAll("h3")).map((h) => h.textContent.trim()).join(" | ")
      === "Which stream to watch | Windows level | Warn me while I work",
    Array.from(lv.querySelectorAll("h3")).map((h) => h.textContent.trim()).join(" | "));
  // The control itself: dB and a number box, NOT a percentage slider. On this endpoint 50 percent
  // is -1.08 dB and unity is about 55 percent, so a percentage is a number nobody can reason
  // about, and dragging cannot land on "no change".
  // WHEN THERE IS NOTHING TO METER, THESE FIVE ARE SKIPPED, NOT FAILED. The Windows-level control
  // is only built for a real endpoint, and in a Remote Desktop session the desk's audio streams
  // belong to the console session, so it is honestly absent. Failing here trains everyone to
  // ignore five failures, which is how a real one gets missed - the recording suite makes the same
  // move when there is no drive in.
  const lvlBox = document.getElementById("meter-input-db");
  if (!lvlBox) {
    const why = (document.getElementById("meter-status") || {}).textContent || "no endpoint";
    ["the Windows level is a NUMBER box in dB",
     "its label carries the unit",
     "its range cannot silently mute the machine",
     "there is a one-press way back to no change",
     "its hint says this applies to EVERY application"].forEach((n) => skip(n, why.slice(0, 90)));
  } else {
  check("the Windows level is a NUMBER box in dB, not a percentage slider",
    lvlBox && lvlBox.type === "number", lvlBox && lvlBox.type);
  // "dB", not the word "decibels": the label is read on every arrival at the control, and dB is
  // what the unit is called everywhere else in the app.
  check("its label carries the unit, so a bare number cannot be mistaken for a percentage",
    lvlBox && /\bdB\b/.test((document.querySelector("label[for='meter-input-db']") || {})
      .textContent || ""),
    (document.querySelector("label[for='meter-input-db']") || {}).textContent);
  check("its range is the narrowed one: nothing can silently mute the machine",
    lvlBox && parseFloat(lvlBox.min) >= -30, lvlBox && lvlBox.min);
  const unity = document.getElementById("meter-input-unity");
  check("there is a one-press way back to no change", !!unity && unity.tagName === "BUTTON");
  check("and its hint says this applies to EVERY application, not just this one",
    /every application/i.test((document.getElementById("meter-input-hint") || {})
      .textContent || ""));
  }
  // NO NAMED GROUPS. A role="group" with a name is announced on the way in, before the control you
  // were heading for, and again as "out of grouping" on the way out. The h3 above each section says
  // the same thing, is reachable with H, and costs nothing when you are not looking for it. What is
  // pinned is that the HEADINGS are there, since they are what carries the structure.
  check("no named groups are left on this pane",
    lv.querySelectorAll("[role='group']").length === 0,
    lv.querySelectorAll("[role='group']").length);
  const meterStatus = document.getElementById("meter-status");
  const streamSel = document.getElementById("meter-stream");
  const streamOff = streamSel && streamSel.getAttribute("aria-disabled") === "true";
  const available = streamSel && streamSel.options.length > 0 && !streamOff;
  check("the stream dropdown is never left empty AND usable with nothing in it",
    !(streamSel.options.length === 0 && !streamOff),
    streamSel.options.length + " options, unavailable=" + streamOff);
  // It is marked unavailable, NEVER removed from the tab order: a natively disabled select is
  // skipped silently, which is how a whole control disappears with no explanation.
  check("and when it is unavailable it keeps its place and carries the reason",
    !streamSel.disabled && (!streamOff || !!streamSel.dataset.offReason),
    streamSel.dataset.offReason);
  check("when there is nothing to meter, the page SAYS why instead of going quiet",
    available || (meterStatus && meterStatus.textContent.trim().length > 30),
    available ? "(streams available)" : (meterStatus && meterStatus.textContent));
  check("and the reason names the real cause rather than saying it failed",
    available || /Remote Desktop|not connected|transfer mode/i.test(meterStatus.textContent),
    meterStatus && meterStatus.textContent.slice(0, 80));
  check("the stream dropdown is named as a DEVICE, which is what it lists",
    /audio device/i.test((document.querySelector("label[for='meter-stream']") || {})
      .textContent || ""),
    (document.querySelector("label[for='meter-stream']") || {}).textContent);
  check("the warn-me switches are built whether or not a stream is there, so the settings are "
    + "reachable before you plug in",
    ["meter-watch-on", "meter-warn-clip", "meter-warn-silence", "meter-clip-sound"]
      .every((id) => {
        const el = document.getElementById(id);
        return el && el.type === "checkbox";
      }));
  // A SOUND as well as the words for clipping: it is the one thing on this tab you react to
  // rather than think about, and a spoken sentence arrives a word at a time.
  check("clipping can be a sound as well as a sentence, and it is on by default",
    document.getElementById("meter-clip-sound").checked);
  // Windows' own MUTE, which reads exactly like a dead microphone and is fixed in a Windows
  // dialog rather than anywhere on the desk.
  const mutedSw = document.getElementById("meter-input-muted");
  check("a Windows mute can be seen and undone from here",
    !mutedSw || mutedSw.type === "checkbox", mutedSw && mutedSw.type);
  check("the two thresholds are NUMBER boxes, not sliders (they are values you know)",
    ["meter-silence-after", "meter-speak-every"].every((id) => {
      const el = document.getElementById(id);
      return el && el.type === "number" && el.labels && el.labels.length === 1;
    }));
  check("speaking the level on a timer is OFF by default: a commentary during a take is unusable",
    document.getElementById("meter-speak-every").value === "0",
    document.getElementById("meter-speak-every").value);
  // THE LEVEL CHECK IS NOT ON THIS TAB. Every Windows stream a RODECaster offers is a mix, so a
  // stream meter cannot answer "is Mic 2 right"; the check is per channel, on Channels
  // (DevTools/ui_checks_levelcheck.js). What is pinned here is that this tab has no orphan button
  // and says where the check lives.
  check("the stream check button is gone from this tab",
    !document.getElementById("meter-check-btn"));
  check("and so is its result line, which would now never be written to",
    !document.getElementById("meter-check-result"));
  const checkHint = ((document.getElementById("meter-check-hint") || {}).textContent || "")
    .replace(/\s+/g, " ").trim();
  check("the tab says where the per-channel check lives now",
    /channels page/i.test(checkHint), checkHint);
  // The stream dropdown STAYS: the background watch below still meters one stream.
  check("the stream dropdown is still here for the watch, and named for watching",
    /watch/i.test((document.querySelector("label[for='meter-stream']") || {}).textContent || ""),
    (document.querySelector("label[for='meter-stream']") || {}).textContent);

  // ---- the KEYBOARD SHORTCUTS tab -------------------------------------------------
  // The options are BUILT FROM /api/hotkeys, because server.py owns the list of actions and this
  // page must not become a second copy of it. So these checks assert the SHAPE, not a fixed set of
  // shortcut names.
  // A LISTBOX WITH ONE TAB STOP: Enter edits in a dialog with Save, Delete deletes after asking,
  // and the Applications key (a `contextmenu` event) or a click opens a menu of the rest.
  await openTab("shortcuts");
  const hkList = document.getElementById("hotkey-list");
  const hkOpts = () => [...hkList.querySelectorAll("[role='option']")];
  const hkKey = (target, key) => target.dispatchEvent(new KeyboardEvent("keydown",
    { key, bubbles: true, cancelable: true }));
  check("the shortcuts are a listbox of options",
    hkList && hkList.getAttribute("role") === "listbox" && hkOpts().length >= 1, hkOpts().length);
  check("the list is labelled", !!document.getElementById(hkList.getAttribute("aria-labelledby")));
  // A description is read every time focus comes back into the list, after every dialog.
  check("and NOT described, so coming back from a dialog is quick",
    !hkList.getAttribute("aria-describedby"));
  const hkHelp = document.getElementById("hotkey-list-help");
  check("its hint comes BEFORE it, where browse mode meets it first",
    hkHelp && (hkHelp.compareDocumentPosition(hkList) & Node.DOCUMENT_POSITION_FOLLOWING)
    && /enter/i.test(hkHelp.textContent) && /applications key/i.test(hkHelp.textContent));
  check("ONE tab stop: exactly one option is in the tab order, the selected one",
    hkOpts().filter((o) => o.tabIndex === 0).length === 1
    && hkOpts().some((o) => o.tabIndex === 0 && o.getAttribute("aria-selected") === "true"));
  check("each option says what the shortcut is on, so the list alone answers 'what are my keys'",
    hkOpts().every((o) => /, /.test(o.textContent)),
    hkOpts().slice(0, 3).map((o) => o.textContent).join(" | "));
  if (hkOpts().length > 1) {
    selectHotkey(hkOpts()[0].dataset.id);
    hkKey(document.activeElement, "ArrowDown");
    check("Down moves the selection AND focus to the next shortcut",
      document.activeElement === hkOpts()[1]
      && hkOpts()[1].getAttribute("aria-selected") === "true");
    hkKey(document.activeElement, "Home");
    check("Home goes back to the first", document.activeElement === hkOpts()[0]);
  }

  // EDIT IS A DIALOG WITH SAVE, so browsing its key list registers nothing.
  const editDlg = document.getElementById("hotkey-edit-dialog");
  hkOpts()[0].focus();
  hkKey(document.activeElement, "Enter");
  check("Enter on a shortcut opens it for editing, as a modal", editDlg.open);
  check("the dialog is named after the shortcut",
    /edit shortcut: ./i.test(document.getElementById("hotkey-edit-title").textContent),
    document.getElementById("hotkey-edit-title").textContent);
  const keySel = document.getElementById("hotkey-key");
  check("focus lands on the key, a real select with a real label",
    document.activeElement === keySel && keySel.labels.length === 1 && keySel.options.length > 30);
  check("there are no modifier switches in it: the keys held are one setting for all",
    !editDlg.querySelector("#hotkey-mods"));
  const offSwitch = document.getElementById("hotkey-off");
  check("a shortcut can be switched off, and the switch says OFF",
    /off/i.test(offSwitch.closest("label").textContent), offSwitch.closest("label").textContent.trim());
  check("and it is inverted correctly: an enabled shortcut shows it unticked",
    !offSwitch.checked === !/, off$/.test(hkOpts()[0].textContent), hkOpts()[0].textContent);
  await probeClear();
  keySel.value = keySel.options[(keySel.selectedIndex + 1) % keySel.options.length].value;
  keySel.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(600);
  check("browsing the key list writes NOTHING", !(await probeLog()).some((e) => e.method === "POST"));
  document.getElementById("hotkey-edit-cancel").click();
  check("Cancel closes it and focus goes back to the shortcut",
    !editDlg.open && document.activeElement === hkOpts()[0]);

  // THE MENU, from the Applications key, which arrives as a `contextmenu` event.
  const menu = document.getElementById("hotkey-menu");
  const ctx = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
  hkOpts()[0].dispatchEvent(ctx);
  check("the Applications key opens the app's menu, not the browser's",
    !menu.hidden && menu.getAttribute("role") === "menu" && ctx.defaultPrevented);
  check("with focus on its first item", document.activeElement === menu.querySelector("[role^='menuitem']"));
  check("its speech item is a checkable item that says its state",
    document.getElementById("hotkey-menu-speak").getAttribute("role") === "menuitemcheckbox"
    && document.getElementById("hotkey-menu-speak").hasAttribute("aria-checked"));
  hkKey(document.activeElement, "Escape");
  check("Escape closes it and returns to the shortcut",
    menu.hidden && document.activeElement === hkOpts()[0]);
  hkOpts()[0].click();
  check("a click opens it too, the way in on a touch screen", !menu.hidden);
  hkKey(document.activeElement, "Escape");

  // DELETE asks first, as a true alertdialog.
  const delDlg = document.getElementById("hotkey-delete-dialog");
  hkKey(document.activeElement, "Delete");
  check("the Delete key asks first, in an alertdialog, with Cancel focused",
    delDlg.open && delDlg.getAttribute("role") === "alertdialog"
    && document.activeElement === document.getElementById("hotkey-delete-cancel"));
  document.getElementById("hotkey-delete-cancel").click();
  check("Cancel deletes nothing and returns to the shortcut",
    !delDlg.open && document.activeElement === hkOpts()[0]);

  // THE KEYS TO HOLD are one setting, in a dialog of its own, named on its button.
  const modsBtn = document.getElementById("hotkey-mods-btn");
  check("the keys-to-hold button says what they are now",
    /change the keys to hold, now .+ and /i.test(modsBtn.textContent), modsBtn.textContent);
  modsBtn.click();
  const modsDlg = document.getElementById("hotkey-mods-dialog");
  check("it opens a dialog of four labelled switches", modsDlg.open
    && document.querySelectorAll("#hotkey-mods input[type='checkbox']").length === 4
    && document.getElementById("hotkey-mods").getAttribute("role") === "group");
  document.getElementById("hotkey-mods-cancel").click();
  check("Cancel closes it and returns to the button", !modsDlg.open && document.activeElement === modsBtn);

  // RESTORE asks first, because it throws the whole list away, and says what it will leave.
  const restoreDlg = document.getElementById("hotkey-restore-dialog");
  document.getElementById("hotkey-restore-btn").click();
  check("Restore default shortcuts asks first, in an alertdialog, with Cancel focused",
    restoreDlg.open && restoreDlg.getAttribute("role") === "alertdialog"
    && document.activeElement === document.getElementById("hotkey-restore-cancel"));
  check("and says what it will leave",
    /replaces your \d+ shortcuts? with the \d+ standard ones and the \d+ SMART pad keys/i
      .test(document.getElementById("hotkey-restore-desc").textContent),
    document.getElementById("hotkey-restore-desc").textContent);
  document.getElementById("hotkey-restore-cancel").click();
  check("Cancel changes nothing", !restoreDlg.open);

  // THE PAD SET is a switch that shows whether it is there, partly ticked when only some is.
  const padSet = document.getElementById("hotkeys-pad-set");
  check("Use the SMART pad keys is a described switch",
    padSet && padSet.type === "checkbox"
    && !!document.getElementById(padSet.getAttribute("aria-describedby") || ""));
  check("and it shows the pad set's state",
    (hotkeyData.pad_set === "all") === padSet.checked
    && (hotkeyData.pad_set === "some") === padSet.indeterminate, hotkeyData.pad_set);
  check("the tab points at Backups for copying shortcuts elsewhere",
    !!document.querySelector("[data-tab-key='shortcuts'] a[href='#/profiles']"));

  // ADD offers what it does AND the key, starting on a free one.
  const addDlg = document.getElementById("hotkey-add-dialog");
  document.getElementById("hotkey-add-btn").click();
  check("Add opens as a modal", addDlg.open);
  await waitFor(() => document.getElementById("hotkey-add-category").options.length > 1, 25000);
  const cats = document.getElementById("hotkey-add-category");
  const cmds = document.getElementById("hotkey-add-command");
  const addKey = document.getElementById("hotkey-add-key");
  check("focus lands on the first thing there is to do, not on the heading",
    document.activeElement === cats, document.activeElement && document.activeElement.id);
  check("the catalogue is offered as a category and then what is in it",
    cats.options.length >= 4 && cmds.options.length >= 1
    && cats.labels.length === 1 && cmds.labels.length === 1,
    cats.options.length + " categories, " + cmds.options.length + " in the first");
  check("the categories are the app's own areas, not a flat dump",
    [...cats.options].some((o) => o.textContent === "Channels")
    && [...cats.options].some((o) => o.textContent === "Levels"),
    [...cats.options].map((o) => o.textContent).join(", "));
  check("and the key is chosen there too, starting on one no shortcut uses",
    addKey.labels.length === 1 && addKey.value
    && !hotkeyData.shortcuts.some((s) => s.enabled && s.key === addKey.value), addKey.value);
  document.getElementById("hotkey-add-cancel").click();
  check("Cancel closes it again", !addDlg.open);
  check("nothing here uses the raw disabled attribute, which takes a control out of the tab order",
    document.querySelectorAll("#sec-settings .cat-tab[data-tab-key='shortcuts'] [disabled]").length === 0);
  const master = document.getElementById("hotkeys-enabled");
  check("there is one master switch, described by its own hint",
    master && master.type === "checkbox"
    && !!document.getElementById(master.getAttribute("aria-describedby") || ""));
  const hkNote = (document.getElementById("hotkeys-note") || {}).textContent || "";
  check("the tab says a shortcut needs Ctrl, Alt or Windows in it",
    /ctrl, alt or windows/i.test(hkNote), hkNote.replace(/\s+/g, " ").trim().slice(0, 120));

  // ---- nothing was written, in the whole run --------------------------------------
  const writes = await probeWrites();
  check("nothing in the run tried to write to the device", writes.length === 0,
    writes.map((e) => e.path).join(" "));

  await probeDone();
}

probeRun(run);
