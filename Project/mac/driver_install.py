"""A STAND-IN for macOS's driver download: RODE's Mac driver is not fetched, and checking or
starting an installer refuses."""

# Empty, so a download fails at once rather than fetching the Windows driver.
ZIP_URL = ""
INSTALLER_SUFFIX = ".pkg"


def signature(path: str):
    raise OSError("Checking an installer's signature is not built for macOS yet.")


def launch(path: str) -> None:
    raise OSError("Installing RODE's driver is not built for macOS yet.")
