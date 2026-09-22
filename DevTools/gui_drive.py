"""Optimised pure-Python GUI driver for the RODE app (WebView2/Chromium). No PowerShell .ps1,
no -ExecutionPolicy bypass, no extra deps beyond Pillow (PIL.ImageGrab) + stdlib ctypes.

Why this shape: the app is a WebView2 app with NO usable CDP (debug port disabled) and a poor/
inconsistent UIA tree (unnamed "Custom" elements), so we cannot read it as text or target named
elements - screenshots remain the only reliable way to SEE it. This driver makes that loop fast and
navigation reliable:
  * CACHED COORDS: ui_map.json (beside this script) holds known click targets per screen, so we click a named
    control with `tap <screen.target>` WITHOUT a screenshot-to-find step.
  * CROPPED/SCALED shots: `shot --crop L,T,R,B --scale N` grabs only the relevant panel, small and
    fast to read (the image read is the slow part).
  * ONE-ROUND-TRIP verify: `tapshot <screen.target> --crop ...` clicks then crops-shots in one call.
  * The Output sidebar can reset to Headphones; use tapshot + eyeball + retry for sticky nav.

Commands:
  python gui_drive.py maximize
  python gui_drive.py rect
  python gui_drive.py shot <out.png> [--crop L,T,R,B] [--scale N]
  python gui_drive.py click <wx> <wy> [double]
  python gui_drive.py tap <screen.target> [double]            # click a mapped coord
  python gui_drive.py tapshot <screen.target> <out.png> [--crop L,T,R,B] [--scale N] [double]
  python gui_drive.py map [screen]                            # print the coord map

Coords are WINDOW-RELATIVE (window maximized to 0,0). --crop is window-relative L,T,R,B.
"""
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

u32 = ctypes.windll.user32
try:
    u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        u32.SetProcessDPIAware()
    except Exception:
        pass

SW_RESTORE, SW_MAXIMIZE = 9, 3
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
TITLE_MATCH = "DECaster App"
MAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_map.json")


def find_window():
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        if TITLE_MATCH in buf.value:
            found.append(hwnd)
        return True

    u32.EnumWindows(enum, 0)
    return found[0] if found else None


def get_rect(hwnd):
    r = wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def foreground(hwnd):
    u32.keybd_event(0x12, 0, 0, 0)
    u32.keybd_event(0x12, 0, 2, 0)
    u32.SetForegroundWindow(hwnd)


def load_map():
    with open(MAP_PATH, encoding="utf-8") as f:
        return json.load(f)


def resolve_target(spec):
    """'screen.target' -> (x, y) from ui_map.json, or None."""
    m = load_map()
    if "." not in spec:
        raise SystemExit(f"target must be screen.target, got {spec!r}")
    screen, name = spec.split(".", 1)
    try:
        return tuple(m[screen][name])
    except KeyError:
        raise SystemExit(f"unknown target {spec!r}; see `gui_drive.py map`")


def _parse_flags(argv):
    """Pull --crop L,T,R,B and --scale N out of argv; return (crop_tuple|None, scale, rest)."""
    crop, scale, rest = None, 1, []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--crop" and i + 1 < len(argv):
            crop = tuple(int(x) for x in argv[i + 1].split(","))
            i += 2
        elif a == "--scale" and i + 1 < len(argv):
            scale = float(argv[i + 1])
            i += 2
        else:
            rest.append(a)
            i += 1
    return crop, scale, rest


def cmd_maximize(hwnd):
    if u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, SW_RESTORE)
    u32.ShowWindow(hwnd, SW_MAXIMIZE)
    foreground(hwnd)
    time.sleep(0.4)
    l, t, r, b = get_rect(hwnd)
    print(f"MAXIMIZED rect L={l} T={t} W={r - l} H={b - t}")


def cmd_shot(hwnd, out, crop=None, scale=1, refocus=True):
    from PIL import ImageGrab
    if refocus:
        foreground(hwnd)
        time.sleep(0.3)
    l, t, r, b = get_rect(hwnd)
    if crop:
        cl, ct, cr, cb = crop
        bbox = (l + cl, t + ct, l + cr, t + cb)
    else:
        bbox = (l, t, r, b)
    img = ImageGrab.grab(bbox=bbox, all_screens=True)
    if scale and scale != 1:
        img = img.resize((max(1, int(img.width / scale)), max(1, int(img.height / scale))))
    img.save(out)
    print(f"SHOT {out} ({img.width}x{img.height})"
          + (f" crop={crop}" if crop else "") + (f" scale={scale}" if scale != 1 else ""))


