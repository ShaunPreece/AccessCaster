"""A STAND-IN for macOS's `app_window.raise_window`: the window is shown, but the app is not
brought to the front, so VoiceOver stays where it was until the user switches to it."""
import diagnostics


def raise_window(handle: int) -> bool:
    diagnostics.event("window.raise", "not on macOS yet")
    return False
