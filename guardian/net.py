"""The only place in `sentinel/` that opens a socket.

Every outbound call the service makes - Telegram, SMTP, the two SMS providers -
goes through the two functions here, and every one of them refuses to connect
when the process is a test run.

**Why a chokepoint rather than a guard per channel.** #68 was a test suite that
loaded the operator's real `.env` and then made real network calls with it: a
live SMTP server answered `535 Missing password` during `test_password_reset`,
which means the suite was one correct password away from mailing real customers.
`tests/conftest.py` now clears the environment, which stops that particular
route. This is the second layer, and it exists because the first protects only
`pytest` - a script somebody writes next month, importing `ContactRouter` to try
something out, passes through none of it. A guard written into each of the four
channels would protect the four that exist; one that lives under all of them
protects the fifth, written by someone who never read this file.

`tests/test_no_live_traffic.py` holds the scan that keeps it true: nothing in
`sentinel/` may import `requests` or `smtplib` except this module. That is the
part that makes the guarantee structural rather than remembered.

**A test that genuinely wants a transport replaces these functions** -
`monkeypatch.setattr(net, "post", fake)` - and wins, in the same way the
environment allowlist lets a test put its own credentials back. The seam is the
fake's insertion point *and* the guard's home, so bypassing the guard requires
saying so.

**Nothing here echoes a URL.** The Telegram bot token lives in the *path* of the
URL these functions are handed, and this module's whole job is to raise
exceptions that get logged. Refusals name the host and nothing else (G-41: keys
never appear in error messages or logs).
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

import requests


class NetworkRefused(RuntimeError):
    """Raised in place of opening a connection, because this is a test run."""


def under_test() -> str | None:
    """Why this looks like a test run, in words, or None.

    Two independent signals, either sufficient:

    `SENTINEL_ENV=test` is set by `tests/conftest.py` for every test in this
    suite, and can be set by hand by anyone running something they don't want
    reaching a real customer.

    `PYTEST_CURRENT_TEST` is set by pytest itself, per test. It covers what
    conftest cannot: a subprocess spawned from a test inherits it, and so does
    a suite in another repository that imports this package.
    """
    if "PYTEST_CURRENT_TEST" in os.environ:   # the one env read in guardian/: pytest's own signal
        return "running under pytest"
    try:
        from api.config import settings
        env = (settings.api_env or "").strip().lower()
    except Exception:  # noqa: BLE001 - the guard must never be the thing that fails
        env = ""
    if env in ("test", "testing"):
        return f"API_ENV={env}"
    return None


def refuse_if_under_test(what: str, target: str) -> None:
    """Raise rather than let `what` reach `target`, if this is a test run.

    `target` is for the human reading the traceback and must never be a whole
    URL - see the module docstring. Callers pass a host."""
    reason = under_test()
    if reason is not None:
        raise NetworkRefused(
            f"refusing to open {what} to {target}: {reason}. "
            "A test that wants a transport replaces the sentinel.net seam with "
            "a fake rather than reaching the network."
        )


def _host_of(url: str) -> str:
    """Host only. The rest of a URL carries credentials on at least one of our
    providers, and this string ends up in an exception message."""
    return urlsplit(url).netloc or "an unparseable url"


def post(url: str, **kwargs):
    """`requests.post`, refused under test."""
    refuse_if_under_test("an HTTP request", _host_of(url))
    return requests.post(url, **kwargs)


# Customer and operator email goes through the platform's Resend outbox
# (guardian/alerts/email.py), never SMTP from here.