def cmd_click(hwnd, wx, wy, double=False, refocus=True):
    l, t, r, b = get_rect(hwnd)
    if refocus:
        foreground(hwnd)
        time.sleep(0.2)
    sx, sy = l + wx, t + wy
    cur = wintypes.POINT()
    u32.GetCursorPos(ctypes.byref(cur))
    for i in range(1, 13):
        u32.SetCursorPos(int(cur.x + (sx - cur.x) * i / 12), int(cur.y + (sy - cur.y) * i / 12))
        time.sleep(0.018)
    u32.SetCursorPos(sx, sy)
    time.sleep(0.25)
    u32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.06)
    u32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    if double:
        time.sleep(0.08)
        u32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.06)
        u32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    print(f"CLICK win=({wx},{wy}) screen=({sx},{sy}){' double' if double else ''}")


def cmd_drag(hwnd, x1, y1, x2, y2, steps=24, refocus=True):
    """Left-button click-drag from (x1,y1) to (x2,y2), window-relative. Stepped move so the
    Chromium layer sees real mousemoves during the drag (same technique as drag.ps1). Used for
    canvas/carousel swipes that clicks can't do."""
    l, t, r, b = get_rect(hwnd)
    if refocus:
        foreground(hwnd)
        time.sleep(0.2)
    sx1, sy1, sx2, sy2 = l + x1, t + y1, l + x2, t + y2
    u32.SetCursorPos(sx1, sy1)
    time.sleep(0.15)
    u32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.12)
    for s in range(1, steps + 1):
        u32.SetCursorPos(int(sx1 + (sx2 - sx1) * s / steps), int(sy1 + (sy2 - sy1) * s / steps))
        time.sleep(0.018)
    time.sleep(0.1)
    u32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    print(f"DRAG ({x1},{y1})->({x2},{y2}) steps={steps}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "rect"
    if cmd == "map":
        m = load_map()
        if len(sys.argv) > 2:
            print(json.dumps(m.get(sys.argv[2], {}), indent=2))
        else:
            for screen, targets in m.items():
                if screen.startswith("_"):
                    continue
                print(f"{screen}: {', '.join(targets)}")
        return 0

    hwnd = find_window()
    if not hwnd:
        print("ERROR: RODE window not found (is the app open?)")
        return 1

    if cmd == "maximize":
        cmd_maximize(hwnd)
    elif cmd == "rect":
        l, t, r, b = get_rect(hwnd)
        print(f"rect L={l} T={t} W={r - l} H={b - t}")
    elif cmd == "shot":
        crop, scale, rest = _parse_flags(sys.argv[2:])
        cmd_shot(hwnd, rest[0] if rest else "gui_shot.png", crop, scale)
    elif cmd == "click":
        crop, scale, rest = _parse_flags(sys.argv[2:])
        cmd_click(hwnd, int(rest[0]), int(rest[1]), len(rest) > 2 and rest[2] == "double")
    elif cmd == "drag":
        crop, scale, rest = _parse_flags(sys.argv[2:])
        steps = int(rest[4]) if len(rest) > 4 else 24
        cmd_drag(hwnd, int(rest[0]), int(rest[1]), int(rest[2]), int(rest[3]), steps)
    elif cmd == "tap":
        crop, scale, rest = _parse_flags(sys.argv[2:])
        x, y = resolve_target(rest[0])
        cmd_click(hwnd, x, y, len(rest) > 1 and rest[1] == "double")
    elif cmd == "tapshot":
        crop, scale, rest = _parse_flags(sys.argv[2:])
        x, y = resolve_target(rest[0])
        out = rest[1] if len(rest) > 1 else "gui_shot.png"
        cmd_click(hwnd, x, y, len(rest) > 2 and rest[2] == "double")
        time.sleep(1.4)                       # let the screen settle, then verify
        cmd_shot(hwnd, out, crop, scale, refocus=False)
    else:
        print(f"unknown cmd: {cmd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
