"""
signing.py

Checking that an index file was written by us.

WHY THIS EXISTS AT ALL. The index names the installer to download and the hash
it should have, so a checksum alone proves nothing: anybody who could replace
the installer in the bucket could replace the checksum beside it. Signing the
index means a compromised bucket is not enough on its own, because the private
key is not in it. The private half lives offline on one machine and goes
neither near the bucket nor into any repository.

IT IS NOT CODE SIGNING and does not stop Windows warning on a first install.
Different question, different answer.

The signature is a SEPARATE FILE holding 64 raw bytes, never embedded in the
JSON: with it inside the document, the signed bytes would have to be some
canonical re-serialisation of the rest, and key order, whitespace and unicode
escaping would all have to match between the tool that signs and the app that
verifies. When they do not, verification fails on a perfectly genuine document
and the failure looks exactly like tampering.

Ed25519 rather than the ECDSA that .NET and Python both have in the box, and
the reason is INTEROP, not strength: one tool signs in C# and three apps
verify, and ECDSA has several ways for the two sides to disagree about
encoding, curve and hash. Ed25519 has one of each, and is deterministic, so
the nonce-reuse failure that leaks an ECDSA private key does not exist.
"""

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# An Ed25519 signature is always exactly this long. Named rather than a bare
# 64 beside the key's own 64 hex characters, which are a different 64.
SIGNATURE_BYTES = 64

# Our public key, 32 bytes as lower-case hex. Not a secret: it only verifies.
#
# A key that is not exactly 64 hex characters refuses every signature (see
# `verify`), so an unset one fails closed. That is the only safe way to leave
# this constant in a public repository: one that accepted anything would be a
# hole in the source waiting for somebody to forget about it.
PUBLIC_KEY_HEX = "4ede0be590d77ba9ed1e095e3c231003347a8aa5928e85783416d4f42e8be3c4"


def is_configured(public_key_hex=None):
    """True once a real key is built in. Until then there is nothing to check
    an answer against, so AccessCaster does not check for updates at all rather
    than asking and silently refusing every answer."""
    return len(public_key_hex if public_key_hex is not None
               else PUBLIC_KEY_HEX) == 64


def verify(index, signature, public_key_hex=None):
    """Was `index` signed by us? `index` is the exact bytes of index.json as
    fetched, not a re-serialisation: the signature covers the file, which is
    the whole reason it is a separate file. `signature` is the bytes of
    index.json.sig.

    FALSE FOR EVERY FAILURE, including a malformed key, a signature of the
    wrong length and anything the library raises. There is exactly one way to
    say yes and every other path says no, which is the shape a security check
    has to have: an exception caught somewhere up the stack is how "could not
    check" turns into "carried on anyway"."""
    key_hex = public_key_hex if public_key_hex is not None else PUBLIC_KEY_HEX
    # Checked before anything else, so a truncated or empty .sig file is
    # refused here rather than deeper in.
    if len(signature) != SIGNATURE_BYTES or len(key_hex) != 64:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_hex)).verify(
            bytes(signature), bytes(index))
        return True
    except Exception:
        return False
