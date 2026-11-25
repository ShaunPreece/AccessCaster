"""A preview of the window built in AppKit directly, beside the wx one, to judge by ear whether a
Mac-native window is worth a second set of pages. macOS only.

    .venv/bin/python DevTools/probe_mac_native.py

It runs the simulator (`simulate_desk.py`, same arguments) and, once the desk is read, opens a
second window, "AccessCaster Mac preview": a sidebar of every page, the Mac's source list, and a
channel's settings beside it. Only the channel pages are built; the rest say so. Both windows drive
the same simulated desk through the same actions and follow the same desk state, so a change made
in one shows in the other. Command+` moves between them.

The channel page is laid out as a Mac app lays out a form: a label column and a control column,
the controls Apple's own with nothing between them and VoiceOver. The slider moves with the
NSSlider's own keys, one decibel per tick. The mic type is a pop-up button applied as it is
chosen: on a Mac a pop-up changes only when an item is picked from its menu, so the wx window's
Apply button guards against something that cannot happen here.
"""
from __future__ import annotations

import sys
import time

if sys.platform != "darwin":
    sys.exit("The Mac preview runs on macOS only.")

import objc  # noqa: E402
from AppKit import (NSApp, NSBackingStoreBuffered, NSButton, NSFont, NSGridCell,  # noqa: E402
                    NSGridCellPlacementTrailing, NSGridView, NSLayoutAttributeLeading, NSMakeRect, NSOutlineView, NSPopUpButton, NSScrollView, NSSlider,
                    NSSplitViewController, NSSplitViewItem, NSStackView, NSTableCellView,
                    NSTableColumn, NSTableViewStyleSourceList, NSTextField, NSView,
                    NSUserInterfaceLayoutOrientationVertical, NSViewController, NSWindow,
                    NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable,
                    NSWindowStyleMaskResizable, NSWindowStyleMaskTitled)
from Foundation import NSObject  # noqa: E402

import simulate_desk  # noqa: E402  (sets the simulator's data dir before any Project import)

import channel_actions  # noqa: E402
import channels  # noqa: E402
import mute_solo  # noqa: E402
import wx  # noqa: E402
from desk_labels import range_words  # noqa: E402
from native import frame as frame_module  # noqa: E402
from native.calls import LatestWins, call  # noqa: E402

TITLE = "AccessCaster Mac preview"
HANDS_OFF_S = 2.0          # as controls.Slider: the desk's echo of a write must not move the slider


class SidebarItem(NSObject):
    """One row of the sidebar: a section, with its pages as children, or a page."""

    def initWithTitle_key_(self, title, key):
        self = objc.super(SidebarItem, self).init()
        if self is None:
            return None
        self.title = title
        self.key = key
        self.children = []
        return self


class Sidebar(NSObject):
    """The outline view's data source and delegate."""

    def initWithPreview_(self, preview):
        self = objc.super(Sidebar, self).init()
        if self is None:
            return None
        self.preview = preview
        self.roots = []
        return self

    def outlineView_numberOfChildrenOfItem_(self, _view, item):
        return len(self.roots if item is None else item.children)

    def outlineView_child_ofItem_(self, _view, index, item):
        return (self.roots if item is None else item.children)[index]

    def outlineView_isItemExpandable_(self, _view, item):
        return bool(item.children)

    def outlineView_viewForTableColumn_item_(self, view, _column, item):
        cell = NSTableCellView.alloc().initWithFrame_(NSMakeRect(0, 0, 200, 22))
        text = NSTextField.labelWithString_(item.title)
        text.setTranslatesAutoresizingMaskIntoConstraints_(False)
        cell.addSubview_(text)
        cell.setTextField_(text)
        text.leadingAnchor().constraintEqualToAnchor_constant_(cell.leadingAnchor(), 2).setActive_(True)
        text.centerYAnchor().constraintEqualToAnchor_(cell.centerYAnchor()).setActive_(True)
        return cell

    def outlineViewSelectionDidChange_(self, notification):
        view = notification.object()
        item = view.itemAtRow_(view.selectedRow())
        self.preview.show_page(item.key if item is not None else None)


class Actions(NSObject):
    """The target of every control's action, handing each to the preview."""

    def initWithPreview_(self, preview):
        self = objc.super(Actions, self).init()
        if self is None:
            return None
        self.preview = preview
        return self

    def switched_(self, sender):
        self.preview.switched(sender)

    def slid_(self, sender):
        self.preview.slid(sender)

    def micTypeChosen_(self, sender):
        self.preview.mic_type_chosen(sender)

    def clearSolos_(self, _sender):
        self.preview.clear_solos()


