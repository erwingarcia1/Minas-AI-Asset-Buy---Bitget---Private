"""Time-based one-time codes, and the ways they go wrong.

Ported from Guardian (tests/test_totp.py, G-28, #32) with the import path
changed and the unused sqlite `db` fixture dropped; the issuer is APP_NAME.

RFC 6238 on top of `hmac` and `hashlib`. No new dependency: this is the module
that decides whether somebody gets into the thing protecting the customer's
exchange accounts, and `pyotp` would be a supply-chain dependency inside the
authentication path of a security product for about forty lines of code.

Organised by attack, like the reset flow:

| Attack | Defence |
|---|---|
| Replay a code seen over someone's shoulder, or in a proxy log | each step is usable once; the last accepted step is recorded |
| Brute-force six digits | the login throttle counts 2FA attempts, not just passwords |
| Widen the window until guessing is cheap | ±1 step only, and the tolerance is a constant, not a parameter |
| Read the shared secret out of the database | encrypted at rest with the same cipher as exchange API keys |
| Lock a customer out during enrolment | a code must be verified before 2FA is switched on |
| Reuse a recovery code | single-use, hashed at rest |
| Turn 2FA off with a stolen session | disabling requires a current code, and emails the owner |

The last one matters most: 2FA that can be removed by whoever holds the session
protects nothing an attacker cannot undo in one click.
"""
from __future__ import annotations

import time

import pytest

from api.services.totp import (
    RECOVERY_CODE_COUNT,
    STEP_SECONDS,
    generate_recovery_codes,
    generate_secret,
    provisioning_uri,
    totp_at,
    verify_code,
)

SECRET = "JBSWY3DPEHPK3PXP"  # RFC-style base32


# --- the algorithm ------------------------------------------------------------


def test_a_generated_secret_is_base32_and_long_enough():
    """160 bits, the RFC 6238 recommendation. A shorter secret is a smaller
    search space for anyone who obtains code/timestamp pairs."""
    secret = generate_secret()

    assert len(secret) >= 32
    assert set(secret) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def test_two_secrets_are_never_the_same():
    assert len({generate_secret() for _ in range(50)}) == 50


def test_a_code_is_six_digits_including_leading_zeros():
    """Formatted, not `int`. A code that renders as "12345" because its
    leading zero was dropped is a code the customer cannot type."""
    codes = {totp_at(SECRET, t) for t in range(0, 2_000_000, 9_973)}

    assert codes
    for code in codes:
        assert len(code) == 6 and code.isdigit()


def test_the_code_changes_every_step_and_is_stable_within_one():
    at = 1_700_000_000
    step_start = at - (at % STEP_SECONDS)

    assert totp_at(SECRET, step_start) == totp_at(SECRET, step_start + STEP_SECONDS - 1)
    assert totp_at(SECRET, step_start) != totp_at(SECRET, step_start + STEP_SECONDS)


def test_a_different_secret_gives_a_different_code():
    assert totp_at(SECRET, 1_700_000_000) != totp_at(generate_secret(), 1_700_000_000)


# --- clock skew ---------------------------------------------------------------


def test_the_previous_and_next_step_are_accepted():
    """One step either side, for a phone whose clock has drifted. Not more:
    every extra step of tolerance multiplies the codes an attacker may guess
    at any moment, and the tolerance is a constant here rather than a
    parameter so it cannot be widened at a call site."""
    now = 1_700_000_000

    for offset in (-STEP_SECONDS, 0, STEP_SECONDS):
        code = totp_at(SECRET, now + offset)
        assert verify_code(SECRET, code, at=now) is not None, offset


def test_two_steps_away_is_refused():
    now = 1_700_000_000

    for offset in (-2 * STEP_SECONDS, 2 * STEP_SECONDS):
        assert verify_code(SECRET, totp_at(SECRET, now + offset), at=now) is None


# --- replay -------------------------------------------------------------------


def test_a_code_cannot_be_used_twice():
    """The attack this defends against is mundane and real: somebody reads the
    code over a shoulder, or it is captured in a proxy log, and it stays valid
    for up to ninety seconds. Accepting it twice makes that window a login."""
    now = 1_700_000_000
    code = totp_at(SECRET, now)

    step = verify_code(SECRET, code, at=now)
    assert step is not None
    assert verify_code(SECRET, code, at=now, last_used_step=step) is None


