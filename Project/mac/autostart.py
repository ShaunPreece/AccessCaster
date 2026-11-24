"""A STAND-IN for macOS's sign-in entry: there is never one, and writing one refuses."""


def command_for(exe: str, flag: str) -> str:
    return f'"{exe}" {flag}'


def current() -> str | None:
    return None


def install(command: str) -> None:
    raise OSError("Starting when you sign in is not built for macOS yet.")


def remove() -> None:
    pass
