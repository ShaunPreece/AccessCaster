"""Proves the two properties the single-instance lock depends on, on this machine's TCP stack.

1. TWO SERVERS CANNOT SHARE THE PORT any more. The old setting is exercised alongside, because the
   fix is only worth having if the old behaviour really did allow a double bind.
2. A RESTART STILL BINDS after a clean stop with connections behind it. `allow_reuse_address = 0`
   is what makes Windows refuse a rebind while sockets sit in TIME_WAIT, so this is the regression
   the change could plausibly cause.

Run it with nothing else on the port: `python DevTools/check_exclusive_bind.py`.
"""

import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8791          # not 8770: the app may be running


class Quiet(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


class Shared(ThreadingHTTPServer):
    allow_reuse_address = True


class Exclusive(ThreadingHTTPServer):
    allow_reuse_address = False


def double_bind(cls) -> bool:
    """True if a second server of this class can bind a port the first already holds."""
    first = cls(("0.0.0.0", PORT), Quiet)
    try:
        second = cls(("0.0.0.0", PORT), Quiet)
        second.server_close()
        return True
    except OSError:
        return False
    finally:
        first.server_close()


def rebind_after_traffic() -> bool:
    """True if an exclusive server rebinds immediately after a clean stop that served requests."""
    srv = Exclusive(("0.0.0.0", PORT), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for _ in range(5):
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=2) as r:
            r.read()
    srv.shutdown()
    srv.server_close()
    try:
        again = Exclusive(("0.0.0.0", PORT), Quiet)
        again.server_close()
        return True
    except OSError:
        return False


def main() -> int:
    shared = double_bind(Shared)
    exclusive = double_bind(Exclusive)
    rebind = rebind_after_traffic()

    print(f"double bind with allow_reuse_address=True : {'ALLOWED' if shared else 'refused'}")
    print(f"double bind with allow_reuse_address=False: {'ALLOWED' if exclusive else 'refused'}")
    print(f"rebind straight after a clean stop        : {'ok' if rebind else 'FAILED'}")

    ok = shared and not exclusive and rebind
    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