class Preview:
    def __init__(self, frame):
        self.frame = frame
        self.desk = frame.desk
        self.key = None            # the channel shown, or None
        self.controls = {}         # field -> control, for the channel shown
        self.moved_at = 0.0
        self.mic_type_values = []
        self.actions = Actions.alloc().initWithPreview_(self)
        self.sidebar = Sidebar.alloc().initWithPreview_(self)
        self.writer = LatestWins(frame, self._send_level, self._level_done, max_wait_ms=300)
        self._build_window()
        self.desk.subscribe(self._desk_event)
        self._fill_sidebar()

    # ---- the window ------------------------------------------------------------------------

    def _build_window(self):
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 900, 600), style, NSBackingStoreBuffered, False)
        self.window.setTitle_(TITLE)
        self.window.setReleasedWhenClosed_(False)

        self.outline = NSOutlineView.alloc().initWithFrame_(NSMakeRect(0, 0, 240, 600))
        column = NSTableColumn.alloc().initWithIdentifier_("page")
        self.outline.addTableColumn_(column)
        self.outline.setOutlineTableColumn_(column)
        self.outline.setHeaderView_(None)
        self.outline.setStyle_(NSTableViewStyleSourceList)
        self.outline.setDataSource_(self.sidebar)
        self.outline.setDelegate_(self.sidebar)
        self.outline.setAccessibilityLabel_("Categories")
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 240, 600))
        scroll.setDocumentView_(self.outline)
        scroll.setHasVerticalScroller_(True)
        side = NSViewController.alloc().init()
        side.setView_(scroll)

        # The split view keeps the view its item had when added, so a page is swapped inside
        # this host rather than by giving the controller a new view.
        self.host = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 660, 600))
        self.detail = NSViewController.alloc().init()
        self.detail.setView_(self.host)
        self._place(self._message("Choose a page in the sidebar."))

        self.split = NSSplitViewController.alloc().init()
        sidebar_item = NSSplitViewItem.sidebarWithViewController_(side)
        sidebar_item.setMinimumThickness_(220)
        self.split.addSplitViewItem_(sidebar_item)
        self.split.addSplitViewItem_(NSSplitViewItem.splitViewItemWithViewController_(self.detail))
        self.window.setContentViewController_(self.split)
        self.window.setContentSize_((900, 600))
        self.window.center()
        self.window.makeKeyAndOrderFront_(None)
        NSApp().activateIgnoringOtherApps_(True)
        self.window.makeFirstResponder_(self.outline)

    def _fill_sidebar(self):
        roots = []
        for section in self.frame.sections:
            item = SidebarItem.alloc().initWithTitle_key_(section.title, None)
            for page in section.pages:
                key = page.key if section.route == "channels" and page.key != "status" else None
                item.children.append(SidebarItem.alloc().initWithTitle_key_(page.title, key))
            roots.append(item)
        self.sidebar.roots = roots
        self.outline.reloadData()
        for item in roots:
            self.outline.expandItem_(item)

    def _place(self, view):
        for old in list(self.host.subviews()):
            old.removeFromSuperview()
        view.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self.host.addSubview_(view)
        for edge in ("leadingAnchor", "trailingAnchor", "topAnchor"):
            getattr(view, edge)().constraintEqualToAnchor_(getattr(self.host, edge)()).setActive_(True)
        view.bottomAnchor().constraintLessThanOrEqualToAnchor_(
            self.host.bottomAnchor()).setActive_(True)

    def _message(self, text):
        label = NSTextField.wrappingLabelWithString_(text)
        stack = NSStackView.stackViewWithViews_([label])
        stack.setOrientation_(NSUserInterfaceLayoutOrientationVertical)
        stack.setEdgeInsets_((20, 20, 20, 20))
        return stack

    # ---- a channel's page ------------------------------------------------------------------

    def _rec(self):
        for rec in (self.desk.channels or {}).get("channels") or []:
            if rec["name"] == self.key:
                return rec
        return None

    def show_page(self, key):
        self.key = key
        self.controls = {}
        rec = self._rec()
        if rec is None:
            self._place(self._message(
                "This page is not in the preview. It is in the main AccessCaster window."))
            return
        self._place(self._channel_view(rec))
        self._show(rec)

    def _channel_view(self, rec):
        data = self.desk.channels
        label = rec["label"]
        is_mic = rec["kind"] == "mic"
        self.number = (channels.mic_channel_number(channels.ui_channel_map()[rec["name"]])
                       if is_mic else None)
        rows = []

        def checkbox(field, title):
            box = NSButton.checkboxWithTitle_target_action_(title, self.actions, "switched:")
            box.setIdentifier_(field)
            self.controls[field] = box
            rows.append([NSGridCell.emptyContentView(), box])

        heading = NSTextField.labelWithString_(label)
        heading.setFont_(NSFont.boldSystemFontOfSize_(17))
        checkbox("muted", f"Mute {label}")
        checkbox("solo", f"Listen (solo) {label}")
        clear = NSButton.buttonWithTitle_target_action_("Clear every solo", self.actions,
                                                        "clearSolos:")
        rows.append([NSGridCell.emptyContentView(), clear])

        if rec["kind"] != "bus":
            lo, hi = ((data["gain_min_db"], data["gain_max_db"]) if is_mic
                      else (data["level_min_db"], data["level_max_db"]))
            slider = NSSlider.sliderWithValue_minValue_maxValue_target_action_(
                lo, lo, hi, self.actions, "slid:")
            slider.setNumberOfTickMarks_(int(hi - lo) + 1)
            slider.setAllowsTickMarkValuesOnly_(True)
            slider.setContinuous_(True)
            slider.setAccessibilityLabel_("Level")
            slider.widthAnchor().constraintGreaterThanOrEqualToConstant_(280).setActive_(True)
            self.range = (lo, hi)
            self.controls["level_db"] = slider
            rows.append([NSTextField.labelWithString_("Level:"), slider])

        if is_mic:
            checkbox("phantom", "48V phantom power")
            checkbox("polarity", "Invert polarity (phase)")
            types = [(value, text) for value, text in data.get("mic_types") or []]
            if types:
                popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 220, 26),
                                                                      False)
                popup.setTarget_(self.actions)
                popup.setAction_("micTypeChosen:")
                popup.setAccessibilityLabel_("Mic type")
                self.mic_types = types
                self.controls["mic_type"] = popup
                rows.append([NSTextField.labelWithString_("Mic type:"), popup])

        grid = NSGridView.gridViewWithViews_(rows)
        grid.setRowSpacing_(10)
        grid.columnAtIndex_(0).setXPlacement_(NSGridCellPlacementTrailing)
        stack = NSStackView.stackViewWithViews_([heading, grid])
        stack.setOrientation_(NSUserInterfaceLayoutOrientationVertical)
        stack.setAlignment_(NSLayoutAttributeLeading)
        stack.setSpacing_(16)
        stack.setEdgeInsets_((20, 24, 20, 24))
        return stack

    def _show(self, rec):
        """Put the record's values on the controls, never sending anything."""
        for field in ("muted", "solo", "phantom", "polarity"):
            box = self.controls.get(field)
            if box is not None:
                box.setState_(1 if rec.get(field) is True else 0)
        slider = self.controls.get("level_db")
        if slider is not None and time.monotonic() - self.moved_at > HANDS_OFF_S:
            value = rec.get("level_db")
            if value is not None and int(slider.intValue()) != value:
                slider.setIntValue_(value)
            self._describe_level(slider)
        popup = self.controls.get("mic_type")
        if popup is not None:
            current = rec.get("mic_type")
            choices = list(self.mic_types)
            if current is not None and all(value != current for value, _ in choices):
                choices.append((current, f"Other model (value {current})"))
            self.mic_type_values = [value for value, _ in choices]
            titles = [text for _, text in choices]
            if list(popup.itemTitles()) != titles:
                popup.removeAllItems()
                popup.addItemsWithTitles_(titles)
            if current in self.mic_type_values:
                popup.selectItemAtIndex_(self.mic_type_values.index(current))

    def _describe_level(self, slider):
        db = int(slider.intValue())
        slider.setAccessibilityValueDescription_(range_words(f"{db} decibels", db, *self.range))

    # ---- what the user does ----------------------------------------------------------------

    def switched(self, box):
        field, on, name = box.identifier(), box.state() == 1, self.key
        sends = {"muted": lambda: mute_solo.action_set_channel_mute(name, on),
                 "solo": lambda: mute_solo.action_set_channel_solo(name, on),
                 "phantom": lambda: channel_actions.action_set_phantom(self.number, on),
                 "polarity": lambda: channel_actions.action_set_polarity(self.number, on)}
        rec = self._rec()

        def done(reply):
            if reply.get("ok") and rec is not None:
                rec[field] = on
            elif not reply.get("ok"):
                box.setState_(0 if on else 1)
            self._say(reply)
        call(self.frame, sends[field], done=done)

    def slid(self, slider):
        self.moved_at = time.monotonic()
        self._describe_level(slider)
        self.writer.change(int(slider.intValue()))

    def _send_level(self, db):
        return channel_actions.action_set_channel_level(self.key, db)

    def _level_done(self, db, reply):
        rec = self._rec()
        if reply.get("ok") and rec is not None:
            rec["level_db"] = db
        elif not reply.get("ok"):
            self._say(reply)

    def mic_type_chosen(self, popup):
        index = popup.indexOfSelectedItem()
        rec = self._rec()
        if index < 0 or rec is None:
            return
        value = self.mic_type_values[index]
        if value == rec.get("mic_type"):
            return

        def done(reply):
            self._say(reply)
            if reply.get("ok"):
                self.desk.refresh()
            else:
                self._show(rec)
        call(self.frame, channel_actions.action_set_mic_type, self.number, value, done=done)

    def clear_solos(self):
        call(self.frame, mute_solo.action_clear_solos, done=self._say)

    def _say(self, reply):
        text = reply.get("message") or ("Done." if reply.get("ok") else "That did not work.")
        self.frame.say(text)

    # ---- the desk --------------------------------------------------------------------------

    def _desk_event(self, event, detail):
        if event == "read":
            self._fill_sidebar()
            rec = self._rec()
            if rec is not None:
                self._show(rec)
        elif event == "changed" and self.key in detail:
            rec = self._rec()
            if rec is not None:
                self._show(rec)


_preview = []
_start = frame_module.MainFrame.start


def _start_with_preview(self):
    _start(self)
    if not _preview:
        wx.CallLater(1500, lambda: _preview.append(Preview(self)))


frame_module.MainFrame.start = _start_with_preview

if __name__ == "__main__":
    sys.exit(simulate_desk.main())
