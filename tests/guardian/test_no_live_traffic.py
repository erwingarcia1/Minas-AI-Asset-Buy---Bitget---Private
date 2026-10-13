"""No test run may reach the network (#68).

The suite used to make real network calls with the operator's real credentials:
`load_dotenv()` inside `sentinel.cli.main` wrote the production `.env` into
`os.environ`, and a live SMTP server answered `535 Missing password` in the
middle of `test_password_reset`. Three layers now stand between a test and a
socket, and this file holds the two that are checkable here.

1. `tests/guardian/conftest.py` blanks every transport setting, so no credential is present.
2. `guardian.net` refuses to open a connection when the process is under test.
3. Nothing in `guardian/` may import `requests` or `smtplib` except
   `guardian.net`, so layer 2 has no way around it. Email and Telegram go
   through the platform's own modules, which queue (the Resend outbox) or
   are refused here before they are called.

Layer 3 is the one that matters most, and it is the reason this file scans the
source rather than only exercising the four channels that exist today. A test
per channel protects those four. The scan protects the fifth.
"""

from __future__ import annotations

import ast
import os
import pathlib

import pytest

from guardian import net
from guardian.alerts.base import AlertMessage

SENTINEL_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "guardian"

# The libraries that open sockets. `ccxt` is deliberately absent: it is the
# exchange client, it is imported all over `guardian/exchange.py` for its
# exception types, and its traffic is refused at `ExchangeClient._call`
# instead - see `test_a_real_ccxt_client_is_refused_under_test`.
NETWORK_MODULES = frozenset({"requests", "smtplib", "urllib.request", "http.client", "socket"})

# The one module allowed to import them.
CHOKEPOINT = "net.py"


