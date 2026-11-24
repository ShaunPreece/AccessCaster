"""A STAND-IN for macOS's desk drive lookup: the desk's storage is never found, so pad and file
transfers say the desk's storage did not appear."""
import contextlib
import os


def desk_roots() -> set[str]:
    return set()


def name(root: str) -> str:
    return os.path.basename(os.path.normpath(root)) or root


def flush(root: str) -> None:
    raise OSError("Flushing the desk's storage is not built for macOS yet.")


def dismount(root: str) -> bool:
    return False


def quiet_errors():
    return contextlib.nullcontext()
