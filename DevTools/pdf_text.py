"""Pull the visible text out of a PDF with nothing but the stdlib.

Exists because the reference material for this project arrives as vendor PDFs (RODE's guides, the
Vortex CallMe manual) on a machine with no PDF library installed, and a manual read of a vendor
document beats guessing at the hardware every time.

Handles the common case only: Flate-compressed content streams with text drawn by Tj/TJ. Scanned
pages carry no text operators and yield nothing, which is the honest answer rather than a wrong one.

    python DevTools/pdf_text.py <file.pdf> [out.txt]
"""
import re
import sys
import zlib
from pathlib import Path

# A PDF string literal: balanced-free scan that respects backslash escapes, which is why this is a
# hand-rolled loop rather than a regex - a character class for "not a paren or backslash" trips over
# the escapes that legitimately contain both.
def _strings(chunk: bytes):
    out, depth, buf, esc = [], 0, bytearray(), False
    for byte in chunk:
        ch = bytes([byte])
        if depth == 0:
            if ch == b"(":
                depth, buf, esc = 1, bytearray(), False
            continue
        if esc:
            buf += ch
            esc = False
            continue
        if ch == b"\\":
            esc = True
            continue
        if ch == b"(":
            depth += 1
        elif ch == b")":
            depth -= 1
            if depth == 0:
                out.append(bytes(buf))
                continue
        buf += ch
    return out


def extract(path: Path) -> str:
    raw = path.read_bytes()
    pieces = []
    for m in re.finditer(rb"stream\r?\n", raw):
        start = m.end()
        end = raw.find(b"endstream", start)
        if end < 0:
            continue
        try:
            pieces.append(zlib.decompress(raw[start:end]))
        except zlib.error:
            pieces.append(raw[start:end])
    words = []
    for chunk in pieces:
        if b"Tj" not in chunk and b"TJ" not in chunk:
            continue
        for s in _strings(chunk):
            words.append(s.decode("latin-1"))
    text = " ".join(words)
    # PDF splits words across show operators, so collapse the runs of spaces that leaves behind.
    text = re.sub(r"[ \t]{2,}", " ", text)
    # A SCANNED PDF MUST RETURN NOTHING, NOT NOISE. An image-only page still carries a stream that
    # happens to contain the two-byte sequence "Tj", so the scan above harvests decompressed JPEG
    # as if it were text and yields a plausible-looking wall of mojibake. Judging it by the share of
    # printable characters is what separates a real text layer from that.
    sample = text[:5000]
    if sample and sum(c.isprintable() for c in sample) / len(sample) < 0.9:
        return ""
    return text


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    text = extract(src)
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(text, encoding="utf-8")
        print(f"{len(text)} chars -> {sys.argv[2]}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
