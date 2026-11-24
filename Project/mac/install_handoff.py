"""A STAND-IN for macOS's installer hand-off: there is no Mac installer, so it refuses."""


def start(installer: str) -> None:
    raise OSError("Installing an update is not built for macOS yet.")
