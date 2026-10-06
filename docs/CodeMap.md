# AccessCaster: code map

Where things are, so a session can jump to the right code instead of reading the codebase.
**Update the PROSE in the SAME edit that adds, deletes, renames or moves a file, type or member.**

**EVERY NUMBER IN THIS FILE IS GENERATED. Do not edit one by hand:**

    python DevTools/codemap_lines.py            rewrite them from the source
    python DevTools/codemap_lines.py --check    report drift and exit 1, change nothing

That covers the `name` (NNN) line references, the line counts in the table below, and the DevTools
file count. It touches nothing else, so everything this file SAYS is still written by a person.

Maintained by hand these numbers go stale within the DAY: measured at 96 of 96 wrong, drifting by up
to 755 lines, because one day's work moves `server.py` and `app.js` by hundreds of lines. A
confidently wrong line number costs more than none at all, since you jump, land somewhere else, and
grep anyway, having been misled first. Run `--check` before you trust a number, and run the tool
after a session that moved code.

--------------------------------------------------------------------------------
## Project/ (the app)

| file | lines | what it owns |
|---|---|---|
| `server.py` | 11882 | HTTP API, every `action_*`, the announcer, the global-shortcut ACTIONS, the tray-facing lifecycle. The big one. |
| `soundpad.py` | 3005 | Pads: create, clear, reassign, voice FX, envelope, trigger mapping, pool hygiene, pad audio tidy. **Every pad read goes through the tree**: `pad_nodes` (each PAD node as a `Pad`, props by name; `pad_positions` and `_fields_by_padidx` are views of it) and `effect_objects` (each PADEFFECTS child as an `EffectObject`, with `detached` and `partial`). A dump that will not parse raises `rcp_read.ModelUnreadable`; there is no fallback reader. Orphan removal is `remove_orphan_objects` (bare bind + clear for an attached orphan, `_delete_effect_object` for a detached one that is last); `remove_partial_objects` deletes partial ones from the end, and `clear_slot` is the clear every in-use path calls, because an FX clear can leave one. `_sanitise_stack` is the ONE effect-stack cleaner (an empty result is allowed), with `validate_fx_stack` (must not be empty) over it. `_transfer_mode` is how every pad write enters and ALWAYS leaves transfer mode; `_replace_slot` is the clear-then-create every high-level create runs, and its `created` key (not `ok`, which also needs a clean pool) says whether the pad was made. **`engage_pad` / `unlatch_pad` are how a pad is FIRED and released from software** (a bare `padActive` at its position; injecting `padButtonPressed` does nothing). `heal_fx_pool` rebuilds any voice-FX pad whose region-B definition is missing, which is the state a factory reset leaves; `SYSTEM_PAD_TYPE` is the bank-8 filter every pad walker needs. |
| `rcp_transfer.py` | 1088 | Record BUILDERS, the send layer (`_write_records` under `send_record` / `send_records_paced`) and transfer mode. Every write shape lives here. `build_path_prop_int` / `_bool` address a TREE PATH and are the primitive; the older `build_channel_prop_*` selector-plus-trailer pair is a view onto them. **Every cross-point write goes through `set_cross_point_linked` / `_excluded` / `_level`**, each gated by `_check_cross_point`. Also the pad-volume mount gate: `find_pad_volume` -> `pad_volume_readable` (it READS BYTES, not just stats) and `quiet_drive_errors` (kills Windows' modal drive box). |
| `app_settings.py` | 228 | THIS APP's own settings, export and import: the keyboard shortcuts, announcement preferences and level warnings that NO profile carries. Pure, like `profiles.py`: it never imports the server and never touches a file. **The export is an ALLOW-LIST because `remote_token` lives in the same settings file** - see `EXCLUDED`, and `PROJECT_GUIDE` part 2. `count_shortcuts` exists because the stored `hotkeys` value is `{enabled, shortcuts}`, so `len()` on it is always 2. |
| `channels.py` | 1125 | Per-channel reads and writes, `CHANNEL_PROP`, strip resolution, the chain set. **`input_value(blob, ch, prop)` is THE reader for every per-input value** (mic gain, phantom, polarity, mic type, instrument gain, line level, input type, colour): one addressed lookup at `devices.input_path`, never positional and never a fallback chain. The five mic setters and `set_channel_level` write to that same address; `line_level_channels` says which channels have a Level at all. `mic_channel_number` = `source_code + 1`. The three per-strip toggles - `set_channel_mute`, `set_channel_solo` and `set_channel_wireless_mute` - are one family on `proc_selector(channel)`, and the wireless one is the mic's OWN mute, not the channel's. Also the OFF-FADER CHAIN STORE (`park_chain` / `take_chain`, `off_fader_chains.json`): a channel with no strip has no processing on the desk, so we hold it. |
| `devices.py` | 1106 | Device models and caps: **`CHANNELS_PRO2` / `CHANNELS_DUO` are THE channel table, and every channel list is a view over one of them** (`_ui_channel_view`, `_matrix_source_view`, `_assignable_view` and their `_expanded_*` twins): adding an input is one `Channel` entry, and the fader dropdown, the Channels page and the matrix rows cannot disagree. `Channel.key` is STORED USER DATA - rename the label, never the key. `FaderSpec` (its `level_selectors` are DERIVED from `count`/`physical` + `FADER_NODE_BASE`, never tabulated), `InputStripSpec`, `MatrixSpec` (**its `block` names the dump section the grid lives in and its `cell_count` counts THAT block only** - 390, the MIX section; see the routing-matrix entry in `PROJECT_GUIDE.md` part 2. **`all_sources` / `all_source_indices` is what a READER or a WRITER walks and `sources_for(expanded)` is only what the page OFFERS** - the desk publishes every row in both modes), `HeadphoneTypeSpec`, `PadSpec`. Pro II and Duo deltas. **`input_path(ch)` is THE address of a channel's input values, on every desk**: INPUTSOURCE[source_code], `input_table_base` 0x26E plus the code. `channel_for_input_path` is its reverse, for naming a change the desk pushed. There are no per-device input selector tables and there must not be again - read that method's comment before adding one. **`force_device` pins the active desk for OFFLINE TESTS ONLY** - never call it from app code. |
| `autostart.py` | 169 | "Start when you sign in": the HKCU Run value, SHARED WITH `installer.iss` (same key, name and command - `test_autostart.py` asserts it). `state()`, `set_enabled()`, `started_at_login()` (the `--startup` flag `tray.main` reads to come up with no page). Refuses from a source run. **`LEGACY_VALUE_NAME` is the pre-rename value "Access Caster"**: `read_value` falls back to it so a machine that has not been upgraded still reads as ON, and `set_enabled` deletes it whichever way it is called, because two entries naming the same exe start the app twice at sign-in. `installer.iss` deletes it too, ungated by the task. |
| `profiles.py` | 890 | Save, restore, `device_snapshot`, `UNIT_KEY`, `_apply_v2`. The routing matrix is captured as a MIX SCHEME (`routing.mix_scheme`, via `routing_schemes.capture`); the three older matrix keys are written for old builds only. |
| `tones.py` | 137 | The announcer's non-speech cues: two-note WAVs (falling = muted, rising = unmuted) generated into the user data dir. `tone_path(name)` makes one on demand and replaces any earlier shape, which is what `TONE_VERSION` in the file name is for. Played by the speech worker through SAPI, so they inherit the announcer's output device. |
| `wording.py` | 46 | Spoken-sentence helpers every module shares: `plural` / `count` ("3 mics", "was"/"were"), `join_and` ("A, B and C") and `describe_duration` ("2 minutes 5 seconds"). Use these rather than writing a plural or a list join inline. |
| `driver_install.py` | 189 | Fetching RODE's Virtual Devices driver: `ZIP_URL`, `PAGE_URL`, `install` (download, extract, `verify_signature`, hand the MSI to Windows). **THE APP'S ONLY OUTBOUND INTERNET CALL, and only from a button press.** The signature is the check and not a hash, because the URL carries no version and RODE replace the file in place; the signer insisted on is `Freedman Electronics`, their legal entity. Every failure raises `DriverInstallError` carrying a sentence that names the manual route. |
| `routing_schemes.py` | 437 | Mix schemes: `capture` (`only=<output key>` makes a ONE-OUTPUT preset), `apply`, `describe` (its `expanded` flag changes the spoken source COUNT and nothing else), `scope_text` (the "(every output)" / "(Chat only)" suffix the dropdown reads, derived per desk), `builtins` (normal / chat-mic-only / chat-normal), the flat JSON store. Keyed by output and source KEY, and walking `MatrixSpec.all_sources`, so a scheme's shape does not depend on the desk's USB mode. |
| `meters.py` | 887 | WASAPI metering, `Watcher`, `_EngineWaker`, `trusted_ceiling`, `level_note`, endpoint volume, `_default_endpoint_name` (both `default_playback_name` and `default_recording_name`). Also the shared MMDevice COM declarations, including `IMMNotificationClient` (Windows' endpoint-change callback, implemented in `virtual_devices`). |
| `virtual_devices.py` | 743 | **RODE's Virtual Device Driver, from this side.** `read_status` / `wait_connected` parse the service's shared status block (`STATUS_MAPPING`; `DriverStatus.phrase` is the sentence for a person), `service_state` reads the service process from the system process list (no elevation) and `record_bind` appends it, per bind attempt, to `logs/bind_history.jsonl`, `IPolicyConfig` is the Sound-panel interface (enable an endpoint, set the default device, no elevation), `_endpoints` tags every endpoint `virtual` (the driver's, enumerator ROOT) or `desk` (the unit's own USB audio), **`reconcile` makes Windows agree with the service** (endpoints on while connected, off while not, `faders_missing` when a held pin stopped the service creating them, `_restore_default` for the output), `describe` is the page's driver line, and **`watch_endpoints` (+ `_EndpointNotifier`) reconciles on every endpoint change Windows reports** rather than on one sample. See PROJECT_GUIDE part 2 under Expanded before touching any of it. |
| `pad_records.py` | 324 | The pad CREATE record builders (sound, voice FX, mixer, MIDI trigger) and `free_handle`. Every handle is a required argument; the remount record and `_frame` come from `rcp_transfer`. Tested by `test_pad_records`. |
| `callme.py` | 752 | **CallMe, RODE's remote guests.** `read(blob)` is the whole page from one dump, and `read_all` returns it with the control and slot paths from the same parse (passwords never included; `slots` trimmed to what the tier allows via `TIER_SLOTS`; `on_fader` says whether CallMe 1, source `CALLME1_SOURCE` 16, is on a fader). `build_answer(call_id, slot, path)` is THE answer and decline write, a composite with a FALSE bool, `build_hangup(path)` is TWO records because a hang-up is an edge, `build_extend(path)` lowers then raises the Extend pair, and `build_dial(code, slot, path)` places an outgoing call (all three in PROJECT_GUIDE part 2 under CallMe). `build_hosting`. `normalise_code` takes a typed code, `rode:<code>` or a whole guest link; `peer_name` speaks a dialled desk as its contact name, else its spelt-out code; contacts are `clean_contacts` / `set_contacts` (the server hands over the stored list) / `contact_name` / `save_contact` / `delete_contact`, pure apart from the one code-to-name table; `CONNECTED` / `UP` are the states that count as a call being connected or up (Expiring included); `sentence` capitalises a line that may open with "a guest". `paths(blob)` finds the control node and the three slot nodes by the property they carry. The wording: `status_sentence`, `call_now` (the one line on the page saying what is happening), `call_details` (bitrate, audio lost and jitter as label and value pairs), `end_label` (Decline or Hang up), `plan_lines` (label and value pairs, one fact to a line on the page), `end_reason`, `spell` (a code one character at a time), `renewal_date`, `utf8` (the desk sends UTF-8, the decoder reads latin-1). **`Watcher` is its own read-only HID handle**, like the recorder's, so calls are tracked with announcements off; it re-seeds from a dump every time the handle opens, speaks through `on_say`, and `note_handled` / `quiet_connect` / `quiet_end` stop a call this app answered or hung up being announced twice. |
| `recorder.py` | 648 | Recorder state, storage volumes, marker drop, elapsed time, the recordings list (`read_recordings` / `parse_recording`), the delete (`SEL_RECORDINGS`, `build_delete_request`), **the spoken wording in TWO forms that must not be confused - `STATE_LABEL` / `STATE_SPOKEN` for what the desk IS doing, and `spoken_change(was, now, medium)` for what just HAPPENED** (a drive going in or out is only tellable from the transition; see PROJECT_GUIDE part 2), and the labels for BOTH multitrack destinations (`MULTITRACK_LABEL` = the unit's, `USB_MULTITRACK_LABEL` = this computer's) and **ALL THREE of the Recording page's hints** (`MULTITRACK_HINT`, `RECORDING_FORMAT_HINT`, `BYPASS_HINT` / `BYPASS_NEEDS_MULTITRACK`) plus `RECORDING_FORMAT_LABEL` (WAV vs MP3 - TWO members only; see PROJECT_GUIDE before adding a third). The browser holds none of this wording. |
| `shows.py` | 370 | The DESK'S own backup (not profiles): `SEL_SHOWCONTROL` 0x28E, `read_shows` (newest first, sorted on `showSnapshotIndex`), `find_by_snapshot`, `read_current`, `send_export` / `send_import` / `send_new_from_default` / `send_delete`, `wait_for`, `error_message`, `restore_fingerprint`. **The handle is the snapshot index, never the uid (positional) and never the uuid (names the show, so backups collide).** **`restore_fingerprint` is how an import proves it ACTED**: the uuid already equals the target in the ordinary case and the command field self-clears to its idle value, so only the desk state can answer. |
| `model_tree.py` | 238 | **THE PARSER, and it OWNS THAT MODULE NAME.** The dump is a self-describing tree; this reads it and every byte is accounted for. `Node` (with `path()` and `address()`), `parse`, `walk`, plus `instances` / `find_property` which only `DevTools/probe_path_write.py` and `tree_tool.py` call - they are the tool-facing half and are NOT dead. **`parse_record` decodes ONE addressed record** (the shape of both our writes and the desk's pushes) and RAISES on anything malformed, deliberately, because `rcp_read._push_record` uses it as a test rather than a decoder. The command line over it is `DevTools/tree_tool.py`, named differently ON PURPOSE: with two files called model_tree.py, `import model_tree` picks whichever comes first on sys.path. |
| `rcp_read.py` | 851 | Model dump reading, ALL of it served from ONE cached tree parse per dump (`_index`, which indexes `by_name` AND `by_path`). **`parse_push(frame)` is THE decoder for anything listening to the desk** (the announcer, the recorder and CallMe watchers, `PushListener`): it returns (path, name, value, kind) for a frame that is exactly one push record and None for anything else, a model-dump slice included. **`read_model_raw` ENDS ON THE LENGTH THE DUMP DECLARES and SKIPS asynchronous push frames** (`_push_record_len`); `_last_read` records what the last dump did, `bounded_by` saying "length" or the "silence" fallback. Both are load-bearing - see "Reading the model dump" in `PROJECT_GUIDE.md` part 2 - and `DevTools/test_read_under_push_live.py` is their proof. **`values(blob, name, kind=None)` is THE way to read a list of values and `first(...)` the way to read one** (first MEANINGFUL: it skips None and "", keeps False and 0); `find_all` under them returns (value, kind) pairs and is for positional reads that need the kind. **`node_at(blob, path)` / `path_value(blob, path, name, default)` are the ADDRESSED reads** - the counterpart to the `build_*` writers, and what anything belonging to ONE channel must use; `MISSING` is their "no such property" sentinel, distinct from a real None/0/False. Also `iter_properties`, `snapshot_properties`, `block_value`, `block_values` (**the block-scoped `values`, and what any positional read of a name two blocks share must use** - `MIX`/`RCSYNCMIX` and `MIXMINUSES`/`RCSYNCMIXMINUES` each share six), `iter_block_properties`, `master_processing` (the master Compellor + delay block the Routing page and a profile share), and `_read_matrix` (ONE grid read, with `read_matrix_state` / `read_matrix_levels` over it). `read_settings` is a RAW STREAM-ORDER diagnostic, not a per-channel read. **There is no fallback reader**: an EMPTY blob reads as an empty model (a desk that has stopped answering; test with `is_model_dump`), any other blob that will not parse raises `ModelUnreadable`, and `read_model_stable` only accepts a dump that `parses`. **`PushListener` is how a command's result is now learned: LISTEN, do not re-dump the tree until the answer shows up.** Own read-only handle, no HID lock, dump frames filtered by `_push_record_len`, frames queued from the moment it opens (open it BEFORE the write - the burst starts 0.02 s after). `wait_for(name, timeout, match=)` for the answer, `wait_until_quiet()` for "the desk has finished", which is a DIFFERENT question - see part 2 of the guide. `lost` is a hint that the desk left the bus, never a proof. |
| `rcp_hid.py` | 533 | HID transport and the lock. **`open_handle(iface, purpose)` / `close_handle` / the `handle` context manager are how EVERY handle in the app is opened**, so the diagnostic log can say who holds the device and for how long; `hid` is imported HERE ALONE. `purpose` is named after the job ("model dump", "pad create", "announcer"), because the log is read by somebody asking what the app was doing. `find_control_interface` answers from a remembered interface list (`refresh_interfaces`, `forget_interfaces`, `fresh=True`; PROJECT_GUIDE part 2). |
| `diagnostics.py` | 416 | **THE DIAGNOSTIC LOG**, `%LOCALAPPDATA%\AccessCaster\logs\diagnostic.log` with one rollover. `start()` (called ONLY from `server._bring_up_server`, which every entry point funnels through, so the file has exactly one writer), `event(kind, detail, **fields)`, `command(rec, why)` (decodes a report-3 record to `property = value at [path]`), the `phase` context manager (start / end / how long), `flush`, `stop`, `log_path`, `folder`. It TEES stdout and stderr, which is what makes every existing `print` visible in the windowed build, and installs `sys.excepthook`, `threading.excepthook` and `faulthandler`. Identical events collapse into one `repeated ... xN` line; the FIRST of anything is always written immediately. Silent until `start()`, so the offline suites and DevTools write nothing. |
| `autogain.py` | 136 | Auto gain measurement and the gain decision, off a WINDOWS endpoint (`chat_endpoint` -> `measure_endpoint` -> `verdict`). |
| `hotkeys.py` | 568 | **The Win32 half of global keyboard shortcuts, and it knows nothing about the desk.** `MODIFIERS`, `KEYS` (incl. `KEY_DASH` / `KEY_EQUALS`, the two at the end of the number row the pad scheme uses), **`DEFAULT_MODS` = Ctrl+Alt+Shift, the chord a fresh install starts on** (read its comment before changing it: it moved off Ctrl+Alt+Windows because that chord cannot have the digits), `combo_text`, `parse_binding` (the validation, incl. "must carry Ctrl, Alt or Windows"), `HotkeyManager` (holds the USER'S LIST of {id, command, mods, key, enabled, speak}: `load`, `saveable`, `add`, **`add_scheme`** (lays a whole named SET onto an existing list, which `load(defaults=)` may never do), `remove`, `get`, `set_binding` (key, on/off, speech), **`set_mods` (the ONE chord every shortcut is held on, `_mods`)**, `reset` (Restore default shortcuts), `set_enabled`, `state`, `start`/`stop`/`reload` (which WAITS for the pump to apply it, so `add` and `set_binding` report their own registration), and the `_pump` thread that both registers and pumps). It calls out to a `runner` and a `labeller`; what a command MEANS is `commands.py`. |
| `commands.py` | 545 | **The catalogue of everything a shortcut can be pointed at, GENERATED from the app's own spec tables** (channels, faders, pads, mix schemes, presets) - never hand-written. `catalogue()`, `label_for()`, `run()` (splits the id on the FIRST DOT), `CATEGORIES`, `LEVEL_STEP`, `PAD_TYPES` (all three kinds since MIDI), **`speaks_by_default`** (the ONE place that decides a shortcut arrives silent; pad firing does), `_pad_control_commands` (bank / slot / what-is-playing, no model dump so they render instantly) vs `_pad_commands` (one per real pad, needs a dump). Imports `server` INSIDE the functions, because server imports this. |
| `midi_out.py` | 233 | **The desk's own USB MIDI port, and the ONLY way to play a sound pad.** winmm through ctypes, no package and no driver. `ports` / `find_port` (prefix match: Windows truncates a port name at 31 chars), module-level `send_cc` / `press_pad` / `release_pad` / `play_pad` over ONE long-lived shared handle (lazy open, one reopen on a failed send, which is what a replug looks like), `MidiOut` as a context manager for tools, `close`, and `midi_enabled()` which is FOR TOOLS ONLY - the app arms MIDI at the point of use and leaves it on. **CC 35 is play-a-pad and the MIDI CHANNEL is which pad**, within the current bank; the bank is an ordinary HID write (`soundpad.select_bank`). |
| `tray.py` | 223 | pywin32 system-tray entry point (this is what ships). Menu: Open, Announcer, **Diagnostic log** (`_open_log`, which opens the file in NOTEPAD rather than handing it to the shell - a `.log` often has no registered handler and `os.startfile` would open the "how do you want to open this" chooser, a dialog with nothing in it to read), Quit. |
| `paths.py` | 74 | `%LOCALAPPDATA%\AccessCaster` and friends. |
| `page.py` | 62 | `render_index()`: splices `static/help.html` into `static/index.html` at the `<!-- INCLUDE help.html -->` marker, cached on both files' mtimes. **Server-side, not a browser fetch**, so the delivered DOM is identical to the section being written out inline and neither the router's focus target nor `buildStaticTabs` can race it. **Both servers go through it** - `server.py` and `DevTools/ui_probe.py` - or the probe's checks would see a page with no Help & About in it. Raises if the marker is gone rather than serving the page without its Help section. |
| `version.py` | 392 | Version string, manually bumped, plus `HISTORY`, the release notes' only source. A correction to the help earns no entry there. |
| `say.ps1` | 92 | SAPI speech, and it SELF-GATES on the foreground window (silent when Claudio has focus), so it is always safe to call. |
| `say_worker.ps1` | 130 | The speaking half `say.ps1` hands off to. |

### server.py landmarks

- Specs that drive read + write + UI + profile from one place: `PROCESSING_CONTROLS` (174),
  `UNIT_SETTINGS` (1475), `_PRESET_PROPS` (4951).
- USB mode and RODE's virtual devices: `action_set_usb_multitrack` / `action_set_usb_expanded`,
  then `_virtual_devices_follow` directly under them (the thread that runs
  `virtual_devices.reconcile` and speaks the outcome; also called from the announcer's reconnect
  in `_announce_loop` and, delayed, from `_bring_up_server`). Its helpers sit just above it:
  `_write_usb_mode` (writes the mode, says whether the product id changed),
  `_write_usb_mode_parked` (the same with Windows' default parked off the desk across an Expanded
  arrival: `_usb_mode_arrives_expanded` decides, `_park_for_arrival` parks and guards, using
  `virtual_devices.park_output` / `keep_parked` / `default_to_system`; on leaving Expanded,
  `_usb_mode_leaves_expanded` parks with no guard and `virtual_devices._wait_for_desk_main` holds
  the move back until the desk's Main output is live; `virtual_devices._note_default` marks a
  default Windows moved by itself as displaced, for a leave nobody parked), `_desk_usb_mode` (the
  re-read before each retry) and `_virtual_devices_expectation`. `_virtual_devices_repair` sits beside
  it as the silent half: the callback `virtual_devices.watch_endpoints` runs on every endpoint
  change Windows reports, sharing `_virtual_devices_busy` so it never fights a mode change. The
  routing read sends `usb_driver` for the page's driver line.
- Global shortcuts. The KEYS are `hotkeys.py`, the CATALOGUE is `commands.py`, and the two meet only
  in `hotkey_manager()`. Here: the actions the catalogue calls into (`_hk_announcer`, `_hk_record`,
  `_hk_mics` with `_hk_muted_mics` above it, `_hk_state`, `_hk_window`),
  `_hotkey_defaults()` (what a fresh install gets) and `_pad_hotkey_scheme()` (the pad half on its
  own, so the "Use the SMART pad keys" switch, `action_set_pad_shortcuts`, can add or remove it),
  `_HOTKEY_V1_COMMANDS` + `_hotkeys_migrate` (carries an old settings file across),
  `_hotkey_label` (cached, because the catalogue can touch the desk), `hotkey_state`,
  `hotkey_commands` (the only part that reads the desk), `_hotkey_page_state` (adds `pad_set` and
  the default counts), `action_set_hotkey`, `action_set_hotkey_mods`, `action_add_hotkey`,
  `action_delete_hotkey`, `action_restore_hotkey_defaults`, `action_set_hotkeys_enabled`. Registered in `_bring_up_server` under
  `shortcuts=`, released in `stop_server`.
- **Firing a pad from a shortcut.** `_hk_fire_pad` dispatches PER PAD by type: `_hk_pad_play`
  (sound, over MIDI, selecting the bank first) or `_hk_pad_toggle` (voice FX / mixer, over HID,
  state in `_hk_pads_on`). Entry points `_hk_pad` (a pinned padIdx), `_hk_pad_slot` (pad N of the
  current bank), `_hk_bank_step`, `_hk_playing_now`. Around them: the pad cache (`_hk_pad_map` /
  `_hk_refresh_pads` / `_hk_pad_entry` / `_hk_maybe_refresh_pads` / `_hk_pads_stale` / `_hk_pad_verify`,
  the "did the RIGHT pad fire" check on the HID path only) - **the cache is a TTL plus explicit
  invalidation, never a refresh per press; the per-press version is what made pad firing flaky**;
  the bank cache (`_hk_note_bank` / `_hk_current_bank`); the MIDI arming (`_hk_arm_midi` /
  `_hk_rearm_midi`, once per session, re-armed after a profile restore); HOLD-mode notes
  (`_hk_pads_held`, `_hk_release_held_pads`, `_hk_release_held`); and the silent play tracking
  (`_hk_note_pad_progress` / `_hk_note_pad_active` / `_hk_play_snapshot` / `_hk_playing_from_model`,
  fed from `_ann_handle_report` and never spoken).
- Expanded tidy-up, before `action_set_usb_expanded`: `_clear_extended_faders` (empties every fader carrying Game, Music, A or B, parking their chains) and `_tidy_after_standard` (the same, worded and spoken, for a desk found in Standard at start-up via `_seed_usb_expanded`, or after it leaves Expanded on its own, from the announcer's reconnect).
- Card and drive, after `action_delete_recording`: `_storage_gate` (cap `storage_write`, Pro II only), `_storage_target` (refuses any volume that is not a recording destination, so never the internal pad storage), `action_storage_eject` (`on=False` reconnects), `action_storage_erase` (listens for the desk to clear its flag). Browser half: `fillStorageChoices`, `applyStorageButtons`, `onStorageEject`, `onStorageErase`, and `loadRecordingFrom`, which draws the Recording page from any reply that carries its payload.
- Firmware updates, after `read_device_state`: `_firmware_update` (the SYSTEM node's update fields), `_firmware_update_sentence`, `action_check_firmware` (writes `updateCheckRequested` and listens for `updateChecking` to clear; never installs). Shown by `firmwareLine` / `onFirmwareCheck` in app.js and spoken by `summariseOverview`.
- Device state and overview: `read_device_state` (341), `device_overview` (1760),
  `_overview_routing` (1672), `_wireless_mics` (314), `read_device_status` (1866).
- Recording: `read_recorder` (660), `action_record` (754), `action_drop_marker` (825),
  `_recorder_watcher` (560), `_recorder_sentence` (700), `action_delete_recording` (865, refused mid-take), `_recorder_payload` (609, the ONE builder
  for the page and for every transport reply), `_recorder_state_changed` (502, the Watcher's
  callback: it words the change via `recorder.spoken_change` and owns the announcer suspend/restore
  round a take), `action_set_usb_multitrack` and
  `action_set_processing_bypass` / `action_set_usb_bypass` (the USB pair is ROUTING's:
  `/api/routing/usb-multitrack` and `/api/routing/usb-bypass`, built by `loadRouting` into the
  `usb` tab and valued by `applyRoutingValues`). The driver line under them is `applyUsbDriver`,
  kept current by `pollRoutingDriver` on `/api/routing/driver` (`read_usb_driver`) while the Routing
  page is open; the same poll's `switching` drives `setUsbSwitching`, which holds both USB Apply
  buttons until the server's `_virtual_devices_following` is released, and re-reads the page's values
  when it is. The fader dropdowns' channel list changes with the mode: the live read carries it as
  `fader_channels` (`_fader_channel_options`) and `applyFaderChannels` rebuilds the options when it
  differs. Writers in `rcp_transfer`:
  `set_recording_processing_bypass`, `set_usb_multitrack_mode`, `set_usb_multitrack_bypass`, all on
  `SEL_OUTPUT_GLOBAL` 0x0D beside `set_multitrack_mode`.
- CallMe (straight after the recorder): `_callme_watcher` (started in `_bring_up_server`, seeded
  by `_callme_dump`), `_callme_gate` (cap `callme`), `read_callme` / `read_callme_live`
  over the ONE builder `_callme_payload`, `_callme_state_now` (the Watcher's state when seeded, so a
  hotkey answer needs no dump), `action_callme_hosting`, `_callme_respond` behind
  `action_callme_answer` / `action_callme_decline` (waits for the slot to go Active before it says
  connected), `action_callme_dial` (replies once the call is ringing or has failed),
  `action_callme_hangup` (by slot, or the one call that is up), `action_callme_end`
  (decline if ringing, else hang up: the default D key), `action_callme_extend` (the answer key
  calls it when nothing is ringing and a call is Expiring), `_callme_on_expiring` /
  `_callme_auto_extend` (the `callme_auto_extend` setting), `callme_status_sentence`. Contacts live in
  settings.json as `callme_contacts` (portable in an app-settings export): `_callme_contacts`,
  `action_callme_contact_save` / `_delete`, and a 404 on a saved contact's code says the code may
  have changed. Shortcuts in `commands._callme_commands`, one "Call <name>" per contact, bound by
  name so editing a code keeps the key.
- Remote access (the token gate): `access_token` / `new_access_token` (settings.json,
  `remote_token`), `_lan_address`, `read_access`, `action_new_access_token`, and in the handler
  `_token_offered` / `_authorised` / `_refuse`. **Local requests bypass it; `/api/access` and
  `/api/access/new` are in `LOCAL_ONLY_ROUTES` so the key never leaves this machine.** The page load
  is the only place the cookie is set.
- Network: `read_network_status` (1959), `action_join_wifi` (2210), `action_set_bt_visible` (2283),
  `_network_write_gate` (2411).
- Announcer: `_announce_loop` (3694), `_ann_handle_report` (3007), `_ann_seed_layout` (3221),
  `_ann_publish_level` (3617), `_ann_publish_mute` (3646), `_ann_publish_solo` (4427),
  `_ann_mark_self_write` (3576), `_ann_is_self_echo` (3585), `_suspend_announcer_for_record` (3832),
  `action_set_announce` (3862) (its `chosen` flag decides whether a toggle is saved), `announcer_starts_on` (2905)
  and `action_set_announce_remember` (3927), the "Start with announcements the way I left them" option.
  A mixer pad's `padActive` silences the mute and cue lines for `_ANN_MIXER_QUIET_S` (2861), using
  the positions `_hk_refresh_pads` caches in `_hk_mixer_pos`.
- Auto gain: `_autogain_run` (4516), `_ChatIsolation` (4631), `_autogain_restore_processing` (4715).
- Per-channel level check: `action_channel_level_check` (6419), `_chan_check_stop` (6359),
  `_level_check_summary` (6288).
- Meters and the Windows level: `read_meters` (6022), `action_set_input_level` (6125),
  `action_meter_watch` (6254).
- Faders and chains: `action_set_fader` (5744), `action_reset_faders` (6570),
  `_follow_chains` (5260), `_read_fader_vector` (5452), `_tidy_fader_target` (5699).
- Per-fader mute and solo (the Levels tab's rows): `action_set_fader_mute`, `action_set_fader_solo`,
  read from `fader_levels_live`; browser half is `onFaderMute` / `onFaderSolo` / `onClearFaderSolos`.
  By FADER rather than by channel name, so an empty strip and a linked pair both work.
- Stereo-linked inputs: `action_set_stereo_link`, `stereo_link_state`, `_stereo_link_gate` and
  `_carry_mute` (which puts back the mute the desk clears when it rearranges a layout). The pair
  table is `devices.FaderSpec.stereo_pairs` and `rcp_transfer.STEREO_LINK_PAIRS`; the browser half is
  `buildStereoLinks` / `onStereoLink` / `applyStereoValues` in `app.js`.
- Routing: `routing_model` (6827), `action_set_cross_point` (7703), `action_reset_all_outputs`
  (6314, a wrapper round the "everything normal" built-in scheme), `_apply_matrix` (6758, which
  applies a profile's `routing.mix_scheme` through `routing_schemes.apply` and keeps the old
  index-keyed branch only for profiles saved before 29th August 2026).
- Mix schemes: `_apply_scheme` (7753, the ONE apply path: gate, lock, write, wording),
  `read_routing_schemes` (7787), `action_apply_routing_scheme` (7823),
  `action_save_routing_scheme` (7837), `action_delete_routing_scheme` (7866). The format and the
  writes are in `routing_schemes.py`.
- **Background jobs, all four on ONE class: `_BackgroundJob` (`start` / `status` / `busy` /
  `step` / `tick`).** The instances are `_profile_job`, `_restore_job`, `_show_job` and
  `_pad_restore_job`; each `action_*` passes a `work(job)` that RETURNS its result dict and the
  class writes the finish, including turning a crash into a sentence. `step`
  reports without advancing the count, `tick` advances it; `phrase(msg, n, total)` words a counted
  step. The page only ever reads `message`, `done` and `ok`.
- **`wake_channels` (+ `/api/channels/wake`): the dead-mic fix.** Nudges each mic gain one step and
  straight back after every reset and import, because a show import never re-programs the ANALOG
  mic preamp and leaves the channel ~55 dB down while the model reads perfect. Nothing can detect
  it by reading; see `PROJECT_GUIDE.md` part 2 before touching this.
- ONE backup, both mechanisms underneath: `action_backup_now`, `action_restore_backup` (**always the profile since 21st September 2026**: the show import crashes the desk about 1 in 6, see its docstring), `backup_restore_plan` (what the confirmation dialog says, including `warnings` - what the backup will leave you unable to hear, via `_restore_silence_warnings`), `_removable_volume_ready`, `_backup_wording`.
- Profiles and the as-found baseline: `snapshot_profile` (8227), `restore_profile` (8376, and it
  takes `clean`: reset the desk first, then restore on top), `baseline_status` (8298),
  `action_baseline_save` (8330).
- The desk's own Shows (all after `restore_profile`): `read_show_state` (the page payload, and the
  ONE place the two option labels are built), `show_job_status`, `_run_show_job` (one job state for
  all four, since they are mutually exclusive), `_shows_write_gate`, `_export_and_wait`,
  `_reset_and_wait`, `_clean_restore_reset` (refuses to reset if the backup did not land),
  `action_show_export` / `action_show_import` / `action_show_delete` (a thin wrapper over
  `_show_delete_work`, which the unified delete also runs so both halves share one job) /
  `action_show_reset`.
- **THE BACKUPS PAGE, which is the ONE thing the page calls**, and the mechanisms above are what it
  picks between: `read_backups` (the whole list in ONE device read - profiles, their paired shows,
  and shows the unit made with no profile beside them), `action_backup_now` (a show when there is
  storage, ALWAYS a profile, paired under one name), `backup_restore_plan` (which half WOULD run,
  for the dialog), `action_restore_backup` (`clean=True` forces the profile path; the reply's `via`
  says which job to poll), `action_delete_backup` (both halves, `_delete_show_then_profile`, with
  the as-found two-step asked BEFORE anything is deleted).
- Wifi scanning: `parse_scan_slot` (the `<SSID>----<security>` split, from the RIGHT since an SSID
  may contain hyphens), `action_scan_wifi`, `WIFI_SCAN_SLOTS` / `WIFI_SCAN_SETTLE_S`. Sits just
  above `_network_write_gate`.
- Capability gates, one per risky write family, and all but one now a one-liner over **`_cap_gate`**
  (which holds the shared reasoning: a selector proven on a Pro II is NOT proven on a Duo, and a
  wrong one can append a stray only a factory reset clears). `_fader_write_gate` (5371),
  `_matrix_write_gate` (7637), `_output_write_gate` (7645), `_network_write_gate`,
  `_shows_write_gate`, `_recorder_gate` (with `_recorder_refusal`, the sentence the Recording
  page's delete note shares). **`_level_write_gate` (5831) keeps its own body** because it has a SECOND
  condition beyond the cap: the device must also carry confirmed faderLevel selectors. Each keeps
  its own refusal sentence, because what the user can and cannot do differs per family.
  `_chain_follow_on` (5250) is not a gate of this kind.
- **`_pad_write` is the shape of every live pad edit**: range-check the grid, take the HID lock,
  run the call, turn a crash into a sentence. Seventeen `action_set_pad_*` / `action_clear_pad` /
  `action_assign_fx` functions had these eight lines written out verbatim. `invalid=` is a refusal
  for a value it cannot check itself (checked AFTER the range and BEFORE the lock);
  `plain_value_error=True` surfaces a ValueError's own text, which soundpad writes as a finished
  user-facing sentence.
- Lifecycle: `_bring_up_server` (11781), `_close_previous_instances` (11746), `_client_watchdog` (135),
  `Handler` (11164).

--------------------------------------------------------------------------------
## Project/static (the web UI)

`app.js` 8748 lines, `index.html` 1966, `style.css` 319.

**`help.html` is the Help & About page (Alt+8), spliced into `index.html` by `Project/page.py` where
the `<!-- INCLUDE help.html -->` marker sits.** A fragment, not a page. It is the only part of the UI
that is prose rather than markup, and the app's ONLY statement of what it cannot do (the Limitations
tab was retired into its intro). `buildStaticTabs` builds its tab strip exactly as when it was
inline, because what the browser receives is unchanged.

- Tabs: **`wireTabStrip` is the ONE tab strip** - the roving tabindex, the arrow/Home/End keys, the
  remembered tab, the "focus the tab before you hide the pane focus is in" rule, and the panel's own
  tab stop. `buildTabs` (305, JS-built pages) and `buildStaticTabs` (358, pages written in
  index.html) are thin adapters over it and differ only in where the panes come from: built, or
  found in the document. Also `tabSection` (336, sub-headings inside a merged tab),
  `setPanelTabStop` (164).
- **Dropdowns that change the desk apply on Enter: `enterToApply` (the rule), `makeStagedSelect`
  (a labelled row built on it), `setStagedValue` (the ONLY place a device value is recorded),
  `revertStaged`, `markStaged` (the on-page waiting line), `addDescription` (adds a hint after the
  Enter one).** Used by fader assignment, the output
  mix mode, headphone type, both USB audio settings, what to record and its format, mic type, the
  mic processing preset, the
  Unit settings choices, the pad Settings dropdowns and the MIDI trigger choices. A dropdown that
  only changes what you are looking at acts at once; the metered stream waits `SELECT_COMMIT_MS`.
- Rebuild guards: `fillOptions` (3383), `rebuildIfChanged` (3411), `setLevelValue` (5634),
  `markLevelsUnread` (5612), `applyUnitSettingValues` (1402).
- Live region: `announce` (18). It REPLACES, it does not queue.
- Confirm modal: **`confirmAction`** (every confirm in the app: pads, mix schemes, deleting a
  recording or a custom effect, and the access link). Its DOM ids are `pad-confirm-*`. A mic
  preset is deleted through its own dialog instead (`openPresetDelete`, `preset-delete-dialog`).
- Remote access panel: `loadAccess` / `wireAccess` (Device > **Remote access**, its own tab, last
  on the strip). `SUPPORT_URL` + `wireSupport` (Help & About > Support this project) are next to
  `wireAboutTabs`.
- Server watch: `initAppLifecycle` (the heartbeat), `noteServer` (each ping reply carries the
  server's `_INSTANCE_ID` and version: three misses open `server-lost-dialog`, a new version
  reloads the page), `openServerLost` / `closeServerLost`. Pinned by `ui_checks_lifecycle.js`.
- App shell: `summariseOverview` + `speakStartupSummary` (the spoken startup state, by exception),
  and `loadVersionBeforeRender`, which the first render awaits so the app heading carries the version
  before focus lands on it. Pinned by `DevTools/ui_checks_shell.js`.
- Keyboard: `bindPageBoundary` (9164, the end-of-page hard stop).
- Router: `render` (8915). Its `focusTo` argument decides where focus lands: `init` passes `"nav"`
  so an opening window starts on the current page's nav link, and a route change takes the default
  and focuses the section's h1.
- Page loaders: `loadVersion` (475, `/api/version`, called from init so the Help & About version is
  there without visiting Device), `loadStatus` (502), `loadOverview` (814), `loadChannels` (2338),
  `renderChannelPanel` (2450), `loadPads` (3477), `loadMonmixOutput` (5389),
  `loadFaderLevels` (5438), `buildMatrix` (5757), `onMatrixOutputChange` (5894, which also words each
  source switch, including the source that matches the output).
- Mix schemes: `wireSchemes` (6007), `loadSchemes` (6023, fills the dropdown; option values are
  `builtin:<id>` or `saved:<name>` so the two name spaces cannot collide), `onSchemeApply` (6108).
- Recording: `fillRecordingList` (7876), `fillRecordingDeleteChoices` (8005),
  `onDeleteRecording` (8035). Everything on that page speaks through `setRecState`, the page's
  ONE live region.
- Builders: `buildLevelSlider` (5648), `buildScaleFields` (3956), `buildTriggerFields` (4120),
  `buildBackChannelList` (3921), `renderCustomFxList` (4669), `fillSpeechChoices` (1665).
- Specs: `REC_TRANSPORT` (7472). Never delete a `label:` key from one.
- Progress dialogs: `pollProfileJob` (6747).
- The factory reset (after `doRestore`): `loadShows` (can it be reset, can it back itself up),
  `openShowConfirm`, `onShowBackupFirstChange`, `doShowConfirmed`, `runShowJob`. Export, restore and
  delete of the unit's own shows go through the one Backups list.
- Wifi scanning: `onWifiScan`, `onScanPick` (fills the NAME box rather than being a second source of
  truth for it).
- Keyboard shortcuts (Device page, tab key `shortcuts`): `wireHotkeys` (all the listeners, called
  from init), `loadHotkeys`, `applyHotkeyState` (the ONE place a payload becomes the whole tab),
  the listbox (`renderHotkeyList`, updated in place by id; `onHotkeyListKey`; `selectHotkey`), the
  menu (`openHotkeyMenu`, `onHotkeyMenuKey`, `runHotkeyMenuItem`), `sendHotkey`,
  `announceAfterFocus`, `onHotkeyPadSetToggle`, and the dialogs: Edit (`openHotkeyEdit` /
  `onHotkeyEditSave`), keys to hold (`openHotkeyMods` / `onHotkeyModsSave`), Add
  (`onHotkeyAddOpen` / `fillHotkeyAddCommands` / `onHotkeyAddConfirm`), Delete
  (`onHotkeyDeleteOpen` / `onHotkeyDeleteConfirm`), Restore (`onHotkeyRestoreOpen` /
  `onHotkeyRestoreConfirm`), all closed through `closeHotkeyDialog`.
- `dropRetiredGroupState` (9352) drops the whole retired `accesscaster.groups` key.

Page shortcuts (`bindShortcuts` in app.js is the source): Alt+1 Channels, Alt+2 Routing, Alt+3 Pads,
Alt+4 Recording, Alt+5 CallMe, Alt+6 Backups (route `profiles`, section `sec-profiles`), Alt+7
Device (route `status`), Alt+8 Help &amp; About (route `about`, section `sec-about`).

- CallMe page: `loadCallme` (one dump on entry), `pollCallme` (the free `/api/callme/live` poll,
  redrawing only when the server's `version` moves), `applyCallme` (text only, never the
  controls), `fillCallmeList`, `onCallmeRespond` (answer, decline and hang up), `onCallmeHosting`, `wireCallme`, and the contacts: `renderCallmeContacts` (rebuilt only on change,
  focus kept by id), `onCallmeContactList` (call, edit, delete), `onCallmeContactSave`. One live
  region, `callme-state`.

--------------------------------------------------------------------------------
## DevTools/ (148 files; the ones worth knowing)

**Browser checks.** `ui_probe.py` (the harness: proxies GETs to the live server, stubs POSTs; it
gives the page `check()`, `sleep()`, `probeLog()`, `probeClear()` and `probeDone()` and nothing
else) plus `ui_checks_<page>.js`: about, appsettings, backups, baseline, boundary, callme, lifecycle,
chantabs, device, levelcheck, network, pads, padsbus, readonly, recording, routing, shell, solo,
**staged**, stereopan, wireless.
`ui_checks_stereopan.js` covers pan: one direction-spoken Pan slider on a mic, two leg sliders and
a Link switch on a stereo channel (`devices.Channel.stereo`), and the linked slider writing both
legs. The panel side is `fillStereoPan` in `app.js`.
**`probeLog()` CARRIES THE POST BODY, not just the path** (2nd September 2026), so a check can ask
WHAT the page tried to write rather than only that it tried: "the wireless mute posts to its own
endpoint" passes just as happily when the page sends the wrong channel name. `ui_checks_pads.js` is the fullest
template, and **it needs `--timeout 420`**: at the default 90 it reports "the page never reported
back within 90s", which reads like a broken page and is only 97 checks taking their time (measured
2nd September 2026, against an unmodified `app.js`, after that message sent a session hunting a
regression that was not there). **`ui_checks_staged.js` covers TWO pages and must be run twice**, `--route routing` and
`--route channels`: it proves that every audio-changing dropdown writes nothing when arrowed and
writes exactly once from its Apply.
**Every checks file must end with `await probeDone();` inside `run()` AND the `run().catch(...)`
launcher**, or the run dies as "the page never reported back within 90s" with no clue why.
**A CHECKS FILE MUST NOT NAME A CHANNEL THAT ONLY ONE DESK HAS.** `solo` and `levelcheck` both
hard-coded `Mic3`, which a Duo does not have (its source 2 is the Headset), so on that desk they
reported six and three failures reading exactly like the control had gone missing - the same trap
`test_announcer.py` pins the desk to avoid. Both now pick the highest-numbered mic out of
`channel-select`, and a wide check derives its "everywhere else" list the same way. Fixed 2nd
September 2026; grep a new checks file for `Mic3` / `Mic4` before trusting a pass on one desk.
`ui_checks_padsbus.js` covers the SMART pads bus being a channel with a chain but no level; it was
written for the Pro II and passes unchanged on a Duo since the bus was added there.
`ui_checks_wireless.js` covers the wireless mic mute: on the mics only, told apart from the channel
mute by name AND description, posting to its own endpoint with the channel it is on.
**`ui_checks_readonly.js` NEEDS A GENERATED FIXTURE AND CANNOT RUN WITHOUT ONE.** It is the check
that no capability-gated control uses the raw `disabled` attribute, and no desk in existence reaches
those branches (all six gates are ON for both), so run `python DevTools/make_readonly_stub.py` first
- it takes what the ATTACHED desk really answers and flips only the gates - then pass
`--stub DevTools/_probe_stub_readonly.json --route routing`. The fixture is gitignored like every
other `_probe_stub_*.json`: the repo carries the checks, not a snapshot of one machine's desk.
**EACH SUITE'S OWN HEADER GIVES ITS COMMAND LINE, AND SOME NEED MORE THAN `--route`.** `baseline`
needs **`--offer-baseline`**, or the probe answers `/api/baseline/status` with "nothing to offer",
the first-run dialog correctly never opens, and the suite reports four failures that are not real
(done, and reported as a bug, 29th August 2026).
**`baseline` IS ALSO SINGLE-USE AND CONSUMES REAL STATE.** One of its checks DECLINES the offer, and
declining is remembered per desk in `%LOCALAPPDATA%\AccessCaster\settings.json` under
`baseline_asked`. So it passes 29 of 29 the FIRST time and fails the same four ever after, and it
has silently used up that desk's genuine first-run offer. To re-run it, remove the desk's hardware
id from that list first (and put the user's own state back afterwards if you did not mean to spend
it). **`pads` needs about TWENTY MINUTES**
(`--timeout 1400`; 97 checks): so many of its checks trigger a fresh pad-grid read off the desk that
it really is that slow, and it was twice called hung at 300 s and 420 s. **`device` needs
`--timeout 600`** (122 checks; it reads a different part of the desk per tab and does not finish in
the default 90 s). Results are only posted at `probeDone`, so a slow run and a hung one are
indistinguishable from outside.
**THE LESSON THAT COST AN HOUR: run a suite the way its own header says, and give it time, before
calling it broken.** Both of the "pre-existing failures" chased on 29th August 2026 were one set of
arguments swept across every suite.

**Global keyboard shortcuts.** `test_hotkeys.py` (offline, no desk, NO KEY PRESSES: the
validation rules, real registration with Windows, dispatch by posting WM_HOTKEY at the pump, our own
clash detection, re-binding, the master switch, and what gets persisted). `test_hotkey_actions_live.py` (the ACTIONS
against the desk; stop the service first, and `--record` to include the recording pair).
`press_keys.py` presses REAL keys system-wide, for `gui_drive.py`, and uses `keybd_event` because
SendInput injects nothing from an RDP session here. A run killed mid-press leaves the modifier held
in every application, so nothing that runs in a sweep may call it.
`probe_free_chords.py` asks Windows which modifier chords will actually give up a key, by
registering and immediately unregistering. **It is the only authority on that**: `RegisterHotKey`
refuses without saying who holds the key, and the obvious guess is often wrong (Ctrl+Alt+Win+digit
looked like Windows and was Shaun's own webcam app). **Stop our server first**, or the keys we hold
read as taken.

**Playing pads from a shortcut.** `test_pad_shortcuts_live.py` drives the shortcut layer against the
attached desk and checks the SPOKEN RESULT of each branch, because that sentence is all the feedback
a shortcut gives. It runs its own announcer, since the tracking under test lives in the push handler,
and it silences the desk at BOTH ends - a playing sound pad survives an ordinary teardown, and one
left over makes the next run's first press a stop rather than a start.
`probe_pad_timeline.py` is the pad-TIMING tool and the only trustworthy one: a reader thread
timestamps every push while the main thread presses at known times, so nothing is inferred from a
rate measured in a gap. Three earlier probes were deleted for getting that wrong.

**Offline Python suites, no device - 33 of them:** `test_announcer`, `test_commands` (a toggle shortcut writes the opposite of the real state), `test_announcer_suspend`,
`test_announcer_remember` (start-up is on unless the remember option holds it off; a take's switch-back is never saved), `test_announcer_reconnect` (the announcer keeps listening across a re-enumeration: it must resolve
the interface again rather than reopen a remembered path, because the desk returns under a
different product id),
`test_autostart`, `test_baseline`, `test_callme`, `test_chain`, `test_driver_install`, `test_duo_parity`, `test_fader_chains`, `test_half_connected`,
`test_hotkeys`, `test_iface_cache` (when the remembered interface list scans and when it is reused), `test_input_read`, `test_latched_pads`, `test_level_check`, `test_meters_ceiling`,
`test_app_settings` (the export allow-list: **that the remote-access token cannot leave in the
file**, and that an import merges rather than replacing this machine's own token),
`test_pad_actions`, `test_pad_records`, `test_preset_now`, `test_profiles`, `test_recorder`, `test_schemes`,
`test_restore_warnings`, `test_shows`, `test_shows_sim`, `test_specs`, `test_storage_state`,
`test_soundpad` (transfer mode is always left, and a pad restore reports a create or setting that
did not take),
`test_virtual_devices` (RODE's driver status block parsed as its tray reads it, and the spoken
outcomes). `watch_vad_endpoints.ps1` is the live companion: it prints a line whenever the set of
RODE audio endpoints changes, which is how a mode change or a replug is watched without touching
the desk.
**An offline suite that asks which desk it is on must PIN one** with
`devices.force_device(devices.PRO2)` at the top, or it answers from the USB port and fails on the
wrong machine. `test_announcer`, `test_shows_sim` and `test_level_check` do.
**`test_input_read.py` covers the ADDRESSED input read** (`channels.input_value`) on both desks,
including the exact Duo mis-attribution, a falsy override beating the table, and a guard that
`input_slot` has not come back. It builds synthetic dumps rather than shipping a capture (a real
dump holds the wifi PSK).
**`fake_model.py` is THE encoder for synthetic dumps**, in the real tree grammar; every offline
suite that needs a dump builds it there (`encode` for (block, property, value) rows, `node` for an
exact tree). A fixture the parser cannot read now raises, so a hand-rolled byte stream will not do.
`test_input_read_live.py` is its hardware half: writes each mic's gain in turn, asserts the others
did not move, restores. Safe with a mic connected - it writes gain only, never phantom.
**`test_restore_mic_order_live.py` now asserts the CHAIN as well as the gain** survives a restore
that changes the voicing.
**`test_duo_caps_live.py` exercises EVERY capability the attached desk has ON** (the Duo plan's A5):
mic gain / polarity / line level / mic type, a processing module and a float param, channel mute, a
fader source swap, a stereo link and unlink, a matrix cross-point and its level, a soundpad create
and clear, monitor mute, and the whole Device settings page. Each writes, diffs the WHOLE tree,
restores and diffs again, so
an unexpected change is visible rather than inferred. `--only NAME` runs one. **It judges by the
diff, NOT by occurrence counts**: on a Duo a first write CREATES the property, so a count is not a
pass condition. It never sleeps a fixed time (`settled` polls until two whole-tree reads agree) and
it excludes `encoderColour`, which is derived live state that decays on a timer - both were sources
of failures that looked like flakiness. It also re-checks that all 20 unit-setting addresses still
resolve on the attached desk, and never touches the `disableAll*` trio.
**Two hooks carry the writes the desk does not undo for you.** `restore_extra(before)` runs after the
restore write, for a value the desk DRAGS others along with (the mic voicing replaces the strip's
whole chain; `outputMonFixed` unmutes the monitor). `ignore={names}` exempts a write-only trigger
from the restore assertion but still PRINTS it, so it can never hide. Passes end to end on a Pro II,
31st August 2026.
**`test_usb_drop_live.py` isolates ONE LAYER AT A TIME for the USB drop**, the fault where a desk goes off
the bus and only a physical replug returns it. `--layers` composes any of `handles1`, `handles2`,
`poll`, `export`, `exportonly` and `app` (that last one exclusive, and it is the CONTROL: if every
layer is clean and the app still drops, the layer list is wrong). No layers means the command alone.
Presence comes from PowerShell `Get-PnpDevice`, NOT `hid.enumerate`, which opens each device on
Windows and would put traffic inside the window that has to stay quiet; it is also a separate
process, which is what makes it valid for the `app` layer. Every trial proves the reset RAN by
reading the new show uuid, so "no drop" can never be "no reset", and it stops dead on the first drop.
**EACH TRIAL IS AN UNBACKED FACTORY RESET, so expendable desks only.** Result 3rd September 2026: 45
resets, every layer clean.

**`probe_show_push.py` answers what the desk PUSHES for a show command**, which is what the listening
design was built on. `--export` or `--reset`, and it prints the frame timings, the name histogram, and
a whole-tree diff either side. It never listens across its own dumps (536 frames of model would read
as a flood of pushes), so the order is snapshot, listen, send, capture, stop, snapshot.

**`test_fader_chains.py` covers the off-fader chain store and all three `_follow_chains` cases**
(moved / left / returned), including a return that does not land being parked again. It mocks
`channels.write_chain` and points `_paths.data_path` at a scratch file, so it never touches the
user's real parked chains.
**`test_pad_actions.py` covers `server._pad_write` and all seventeen pad actions over it** (the
range guard, `invalid` landing BEFORE the device lock, a crash becoming a sentence,
`plain_value_error`). Added 30th August 2026 with the helper, because the audit found that family
had NO offline coverage at all - the most numerous action family in the app, and not one test
called any of it.
**`test_specs.py` is where "two tables that must agree" checks live.** Today that is the unit
settings: `rcp_transfer.UNIT_SETTING_SEL` is the single source of which ones exist, and every entry
is either on the Device page or in `server.UNIT_SETTINGS_NOT_ON_PAGE` with a reason. Put any future
cross-table invariant here rather than in a new file. Show parsing is testable with no device at all through `fake_model`.
**`test_shows_sim.py` drives the REAL `server.action_show_*` against a FakeDesk with faults
injected** (an export that does nothing, an import the unit rejects, a reset that does not take).
It is the only repeatable cover the import, the factory reset and the clean restore have, since
each is too costly to run over and over on a working desk. It proves the DECISIONS, never the
bytes: the load-bearing case is that a failed backup must abort the reset with the desk untouched.

**A SUITE THAT TOUCHES THE DESK IS NAMED `_live`, WITHOUT EXCEPTION.** The name is what everything
else filters on - a sweep of "the offline suites", and `DevTools/_bootstrap`, which blocks device
writes for `test_*` and hands the licence back for `test_*_live`. A live suite under a plain name
gets run by both as though it were safe, and it writes to whatever desk is plugged in.
**IT IS NOT THEORETICAL AND A COMMENT DOES NOT PREVENT IT** (21st September 2026): `test_unit_settings`
and `test_network_scan` carried exactly this warning, in this file, and were still swept up as
offline nine times in one afternoon. Each pass muted Mic 2, unmuted it and saved a profile on a
desk being worked at, and every pass reported ALL CHECKS PASSED. Both are now `_live`.
**THE BOOTSTRAP GUARD DOES NOT COVER THIS CASE, which is why the NAME has to.** It refuses writes
inside the test's own process; a suite that drives the running server over HTTP does its writing in
the SERVER's process, where nothing is a test. The diagnostic log is the way to check after the
fact: run the sweep, and if `%LOCALAPPDATA%\AccessCaster\logs\diagnostic.log` gains a line, an
"offline" suite reached the desk.

**Live device tests (write, read back, restore):** `test_unit_settings_live`,
`test_pad_trigger_live`, `test_orphan_cleanup_live` (makes an attached orphan, then a partial object, on padIdx 16 and
17, and checks each is removed and the whole tree returns to where it started), `test_recorder_live`,
`test_player_live` (loads a file into the unit's own player and watches for movement),
`test_eq_q_live`, `test_meters_live`, `test_network_scan_live` (needs `--wifi` / `--bt` and exits 2
without them),
`test_flush_verify_live` (the licence for the fast flush: write a pad, leave transfer mode,
re-enter, md5 it back off the desk),
`test_schemes_live` (captures the mixes, drives Chat, puts them back, proves the matrix and the
property counts are unchanged; needs the server stopped),
`test_cross_point_wide_live` (a cross-point above source 13, where the selector needs TWO address
bytes: the tree must put the MIX instance at the selector `cross_point_selector` builds, the write
must read back, and the node/property/MIX counts must be unchanged. `--source` / `--output`, and it
exits 2 rather than discard a hand-set level on an unlinked cell. Touches no output mode, so
nothing about it is audible),
`test_monitor_output_live` (EAR TEST on the rear line outs, `--block on|off`; one block per run and
it counts down first, and its docstring carries why both of those are non-negotiable),
`test_mic_type_gain_live` (steps a mic through every voicing and records the gain each one applies:
the table behind the restore's write order. Restores gain, type AND instrument gain),
`test_restore_mic_order_live` (drives `profiles._apply_v2` itself on one channel from the state a
factory reset leaves, so the ordering fix is proven through the real code without a reset),
`test_clean_restore_live` (**FACTORY-RESETS THE DESK**, refuses without `--yes`: the whole clean
restore through the HTTP API with a 6467-property diff, then imports the backup it took to put the
desk back and diffs that too),
`test_hotkey_actions_live` (the shortcut ACTIONS against the desk; stop the service first, and
`--record` to include the recording pair),
`test_read_under_push_live` (**the model read against a desk that is deliberately pushing**: fires a
pad over MIDI, then asserts the read is prompt, the same length and the same tree structure as a
quiet one, that the push filter fires on real pushes and never on dump content, and that frames go on
arriving after the burst. Also runs the PRE-FIX loop on purpose, on a cut-down ceiling, so both
faults stay demonstrated. Read-only apart from `systemMidiControl`, which it restores; needs nobody
at the desk).

**Six live tests were DELETED and their ANSWERS are the reason they are gone.** Do not rewrite
them expecting a new answer:
- `test_storage_mount_live` - walked every `transferModeType` x `storageVolumeTransfer` pair and
  reported what Windows mounts. The answer is "pad storage, always"; the route, if there is one,
  is the gadget LUN rather than another transfer mode.
- `test_device_meters_live` - sampled all 22 METER nodes quiet and then while speaking through the
  desk, and refused to judge them until a Windows-side witness endpoint passed first. Answer as of
  29th August 2026: **meter values are not in the model dump**, which is why metering reads Windows
  audio endpoints instead.
- `test_recording_quality` and `test_recording_quality_takes` - what the two bypasses and the
  quality field do, the second by MAKING REAL RECORDINGS (one take per `recordingCompressionQuality`
  value, same phrase and duration each time) and reading the format out of `recordingContent`'s
  path. Answer: WAV and MP3, two values, and it is `recorder.RECORDING_FORMAT_LABEL` now.
- `test_tone_live` - wrote `toneGeneration` on TEST 0x290 and measured the main-mix endpoint.
- `test_voxlab_derive` - the VoxLab knob derivation, now in `channels.VOXLAB_KNOB_PROPS`.

**Address and selector work:** **`tree_tool.py` FIRST - it parses the dump as the tree it actually
is and gives any node its real address (`--find`, `--prop`, `--anchors`); `probe_path_write.py` is
the path-addressed probe that goes with it (LANDED / STRAY / NOTHING).** Then
`selector_map.py` (header to selector, everything derivable), `selector_from_push.py` (a pushed
record names its own selector),
`input_sources.py` (the 30-entry INPUTSOURCE table with the reserved slots marked, `--save` /
`--diff` two dumps: what a mode switch adds is how Expanded's four source codes were found, and
it is how to confirm them on a Duo),
`probe_derived_write.py`, `cap_probe.py` (drive one control with USBPcap running),
`model_diff.py` (block-aware snapshot / diff of all 6436 property occurrences, `snap` / `diff` /
`watch`; the tool for "the desk just did something, what changed?"), `transfer_volumes.py` (what
Transfer Mode actually mounts, per `transferModeType` value),
**`firmware_dig.py` - THE DESK'S OWN FIRMWARE, and it outranks every other source.** A dump shows
what the desk PUBLISHES, the RODE app shows what the app KNOWS, the firmware shows what the desk
DOES. RODE publish it: `update.rode.com/manifest.json` names `rcp2/<version>/app.zip` (the two Linux
binaries, md5 published beside it and verified) and `os.tar.gz` (the whole OS, 169 MB, not fetched).
The image is RODE's, so it lives OUTSIDE the repo at `C:\Users\shaun\rode-firmware` and is never
committed. Unpack each release as `app-<version>`: the newest is used by default, and pointing
`ACCESSCASTER_FIRMWARE` at an older one is how two releases get compared. `rc_audio_mixer` is the
engine that speaks report-3 to us; `rc_gui_juce` is the TOUCHSCREEN, so every screen we cannot reach
is readable there. It is what turned the clock from a dead end into a route
(`ntpdate pool.ntp.org`), and what settled where the Icecast page lives.
**`pdf_text.py` - text out of a vendor PDF with no third-party library.** The reference material for
this hardware arrives as PDFs on a machine with no PDF package installed. Scanned pages return
NOTHING rather than the decompressed JPEG a naive scan mistakes for text.
**`app_name_table.py` - THE SAFE NAME ENUMERATOR, and read this before anyone proposes guessing a
property name at the desk.** The protocol's names sit in one contiguous block inside the RODE app's
binary; it anchors on names the desk publishes, takes the region they span and reads the lot. 666
names against the desk's 472, so it carries block names, enum values AND properties the desk does
not publish (`systemLanguage`, `channelPanMode`, `hardwareLayout`, `debugMode`, `clock24Hour`,
`padProgressRequestSignal`, the `phoneBook*` family, and blocks like DUCKER, RADIO, THEME).
Writing a guessed name at the desk appends a stray only a factory reset clears; reading the table
costs nothing.
`analyse_clean_restore.py` (offline, no device: diffs two dumps by TREE PATH and sorts every
difference into by-design / pads / "came back as the FACTORY value" / real. That last split is what
killed the timing theory. It knows the by-design rules, so a clean run means clean).

**`probe_dead_props.py` and `probe_dead_props_write.py`** check the guide's PROVED INACTIVE list
against whichever desk is attached. The first is read-only: block, address, occurrence count and
resting value for each, which is how the Duo was shown to carry all of them at the same addresses
as the Pro 2. The second writes each one that takes a single operand, classifies the result
SWALLOWED / STORED / SIDE EFFECT / STRAY, and restores it. **It skips `toneGeneration` and
`emergencyMuteActive` unless `--include-audible`**, because their proof needs a listener, and it
never writes a `pair` property, because guessing an operand layout is how a stray gets appended.
This is what found `btDoScan` alive.

**`probe_unclaimed_props.py`** writes, reads back and restores the six readable properties nothing
in the app uses, addressing each at its TREE path rather than a guessed selector, and watches the
push stream for any reaction. Counts the model's nodes, properties and names either side.

**`probe_callme_live.py`** drives CallMe on a live desk: `state` reads every `sip*` property,
`host on|off` switches hosting and prints the guest link, `watch` / `speak` log (and speak) every
call push, and `answer [slot]` answers the next incoming call with the touchscreen's composite.

**`ui_checks_storage.js`** pins the Recording page's card and drive section: the internal pad storage never offered, Eject named for what it will do, erase asking first.

**`probe_update_check.py`** asks the desk to check for a firmware update and logs every update push; it never installs. **`probe_storage_live.py`** ejects or erases the card or drive and logs the desk's answer, refusing the internal pad storage (anything `recorder.read_volumes` does not mark `can_record`).

**`probe_delete_backup_live.py`** drives `action_delete_backup` down both its branches against a
real desk: a `show:<snapshot>` with no profile beside it, and a named backup whose two halves must
both go. Checks the drive's free space rises by the show's 22 MB, which is the only direct evidence
the file left, and that the as-found baseline is still refused without `force` afterwards.
**It is a `probe_` and must stay one.** `_bootstrap` gives anything named `test_*.py` a throwaway
data dir, which is right for a test and wrong here: the both-halves branch pairs a REAL profile
with a REAL show, and under the sandbox it saw an empty profile store and reported that the backup
it had been asked to delete did not exist. The show half passed regardless, because a show lives on
the desk rather than in that folder, which is what made the failure look selective rather than
environmental.

**A BACKGROUND JOB'S STATUS KEY IS `running`, NOT `busy`** (`server.show_job_status`). Polling a key
the status does not carry reads as None, so a wait returns at once and the read-back happens while
the job is still going: that is what made `action_delete_backup` look as though it left the
computer's copy behind when it had simply not finished.

**`bind_timeline.py` and `vad_trace.py`** are the pair for RODE's virtual-device service, which
binds to the desk on some arrivals and not others. The first samples, several times a second, which
of the desk's device interfaces exist and which are ACTIVE, alongside the service's own connected
flag, and prints every change with a millisecond offset; `--snapshot` reads the current state and
exits. Read-only, no elevation, and it makes no arrival itself: use the app's USB mode control or
the cable. The second holds the global `DBWIN` debug channel, which needs ELEVATION and needs
DebugView closed, and exists to record a negative: the service carries a full trace string table
and emits none of it. See `PROJECT_GUIDE.md` part 2 under Expanded for what both measured.
**`bind_history.py`** reads the per-attempt history the app itself records and compares bound
attempts with missed ones field by field; `--last N` limits the table.

**`probe_bind_hold.py`** is the causal test of the bind: it holds a stream on the desk's Main
output from the moment it appears and reports whether the service bound before, during or after
the hold. Four trials out of four bound 0.22 s after release and never during.

**`probe_bind_default_output.py`** measures the service's bind RATE: it drives the USB output mode
off and back through the app's API and counts binds per ARRIVAL, not per flip, because the app's own
follow-up makes extra arrivals whenever it misses and each is another trial. `--control` runs the
same cycles changing nothing, and **any run without it is worthless**, because the rate swings with
whatever is playing to the desk's Main output. Needs the server running; restores the default
output it found.

**`check_exclusive_bind.py`** settles the two TCP facts the single-instance lock rests on, on port
8791 so it never disturbs a running app: that `allow_reuse_address = 1` really does let two servers
share a port on Windows, and that `= 0` still rebinds straight after a clean stop with traffic
behind it (the TIME_WAIT regression the change could have caused). Offline, no device. See
`PROJECT_GUIDE.md` part 2 under the server's lifecycle.

**`check_notes.py` enforces the private notes' own rules, the ones a script can settle**: an entry that is
really a cleanup, a status column creeping back into `PARITY.md`, `Now / Next` past 250 lines, a
session-summary heading, em dashes, any file named that no longer exists (in the notes, the docs,
and the `.py` and `.js` sources), an entry written twice in one file, and a code comment that
reads as history (a date, "used to be", "it turned out"; `version.py` and `help.html` are exempt
as user-facing). Run
`python DevTools/check_notes.py --check` before finishing a session. It does not check taste; it
catches the specific ways these files have gone wrong before.

**Generated documents, all four regenerable with a `--check` drift guard that exits 1:**
**`property_list.py` -> `docs/PROPERTY_LIST_<DESK>.md`** (every property the DESK
publishes, names and types only, never values; reads the connected desk, read-only, and runs fine
alongside the app's server. It is the RAW MATERIAL, and it feeds `property_coverage.py` and
`test_specs.py`. `--check` compares the NAME SET, not the counts, because counts move with ordinary
use while a new NAME is either a firmware update or a STRAY),
`property_coverage.py` and `profile_coverage.py` write their per-desk reports into the private
notes, and **`codemap_lines.py` -> the NUMBERS IN THIS FILE** (the
`name` (NNN) references, the table's line counts, the DevTools file count). Run
`python DevTools/codemap_lines.py` after a session that moved code; never edit one of those numbers
by hand. It touches no prose.

**`audit_code.py`** is the stdlib code audit: syntax, unused imports, dead top-level functions
across `Project/`, with the reference sweep widened to `DevTools/` so a function only a probe calls
is not called dead. It runs from anywhere.

`run_dev_server.py`, `wifi_join.py` (password on stdin; strips the BOM PowerShell adds on a pipe),
`screen_shot.ps1` (**photographs the desk's touchscreen** with the Insta360 webcam; records a
five-second clip and keeps the LAST frame, because a single-frame grab is always out of focus),
`check_drive_recordings.py` (WAV cue
points, and it needs the drive in THIS PC, not the desk), `midi_monitor.py`,
`midi_out.py` (**the only way to PLAY a pad from software**: the desk's class-compliant "MIDI
function" port through winmm/ctypes, no package. `midi_enabled()` turns `systemMidiControl` on and
puts it back, `MidiOut().play_pad(n)` fires one. CC number is the FUNCTION, MIDI channel is WHICH
pad. Run it directly to list ports or fire a pad), `state_observatory.py`
(`listen` takes no `--raw`), `gui_drive.py` +
`ui_map.json` (drive the RODE app), `duo_probe.py` (read-only bring-up probe for any new desk),
`announcer_echo_check.py`, `audit_code.py`, `watch_backchannel.py` (samples Mic 1 and USB 2's mute
and cue flags through the running service while a Back Channel pad is held, which is the only way
to tell a pad that did nothing from one that did its job and tidied up).

**Two cap-opening probes, and both are worth reading before writing another one.**
`duo_stereo_probe.py` asks whether a stereo PAIR CODE is live on a Duo, and `--control CODE` is the
half that matters: it runs the same write against a source the desk cannot have (3 = Mic 4), because
the engine's model is device-agnostic and will answer either one identically. Two verdicts died
before that control was added. `duo_network_probe.py` does structure-then-engine on the NETWORK
block: it identifies root child 0x10, checks every property the app writes is on it, then cycles the
radio and watches the DESK drop and regain its own IP address. `--skip-radio` leaves the radio alone.

**Device writers, the small accidental-damage surface:** `bt_probe`, `gui_drive`, `inject_button`,
plus the live tests above.

--------------------------------------------------------------------------------
## Build and assets

- `ProjectBuilds/build_installer.ps1` -> `ProjectBuilds/Output/AccessCasterSetup-v<version>.exe`. Four steps:
  icon, RELEASE NOTES, PyInstaller freeze, Inno Setup compile. Uses `AccessCaster.spec`
  (PyInstaller), `installer.iss` (Inno Setup 6), `gen_icon.py` (pillow).
  Entry point is `tray.py`; user data lives in `%LOCALAPPDATA%\AccessCaster`.
- `ProjectBuilds/make_release_notes.py` -> `Project/ReleaseNotes.html`, generated from
  `version.py` HISTORY (a LIST, not prose) and gitignored because it is derived. The spec bundles it
  and `installer.iss` offers it as a ticked post-install action. HTML for heading navigation.
- `ProjectAssets/`: `mic_presets.json` (factory presets, module flags included),
  `fx_recipes.json` (voice-FX recipes, shared by both desks), `APP_WALKTHROUGH.txt`,
  `RODE_APP_SECTIONS.txt`.

--------------------------------------------------------------------------------
## docs/ (the reference documents, and this file is one of them)

`PROJECT_GUIDE.md` is the one to read first, and the one written for somebody who did not write
this code: what the protocol turned out to be, how it was worked out, how the app is layered, and
the traps. Part 1 is the narrative, part 2 the invariants. Keep it free of session detail.

`RODECASTER_PROTOCOL.txt`, beside this file, is the definitive byte-level spec:
looked up rather than read.

`PARITY.md` is the parity TARGET, every feature the desk and the app have, and it deliberately
carries no status, because a hand-kept status column rots. `SOUNDPADS.md` is the single source of
truth for pad and voice-FX work and is the first thing to read before touching either.
`PROPERTY_LIST_<DESK>.md` is GENERATED, one per desk, by `DevTools/property_list.py`: every property
that desk publishes, names and types only.

**THE DEVELOPMENT NOTES ARE NOT IN THIS REPO AND ARE NOT PUBLIC.** What shipped and why, what is
open right now, the per-device work logs and the generated coverage reports live in a private
sibling repo, reached by `_bootstrap.notes_path`. Nothing in them is needed to understand the
hardware or the code, which is what the files here are for. `_bootstrap.docs_path` is the public
counterpart, and choosing the wrong one of the two either publishes a note or hides a finding.
