"""The system's text to speech, rendered into memory for `wave_player` to play.

`apartment()` is a context manager every thread wraps its use of this module in; no object made
inside it may outlive it.

`Synth(sample_rate)` is a voice at the app's speaking rate, at first the system's default voice:
  - `voice_names()` and `output_names()`: what the system offers, by the names a user sees.
  - `use_voice(name)`: speak with the voice of that exact name.
  - `output_device(name)`: the `wave_player` device for the output of that exact name, resolved
    afresh at every open, or `wave_player.DEFAULT_DEVICE` when it is not there.
  - `render(text)`: `text` spoken as 16-bit mono PCM at `sample_rate`, never parsed as markup, with
    whatever silence the voice pads it with.

Each platform supplies its own.
"""
import sys

if sys.platform == "win32":
    from win.speech_engine import Synth, apartment
elif sys.platform == "darwin":
    from mac.speech_engine import Synth, apartment
else:
    raise ImportError(f"No speech engine for {sys.platform}: the Windows one is "
                      "win/speech_engine.py")

__all__ = ["Synth", "apartment"]
