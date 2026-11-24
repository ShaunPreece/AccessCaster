"""AccessCaster - the packaged app's entry point: the service, the window and the tray icon, in one
process.

Running this is the "app". It brings the service up (announcer, shortcuts and watchers) on a
background thread, opens the native window (`native/`), and puts an icon where the system keeps
them (`wording.TRAY`). Closing the window hides it: the service keeps running behind the icon,
and the window comes back as it was left. The user quits from the icon's menu.

The icon's menu is the system's own native menu, so a screen reader reads it as a standard menu
without anything of ours having to describe it. It offers:

  - Open AccessCaster    show the window
  - Announcer            checkable toggle for the spoken button-press announcer
  - Diagnostic log       open the app's own log of what it did (see Project/diagnostics.py)
  - Check for updates    or "Install update to version X" once one is waiting (app_updates.py)
  - Quit                 stop the service and exit

The icon is also how the automatic checks reach somebody who is not looking at the window: a new
version of the app, or of the desk's firmware, is announced as a SYSTEM NOTIFICATION from it
(`_notify`), which a screen reader reads without the focus moving and without the app's own voice
talking over it.

The platform supplies:
  - `Icon(owner)`: the icon, made on the UI thread. `add()` shows it; `remove()` takes it away;
    `destroy()` ends it; `notify(text, title)` raises a notification from any thread;
    `post_open()` asks, from any thread, for the window to be shown and raised;
    `show_from_other_thread()` shows the window from another thread and returns its handle once
    it is shown, or None when the UI thread did not answer. It calls back on the UI thread:
    `owner.open_window()`, `owner.show_window()` (returns the handle), `owner.menu_items()`
    (a list of (key, label, checked), None for a separator; a label may mark its access key with
    "&"), `owner.menu_chosen(key)`, `owner.notice_chosen()`, `owner.request_quit()`,
    `owner.session_ending()`, and reads `owner.quitting`.
  - `window_to_raise(handle)`: a modal dialog open on the window, or the window itself.
  - `find_running(wait_s)`: this session's running copy, or None, looking for up to `wait_s`.
  - `open_running(running)`: have that copy show and raise its window.
  - `show_refusal(text)`: a blocking native message, with no wx app running.
  - `open_log(path, folder)`: show the log file, or its folder when `path` is None.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import wx

import announcer
import app_updates
import app_window
import autostart
import desk_access
import diagnostics
import firmware_auto
import instance
import rode_app
import service
import speech
from native import dialogs
from native.dialogs import TextDialog
from native.frame import MainFrame
from selfupdate import phrases as update_phrases
from wording import OS_NAME

if sys.platform == "win32":
    from win.tray import Icon, find_running, open_log, open_running, show_refusal, window_to_raise
else:
    raise ImportError(f"No tray icon for {sys.platform}: the Windows one is win/tray.py")

# How long Quit waits for the open dialogs to end before ending the process anyway. Each ends within
# one 50 ms pass, so only a dialog whose loop will not end takes this long.
DIALOGS_END_WITHIN_S = 2.0
# How the process ends when it cannot wait for the window (`TrayApp._end_now`). Replaced in tests.
_exit = os._exit

_OTHER_ACCOUNT = ("AccessCaster is already running in another account on this computer.\n\n"
                  "Only one account can use it at a time. There is a single RODECaster on the USB, "
                  "and the spoken announcements and keyboard shortcuts belong to the account that "
                  "started the app.\n\n"
                  "Quit AccessCaster in the other account, or sign that account out, then start it "
                  "again here.")
_SESSION_ENDING = f"{OS_NAME} is closing the app"
# How long a second launch waits for this session's copy to show its icon, when the lock is held
# but no copy is found yet: a copy that has only just started.
_FIND_RUNNING_S = 3.0
# How long it then waits for the lock to come free: a copy that has just quit holds it until its
# process ends, a moment after its icon has gone.
_QUITTING_COPY_S = 10.0


class TrayApp:
    """The process: the service, the tray icon and the window, all owned here.

    The window is built the first time it is wanted and kept when it is closed, so it reopens
    instantly and where it was left; it is destroyed only by Quit."""

    def __init__(self, app: wx.App, loop: wx.GUIEventLoop, open_window: bool):
        self.app = app
        self.loop = loop
        self.frame = None
        self._ui_thread = threading.get_ident()
        self._starting = True             # the service is still coming up
        self._quitting = False
        self._ended = False               # `_end_now` has run
        self._window_gone = False         # quit has closed the window and its dialogs
        self._notice = "app"              # what the last notification was about: app or firmware
        self.icon = Icon(self)
        self.icon.add()
        app_updates.notify = self._notify
        firmware_auto.notify = lambda text: self._notify(text, firmware_auto.NOTIFICATION_TITLE,
                                                          "firmware")
        rode_app.on_seen = self._rode_app_seen
        app_updates.open_window = self.icon.post_open
        app_window.show = self._show_from_any_thread
        # The system ending the session closes the window through wx as well as through the icon.
        # wx's own answer to the query closes the window, which hides it, and so refuses to let the
        # session end; these replace it.
        app.Bind(wx.EVT_QUERY_END_SESSION, lambda event: None)
        app.Bind(wx.EVT_END_SESSION, lambda event: self._end_now(_SESSION_ENDING))
        if open_window:
            self.open_window()
        # Started at sign-in, the announcer's greeting is what says the app is running. With the
        # window opening in front, it says so already.
        greeting = "" if open_window else announcer.GREETING_STARTED
        threading.Thread(target=self._bring_up, args=(greeting,), name="service-start",
                         daemon=True).start()

    @property
    def quitting(self) -> bool:
        return self._quitting

    # -- the service ------------------------------------------------------------------------------
    def _bring_up(self, greeting: str) -> None:
        service.start(greeting=greeting)
        wx.CallAfter(self._brought_up)

    def _brought_up(self) -> None:
        self._starting = False
        if self._quitting:
            # Quit was chosen while the service came up: finish what `_quit` left, once its window
            # has closed (`_close_window` finishes it otherwise).
            if self._window_gone:
                self._finish_quit()
            return
        if self.frame:
            self.frame.start()

    # -- the window -------------------------------------------------------------------------------
    def show_window(self) -> int:
        """Show the window, building it the first time. Returns its handle, or 0 while quitting.
        Raises nothing: that needs the right to take the foreground, which the caller holds."""
        if self._quitting:
            return 0
        if not self.frame:
            self.frame = MainFrame(self.request_quit)
            self.frame.present()
            self.frame.tree.SetFocus()
            if not self._starting:
                self.frame.start()
        else:
            self.frame.present()
        return self.frame.GetHandle()

    def open_window(self) -> None:
        """Show and raise the window, on this thread, then open an update decision the tray asked
        for (`app_updates.request_decision`)."""
        handle = self.show_window()
        if not handle:
            return
        app_window.raise_window(window_to_raise(handle))
        if not self._starting and app_updates.take_request():
            self.frame.check_updates()

    def _show_from_any_thread(self) -> bool | None:
        """`app_window.show`. The Open AccessCaster shortcut calls this on the hotkey thread,
        inline, where the foreground right exists (hotkeys._FOREGROUND_COMMANDS). The window is
        shown on the UI thread while the shortcut's handler is still running, and the raise happens
        back on the calling thread. None while quitting: there is no window to show."""
        if self._quitting:
            return None
        if threading.get_ident() == self._ui_thread:
            handle = self.show_window()
        else:
            handle = self.icon.show_from_other_thread()
            if handle is None:
                return False
        return bool(handle) and app_window.raise_window(window_to_raise(handle))

    # -- the icon's menu and notifications --------------------------------------------------------
    def menu_items(self) -> list:
        waiting = app_updates.held_version()
        return [("open", "&Open AccessCaster", False),
                ("announce", "&Announcer", speech.enabled.is_set()),
                ("log", "Diagnostic &log", False),
                ("update", update_phrases.tray_install(waiting) if waiting is not None
                 else update_phrases.TRAY_CHECK, False),
                None,
                ("quit", "&Quit", False)]

    def menu_chosen(self, key: str) -> None:
        if key == "open":
            self.open_window()
        elif key == "announce":
            self._toggle_announcer()
        elif key == "log":
            self._open_log()
        elif key == "update":
            # A version already waiting goes straight to the decision in the window. Otherwise the
            # check runs on its own thread and answers in a notification, or opens the decision.
            if app_updates.held_version() is not None:
                app_updates.request_decision()
            else:
                app_updates.check_from_tray()
        elif key == "quit":
            self.request_quit()

    def notice_chosen(self) -> None:
        """Choosing either notice is asking for its decision: the firmware's install question, or
        the app's update. With no app update waiting it shows the window."""
        if self._notice == "firmware":
            self.open_window()
            if self.frame is not None and not self._starting:
                self.frame.install_firmware()
        elif app_updates.held_version() is not None:
            app_updates.request_decision()
        else:
            self.open_window()

    def _notify(self, text: str, title: str = update_phrases.NOTIFICATION_TITLE,
                notice: str = "app") -> None:
        """Raise a notification from the icon; `notice` is what choosing it opens. Called from any
        thread. The icon shows one notification at a time, so the last one raised is the one a
        click answers."""
        self._notice = notice
        self.icon.notify(text, title)

    # -- actions ---------------------------------------------------------------------------------
    def _open_log(self) -> None:
        """Show the diagnostic log, flushed first, so what opens is current. Nothing has been
        written before the service starts, and then the folder is shown instead."""
        diagnostics.flush(force=True)
        path = diagnostics.log_path()
        open_log(path if os.path.exists(path) else None, diagnostics.folder())

    def _rode_app_seen(self) -> None:
        """RODE's app has opened (`rode_app.on_seen`, on its watcher's thread): said in the app's
        own voice, heard whatever is in front."""
        speech.say(rode_app.NOTICE)

    def session_ending(self) -> None:
        self._end_now(_SESSION_ENDING)

    def _end_now(self, why: str) -> None:
        """End the process at once: the service lets go of the shortcuts, the HID handles and the
        port, then the process exits without unwinding the window.

        For the system ending the session and the installer closing the app, which both expect the
        process gone promptly. Quit's own path ends each open dialog's modal loop first, and an
        update always has its dialog open when the installer closes the app; a loop that does not
        end there leaves the process running and the installer unable to replace it. Both the icon
        and wx deliver the end of the session, so this runs once."""
        if self._ended:
            return
        self._ended = True
        self._quitting = True
        diagnostics.event("app.end_now", why)
        try:
            self.icon.remove()
            service.stop(why)
        except Exception as exc:
            diagnostics.event("app.end_now_failed", f"{type(exc).__name__}: {exc}")
        _exit(0)

    def _toggle_announcer(self) -> None:
        # Saved or not by `action_set_announce`, by the same rule as the window's switch and the
        # shortcut.
        announcer.action_set_announce(not speech.enabled.is_set())
        if self.frame:
            self.frame.reread("settings")

    def request_quit(self) -> None:
        """Quit, unless the desk is part-way through being changed (`desk_access.work_in_progress`):
        the window then says what is running, and the app stays. The session ending quits
        regardless, through `_end_now`."""
        busy = desk_access.work_in_progress()
        if busy is None:
            self._quit()
            return
        diagnostics.event("app.quit_refused", busy)
        self.open_window()
        if self.frame:
            wx.CallAfter(self._still_working, busy)

    def _still_working(self, busy: str) -> None:
        dialog = TextDialog(self.frame, "Still working",
                            f"{busy} Quit again once it finishes, so the desk is not left half "
                            "written.", [(wx.ID_OK, "OK")], default=wx.ID_OK, escape=wx.ID_OK)
        with dialog:
            dialog.ShowModal()

    def _quit(self) -> None:
        """Quit: the window, the icon, then the service. Safe to call more than once."""
        if self._quitting:
            return
        self._quitting = True
        diagnostics.event("app.quitting", "closing the window and its dialogs")
        self.icon.remove()
        self._close_window(set(), time.monotonic())

    def _close_window(self, ended: set[int], since: float) -> None:
        # A dialog open in the window runs a loop of its own inside this one, and the process waits
        # on it. Only the innermost loop can be ended, so the dialogs end newest first, each once
        # the one inside it has returned. `ended` holds the ids of those already asked to end.
        modal = dialogs.modal_dialogs()
        if modal:
            if time.monotonic() - since > DIALOGS_END_WITHIN_S:
                self._end_now(f"{modal[-1].GetTitle()!r} did not close")
                return
            newest = modal[-1]
            if id(newest) not in ended:
                ended.add(id(newest))
                try:
                    newest.EndModal(wx.ID_CANCEL)
                except Exception as exc:
                    # wxPython raises a failed wx assertion as an exception. The check below still
                    # has to run, or the quit stops here with the icon already gone.
                    diagnostics.event("app.end_modal_failed", f"{type(exc).__name__}: {exc}",
                                      dialog=newest.GetTitle())
            wx.CallLater(50, self._close_window, ended, since)
            return
        if self.frame:
            self.frame.Close(force=True)
        self._window_gone = True
        if not self._starting:
            self._finish_quit()

    def _finish_quit(self) -> None:
        service.stop("quit from the tray")
        self.icon.destroy()
        # Not `Exit`, which is ignored unless this loop is the active one: Quit can arrive inside a
        # dialog's loop, which ends first.
        self.loop.ScheduleExit()


