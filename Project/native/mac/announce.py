"""A STAND-IN for macOS's `announce.Notifier`: every line is dropped, so VoiceOver hears none of
the window's spoken replies. The Mac's needs NSAccessibility's announcement request."""


class Notifier:
    def __init__(self, window, name):
        self.name = name
        self.closed = False

    def say(self, text, recent=False):
        return None

    def close(self):
        self.closed = True
