"""A STAND-IN for macOS's level meters: the system lists no audio devices, so the meters say the
desk's streams are not there."""


def remote_session() -> bool:
    return False


def list_endpoints() -> list:
    return []


class EndpointReader:
    def __init__(self, endpoint_id: str):
        raise OSError("Level meters are not built for macOS yet.")


def read_level(endpoint_id: str) -> dict:
    raise OSError("Reading a device's level is not built for macOS yet.")


def write_mute(endpoint_id: str, muted: bool) -> bool:
    raise OSError("Muting a device is not built for macOS yet.")


def write_level(endpoint_id: str, db: float) -> float:
    raise OSError("Setting a device's level is not built for macOS yet.")