def _claim_when_free(wait_s: float) -> bool:
    """Take the single-instance lock once a quitting copy lets it go, trying for up to `wait_s`."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        time.sleep(0.25)
        if instance.claim():
            return True
    return False


def _refuse_other_account(interactive: bool) -> None:
    """Say that another account already has the app. Only when started by a person: at sign-in
    nobody is watching, and a blocking dialog would sit there unexplained."""
    diagnostics.event("app.refused", "another account is running AccessCaster")
    if interactive:
        show_refusal(_OTHER_ACCOUNT)


def main() -> int:
    # STARTED AT SIGN-IN, OR BY A PERSON? (`autostart.started_at_login`) At sign-in we come up in
    # the tray with the announcer and the shortcuts live and open NOTHING: a window taking the focus
    # of whatever you signed in to do is the reason people turn autostart off again. A restart
    # after an update opens the window: the update was started from it, and App settings there
    # reads what the update did.
    open_window = not autostart.started_at_login()

    # Single instance, across every account. The lock held, the copy is either this session's,
    # which shows its window while this one goes, one that is quitting, whose lock is waited for,
    # or another account's, which is refused.
    running = None
    if not instance.claim():
        running = find_running(_FIND_RUNNING_S)
        if running is None and not _claim_when_free(_QUITTING_COPY_S):
            _refuse_other_account(open_window)
            return 1
    if running is not None:
        if open_window:
            open_running(running)
        return 0

    app = wx.App()
    # An event loop of our own rather than `app.MainLoop`, which returns at once when no window
    # exists, as at sign-in, and ends when the last one is destroyed. This one runs until Quit.
    loop = wx.GUIEventLoop()
    TrayApp(app, loop, open_window)
    activator = wx.EventLoopActivator(loop)
    loop.Run()
    del activator
    return 0


if __name__ == "__main__":
    sys.exit(main())
