"""A STAND-IN for RODE's macOS Virtual Devices driver: it reads as not installed, and nothing is
changed."""
import threading


def driver_state() -> dict:
    return {"installed": False}


def service_state() -> dict:
    return {}


def sync_endpoints(status, expect_connected, move_default: bool, parked: str) -> dict:
    return {}


def park_default():
    return None


def hold_default(endpoint_id: str) -> bool:
    return False


def default_to_system(timeout_s: float) -> str:
    return ""


def virtual_endpoint_counts() -> tuple:
    return 0, 0


def watch_endpoints(on_change, stop: threading.Event) -> None:
    stop.wait()