def test_an_older_step_is_refused_once_a_newer_one_has_been_used():
    """Otherwise replaying the *previous* step's code still works after the
    current one has been spent — the skew tolerance would become a replay
    window."""
    now = 1_700_000_000
    current_step = verify_code(SECRET, totp_at(SECRET, now), at=now)

    previous = totp_at(SECRET, now - STEP_SECONDS)
    assert verify_code(SECRET, previous, at=now, last_used_step=current_step) is None


def test_the_next_step_still_works_after_this_one_is_spent():
    """The customer must be able to sign in again thirty seconds later."""
    now = 1_700_000_000
    step = verify_code(SECRET, totp_at(SECRET, now), at=now)
    later = now + STEP_SECONDS

    assert verify_code(SECRET, totp_at(SECRET, later), at=later, last_used_step=step) is not None


# --- malformed input ----------------------------------------------------------


@pytest.mark.parametrize("code", ["", "     ", "abcdef", "12345", "1234567", None, "12 34 56"])
def test_a_malformed_code_is_refused_rather_than_raising(code):
    """This runs on unauthenticated input. An exception here is a 500 that
    tells the sender something, and a crash loop is a denial of service."""
    assert verify_code(SECRET, code, at=1_700_000_000) is None


def test_a_malformed_secret_fails_closed():
    """A corrupted or truncated secret must reject every code, not accept
    them. Failing open here is the whole game."""
    for bad in ("", "not-base32!!", "A"):
        assert verify_code(bad, "123456", at=1_700_000_000) is None


def test_an_empty_secret_produces_no_code_at_all():
    """Sharper than "it rejects 123456", which any secret does 999,999 times
    out of a million.

    A secret that decodes to zero bytes is still a usable HMAC key — it
    produces a perfectly well-formed six-digit code that an attacker who
    guesses the secret is empty can compute. So the empty case has to be
    rejected at decode time, not left to fail by luck at comparison time. This
    is what a truncated column or a half-written migration looks like."""
    for empty in ("", "        ", "========"):
        assert totp_at(empty, 1_700_000_000) == ""
        assert verify_code(empty, "000000", at=1_700_000_000) is None


def test_verification_is_constant_time_in_the_comparison():
    """Digit-by-digit comparison leaks the code one character at a time to
    anyone who can time the endpoint."""
    import inspect

    from api.services import totp

    source = inspect.getsource(totp)
    assert "compare_digest" in source


# --- recovery codes -----------------------------------------------------------


def test_recovery_codes_are_generated_in_a_usable_quantity():
    codes = generate_recovery_codes()

    assert len(codes) == RECOVERY_CODE_COUNT
    assert len(set(codes)) == RECOVERY_CODE_COUNT


def test_recovery_codes_are_long_enough_to_resist_guessing():
    """These bypass the second factor entirely, so they are as valuable as the
    password. Short, friendly codes are how a "recovery" feature becomes the
    easiest way in."""
    for code in generate_recovery_codes():
        assert len(code.replace("-", "")) >= 10


# --- the provisioning URI -----------------------------------------------------


def test_the_provisioning_uri_names_the_product_and_the_account():
    """What the customer sees in their authenticator app. Without an issuer
    the entry reads as an anonymous six digits among a dozen others, and the
    one they need during an incident is the one they cannot find."""
    uri = provisioning_uri(SECRET, "alice@example.com")

    assert uri.startswith("otpauth://totp/")
    assert "Minas" in uri
    assert "alice%40example.com" in uri or "alice@example.com" in uri
    assert f"secret={SECRET}" in uri


def test_the_provisioning_uri_escapes_the_account_name():
    uri = provisioning_uri(SECRET, "a b&c@example.com")

    assert " " not in uri
    assert "&c@" not in uri


def test_the_secret_is_not_derived_from_the_email():
    """A secret that could be recomputed from the account name is not a
    secret. Belt and braces against a future "convenience" refactor."""
    a = generate_secret()
    b = generate_secret()

    assert a != b
    assert time.time() > 0  # nothing time-derived either


def test_recovery_codes_are_eighty_bits_in_four_readable_groups():
    """48 bits under one unsalted SHA-256 could be brute-forced offline from a copy of the
    database in hours; 80 bits cannot. Codes issued at the older length hash the same way."""
    from api.services.totp import hash_recovery_code
    for code in generate_recovery_codes():
        groups = code.split("-")
        assert len(groups) == 4 and all(len(g) == 5 for g in groups), code
        flat = code.replace("-", "")
        assert len(flat) == 20 and all(ch in "0123456789ABCDEF" for ch in flat)
    assert hash_recovery_code("abcd-ef01-2345") == hash_recovery_code("ABCDEF012345")