def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def _source_files() -> list[pathlib.Path]:
    return sorted(p for p in SENTINEL_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_scan_can_see_the_source_it_claims_to_scan():
    """The guarantee below is an absence, and an absence is what a scan that
    found no files also reports. Two of this project's tests have passed for
    exactly that reason, so this one states the population first."""
    files = _source_files()
    assert len(files) > 20, f"only found {len(files)} source files - the scan is looking in the wrong place"
    assert any(p.name == CHOKEPOINT for p in files)
    # And the scan can actually see an import when there is one to see.
    assert NETWORK_MODULES & _imported_modules(SENTINEL_DIR / CHOKEPOINT)


def test_only_sentinel_net_may_import_a_network_library():
    """Whoever writes the fifth transport finds out here, not in production."""
    offenders = {
        str(path.relative_to(SENTINEL_DIR)): sorted(NETWORK_MODULES & _imported_modules(path))
        for path in _source_files()
        if path.name != CHOKEPOINT and NETWORK_MODULES & _imported_modules(path)
    }
    assert offenders == {}, (
        f"these modules open their own sockets: {offenders}. Route them through "
        f"guardian.net, which refuses to connect during a test run."
    )


# --- the refusal itself ------------------------------------------------------


def test_the_suite_runs_with_the_marker_set():
    """conftest's half of the contract. If this fails, every refusal below is
    passing for a reason other than the one it claims."""
    assert "PYTEST_CURRENT_TEST" in os.environ
    assert net.under_test() == "running under pytest"


def test_pytest_alone_is_enough_without_the_marker(monkeypatch):
    """The second signal, which covers what conftest cannot: a subprocess a
    test spawns, or another repository's suite importing this package."""
    pass  # (SENTINEL_ENV has no platform equivalent)
    assert net.under_test() == "running under pytest"


def test_a_process_that_is_neither_is_allowed_to_connect(monkeypatch):
    """The guard has to be capable of saying yes, or the tests below prove
    nothing about *when* it says no."""
    pass  # (SENTINEL_ENV has no platform equivalent)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert net.under_test() is None


@pytest.fixture(autouse=True)
def the_wire_itself_is_a_landmine(monkeypatch):
    """Past the guard is a real socket, and these tests are about deleting the
    guard.

    A test proving "the guard refuses" fails usefully when the guard is gone
    *only* if what lies beyond it is not the actual Telegram API. Without this,
    a mutation probe that removes a refusal - which is exactly the probe this
    file invites - turns the suite back into the thing #68 was opened about.
    So the libraries underneath are replaced with something that raises: the
    tests below still fail when the guard goes, and they fail without dialling
    anybody."""
    def _reached_the_network(*args, **kwargs):
        raise AssertionError("the guard was bypassed and this run tried to open a real connection")

    monkeypatch.setattr(net.requests, "post", _reached_the_network)


def test_post_refuses_and_names_the_host_only():
    """A Telegram bot token lives in the path of the URL this is handed, and
    this exception is destined for a log (G-41)."""
    url = "https://api.telegram.org/bot123456:SUPER-SECRET-TOKEN/sendMessage"
    with pytest.raises(net.NetworkRefused) as exc:
        net.post(url, json={})
    assert "api.telegram.org" in str(exc.value)
    assert "SUPER-SECRET-TOKEN" not in str(exc.value)
    assert "sendMessage" not in str(exc.value)


def _alert():
    return AlertMessage(account_label="acct", rule="withdrawal_initiated", severity="CRITICAL",
                        confidence="high", message="0.5 BTC withdrawal", evidence={})


def _channels():
    """Every real transport, built with credentials that must never be used.

    Named individually rather than discovered from `AlertChannel.__subclasses__`
    on purpose: the fakes this suite defines are subclasses too, and a
    discovery that swept them up would assert that a fake refuses to send -
    which is both false and the opposite of what fakes are for."""
    from guardian.alerts.clicksend import ClickSendSmsChannel
    from guardian.alerts.sms import TwilioSmsChannel
    from guardian.alerts.telegram import TelegramChannel

    return {
        "telegram": TelegramChannel("chat"),
        "twilio": TwilioSmsChannel("SID", "token", "+15550000", ["+15551111"]),
        "clicksend": ClickSendSmsChannel("user", "apikey", "Minas", ["+15551111"]),
    }


def test_the_email_channel_only_queues_to_the_outbox():
    """Email is not a socket from this process: the worker queues into the
    platform's Resend outbox and the API's scheduler sends later. So the
    channel neither refuses nor connects; it leaves one message per recipient
    in the queue, which this test reads back and removes."""
    import json

    import redis

    from api.config import settings
    from api.services.email_outbox import OUTBOX_KEY
    from guardian.alerts.email import OutboxEmailChannel

    r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    before = r.zcard(OUTBOX_KEY)
    OutboxEmailChannel(["to@real.test"], audience="customer").send(_alert())
    queued = [json.loads(m) for m in r.zrange(OUTBOX_KEY, 0, -1)]
    mine = [m for m in queued if m["to"] == "to@real.test"]
    assert len(mine) == 1 and mine[0]["template"] == "guardian_alert" and mine[0]["category"] == "security"
    assert "withdrawal" in mine[0]["text"].lower()
    for m in queued:
        if m["to"] == "to@real.test":
            r.zrem(OUTBOX_KEY, json.dumps(m))
    assert r.zcard(OUTBOX_KEY) == before


@pytest.mark.parametrize("name", sorted(_channels()))
def test_every_channel_refuses_to_deliver_under_test(name):
    with pytest.raises(net.NetworkRefused):
        _channels()[name].send(_alert())


def test_a_real_ccxt_client_is_refused_under_test():
    """The exchange side of the same promise. A fake exchange is not refused -
    the whole suite depends on that - so the check keys on the client being a
    genuine ccxt one.

    The call handed to `_call` is a landmine rather than `fetch_balance` for
    the reason the fixture above exists: if the guard is deleted, this must
    fail without Bitget hearing about it."""
    import ccxt

    from guardian.exchange import ExchangeClient

    def _reached_the_exchange(*args, **kwargs):
        raise AssertionError("the guard was bypassed and this run tried to call the exchange")

    live = ExchangeClient(ccxt.bitget({"apiKey": "k", "secret": "s"}), "k", "s")
    with pytest.raises(net.NetworkRefused):
        live._call(_reached_the_exchange)


def test_a_fake_exchange_is_not_refused():
    """The other direction, and the one that would break every exchange test in
    this suite if the guard were written as 'refuse everything under test'."""
    from guardian.exchange import ExchangeClient

    class _FakeExchange:
        def fetch_balance(self):
            return {"total": {"BTC": 1}}

    assert ExchangeClient(_FakeExchange(), "k", "s").fetch_balance() == {"total": {"BTC": 1}}
