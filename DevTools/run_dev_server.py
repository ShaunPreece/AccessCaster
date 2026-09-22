"""Start the AccessCaster service the way a DEV session wants it, in the foreground.

Why this exists. A server-side change needs the SERVICE RESTARTED, not the window reloaded (the
lesson the announcer pickers taught, docs/PROJECT_GUIDE.md), so any session that touches
server.py / soundpad.py / channels.py has to bring the service back up afterwards. Doing that with
`python -c "import server; ..."` through PowerShell's Start-Process is a quoting trap: the multi-line
argument gets mangled and the process dies silently, leaving nothing on port 8770 and no error to
read. A real file cannot be mis-quoted.

Differences from the installed tray app, all deliberate for a dev session:
  - watchdog OFF, so a headless-browser render (DevTools/ui_probe.py) cannot make the page's
    pagehide beacon shut the service down;
  - no browser opened, since the page is usually already open or not wanted;
  - announcer ON, because that is how Shaun actually uses the desk and a restart should not leave
    him without spoken button presses.

Usage:
    python DevTools/run_dev_server.py            # foreground; Ctrl+C to stop
Run it with the Bash/PowerShell tool's background flag to leave it serving.
"""
import _bootstrap  # noqa: F401  - puts Project/ on sys.path

import server


def main():
    srv = server._bring_up_server(open_browser=False, watchdog=False, announcer=True)
    if not srv:
        print("could not bring the server up (is something already on port 8770?)")
        return 1
    print("serving on http://127.0.0.1:8770 (watchdog off, announcer on)")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
