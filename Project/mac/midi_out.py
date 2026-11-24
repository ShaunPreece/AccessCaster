"""A STAND-IN for macOS's MIDI output: there are no ports, and opening one refuses."""


def ports() -> list:
    return []


def find(name: str):
    return None


def open_port(name: str):
    raise RuntimeError("MIDI output is not built for macOS yet.")


def send(handle, status: int, data1: int, data2: int) -> None:
    raise OSError("MIDI output is not built for macOS yet.")


def close_port(handle) -> None:
    pass
