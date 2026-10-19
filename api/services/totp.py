"""RFC 6238 time-based one-time codes, on the standard library.

Ported verbatim from Guardian (sentinel/totp.py, G-28, #32) for the platform's
optional 2FA (v0.9.0). The issuer is the platform's APP_NAME.

No `pyotp`. This module decides whether somebody gets into the thing that
protects a customer's exchange accounts, and taking a supply-chain dependency
inside the authentication path of a security product - for roughly forty lines
of HMAC - is a worse trade than writing them.

Two constants carry most of the security here and both are deliberately
constants rather than parameters:

`STEP_SECONDS` is the standard thirty. `SKEW_STEPS` is one, which means three
codes are valid at any instant: the previous, the current and the next. That is
the smallest window that tolerates a phone whose clock has drifted, and every
extra step multiplies what an attacker may guess. Making it a parameter would
mean a call site could widen it, and the call site that eventually does will be
the one written in a hurry.

Replay is the caller's job and this module makes it possible rather than
optional: `verify_code` returns the *step number* it matched, and the caller
stores it and passes it back. A code is then usable exactly once, and so is
every earlier code - otherwise the skew tolerance becomes a replay window.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from urllib.parse import quote

# The RFC 6238 default, and what every authenticator app assumes.
STEP_SECONDS = 30

# Steps of tolerance either side of now. See the module docstring: one, and not
# a parameter.
SKEW_STEPS = 1

DIGITS = 6

# 160 bits, the RFC 6238 recommendation, base32-encoded to 32 characters.
_SECRET_BYTES = 20

# Enough that a customer who loses their phone on a trip is not locked out, few
# enough that the printed list stays something a person actually keeps.
RECOVERY_CODE_COUNT = 10


def issuer() -> str:
    """The name an authenticator app shows beside the code: APP_NAME, read
    at call time so tests and deployments that set it are honoured."""
    from api.config import settings
    return settings.app_name


def generate_secret() -> str:
    """A fresh shared secret, base32 with no padding.

    From `secrets`, never derived from the account name or the time. A secret
    that can be recomputed from something public is not a secret, and that is
    exactly the shape a future "we can regenerate it if they lose it"
    convenience would take."""
    return base64.b32encode(secrets.token_bytes(_SECRET_BYTES)).decode().rstrip("=")


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """Single-use codes that bypass the second factor entirely.

    Which makes them exactly as valuable as the password, so they are long
    enough to be worth nothing to a guesser online or to anyone holding a copy
    of the database: 80 bits of `secrets` output, so the single unsalted SHA-256
    they are stored under cannot be brute-forced offline either (48 bits could
    be, in hours on one GPU). Short friendly codes are how a recovery feature
    becomes the easiest way in. Grouped with a hyphen only so a human can read
    one off a piece of paper without losing their place. Codes issued at the
    older 48-bit length still verify: the hash is of the normalised string."""
    codes = []
    for _ in range(count):
        raw = secrets.token_hex(10).upper()  # 20 hex chars, 80 bits
        codes.append("-".join(raw[i:i + 5] for i in range(0, 20, 5)))
    return codes


def _decode_secret(secret: str) -> bytes | None:
    """None for anything unusable. Fails closed: a corrupted or truncated
    secret must reject every code rather than accept them."""
    if not secret:
        return None
    padded = secret.strip().replace(" ", "").upper()
    padded += "=" * (-len(padded) % 8)
    try:
        raw = base64.b32decode(padded, casefold=True)
    except (ValueError, TypeError):
        return None
    return raw or None


def totp_at(secret: str, at: float) -> str:
    """The code for the step containing `at`. Empty string for a bad secret."""
    key = _decode_secret(secret)
    if key is None:
        return ""
    return _code_for_step(key, int(at) // STEP_SECONDS)


def _code_for_step(key: bytes, step: int) -> str:
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFF_FFFF
    # Zero-padded. A code that renders as "12345" because its leading zero was
    # dropped is a code the customer cannot type.
    return str(truncated % (10 ** DIGITS)).zfill(DIGITS)


def verify_code(secret: str, code: str | None, at: float,
                last_used_step: int | None = None) -> int | None:
    """The step number this code matched, or None.

    Returning the step rather than a bool is what makes replay protection
    possible: the caller stores it and passes it back as `last_used_step`, and
    every step at or below that is then refused. Without it a code stays valid
    for the whole ninety-second window, which is long enough for somebody who
    read it over a shoulder or found it in a proxy log.

    Never raises. This runs on unauthenticated input, where an exception is a
    500 that tells the sender something and a crash loop is a denial of
    service."""
    key = _decode_secret(secret)
    if key is None or not code:
        return None
    candidate = str(code).strip().replace(" ", "")
    if len(candidate) != DIGITS or not candidate.isdigit():
        return None

    current = int(at) // STEP_SECONDS
    for offset in range(-SKEW_STEPS, SKEW_STEPS + 1):
        step = current + offset
        if last_used_step is not None and step <= last_used_step:
            # Not just "this exact code" - every earlier step too. Otherwise
            # the previous step's code still works after the current one has
            # been spent, and the skew tolerance IS the replay window.
            continue
        if hmac.compare_digest(_code_for_step(key, step), candidate):
            return step
    return None


def provisioning_uri(secret: str, account_name: str, issuer_name: str | None = None) -> str:
    """The `otpauth://` URI an authenticator app scans.

    The issuer is not decoration. Without it the entry reads as an anonymous
    six digits among a dozen others, and the one the customer needs during an
    incident is the one they cannot find."""
    issuer_value = issuer_name or issuer()
    label = quote(f"{issuer_value}:{account_name}", safe="")
    params = f"secret={quote(secret, safe='')}&issuer={quote(issuer_value, safe='')}" \
             f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    return f"otpauth://totp/{label}?{params}"


def hash_recovery_code(code: str) -> str:
    """Recovery codes are stored hashed, like passwords - a database read must
    not yield a way past the second factor.

    SHA-256 rather than scrypt: these are 80 bits of `secrets` output with no
    dictionary to attack, so a work factor would buy nothing and cost a second
    on every login attempt. Normalised first so a customer typing it back with
    or without the hyphens, in either case, still gets in."""
    normalised = (code or "").strip().upper().replace("-", "").replace(" ", "")
    return hashlib.sha256(normalised.encode()).hexdigest()
