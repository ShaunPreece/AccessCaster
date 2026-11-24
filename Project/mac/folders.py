"""macOS's `folders.open_focused`, in the Finder. `open` brings the Finder to the front with the
folder's window, which VoiceOver follows."""
import subprocess


def open_focused(folder: str) -> None:
    subprocess.run(["open", folder], check=True, capture_output=True)
