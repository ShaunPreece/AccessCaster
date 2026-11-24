"""A STAND-IN for macOS's process lookup: RODE's app is never seen running."""

PROCESS = "RODECaster App"


def running(process: str) -> bool:
    return False
