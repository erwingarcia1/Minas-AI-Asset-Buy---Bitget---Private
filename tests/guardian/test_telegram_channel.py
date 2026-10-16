"""Guardian's Telegram adapter keeps the platform sender's contract.

api/services/telegram.py posts to the same chats and promises that no message
carries a full wallet address or a full email (addresses show their first 6
and last 4 characters, emails two characters and the domain). The adapter
masked emails only; a withdrawal alert's message and evidence both carry the
destination address, so every one of them left in full.

The second half is the worker calling `send_sync` from a thread pool. The
sender's rate limiter is module state with a check-then-append around the HTTP
call, so threads that are not taking turns can all pass the check and overshoot
the per-minute cap. The API process is async and single-threaded; only the sync
entry point needed the lock.
"""
from __future__ import annotations

import json
import threading
import time

import httpx

from api.config import settings
from api.services import telegram
from guardian.alerts.base import AlertMessage
from guardian.alerts.telegram import TelegramChannel, redact

BTC = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
ETH = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
TRON = "TJRyWwFs9wTFGZg3JbrVriFbNfCug5tDeC"


def _alert(message: str, evidence: dict) -> AlertMessage:
    return AlertMessage(account_label="Main (spot)", rule="withdrawal_to_unseen_address",
                        severity="CRITICAL", confidence="high", message=message, evidence=evidence)


# --- addresses ---------------------------------------------------------------


def test_an_address_in_the_evidence_is_masked_wherever_it_appears():
    """The detector writes the same address into the free-text message and the
    evidence, so masking the evidence line alone would leave it in the line
    above."""
    alert = _alert(f"Withdrawal to address never seen before in this account's history: {BTC}",
                   {"order_id": "wd-1", "address": BTC, "coin": "BTC", "amount": "0.5"})

    text = redact(alert.render(), alert.evidence)

    assert BTC not in text
    assert text.count(telegram.mask_address(BTC)) == 2, "the message line and the evidence line"
    assert telegram.mask_address(BTC) == "bc1qw5…f3t4"
    assert "0.5" in text and "wd-1" in text, "amounts and references stay: the operator needs them"


def test_rule_names_and_copy_survive_the_masking():
    """The platform's own long-token scrub would eat `withdrawal_to_unseen_address`
    (underscores count there). Ours masks runs of letters and digits only, with
    at least one digit, which is what an address looks like and our copy never
    does."""
    alert = _alert(f"Withdrawal to address never seen before: {BTC}", {"address": BTC})

    text = redact(alert.render(), alert.evidence)

    assert "withdrawal_to_unseen_address" in text
    assert "Main (spot)" in text
    assert "Minas AI Guardian key" in text, "the runbook is intact"


def test_text_with_no_evidence_is_masked_by_shape():
    """The daily digest is one body built from stored messages with an empty
    evidence dict, so there is nothing to match by value; the address formats
    themselves have to be recognised."""
    body = (f"Guardian daily digest - 2 MEDIUM/LOW event(s) in the last 24h\n"
            f"  [MEDIUM] transfer_route_never_seen @ 2026-10-16T08:00: route funding->spot\n"
            f"  [LOW] baseline_drift_new_deposit_source @ 2026-10-16T08:00: {ETH} and {TRON}")

    text = redact(body, {})

    assert ETH not in text and TRON not in text
    assert telegram.mask_address(ETH) in text and telegram.mask_address(TRON) in text
    assert "transfer_route_never_seen" in text and "baseline_drift_new_deposit_source" in text
    assert "2026-10-16T08:00" in text


def test_a_list_of_addresses_in_the_evidence_is_masked():
    """`detect_baseline_drift` reports `addresses: [...]` (plural key, list value)."""
    alert = _alert("2 new deposit source address(es) added to baseline", {"addresses": [BTC, ETH]})

    text = redact(alert.render(), alert.evidence)

    assert BTC not in text and ETH not in text
    assert telegram.mask_address(BTC) in text and telegram.mask_address(ETH) in text


def test_a_nested_address_is_masked():
    """`_report_unidentifiable` nests an `example` record carrying `address`."""
    alert = _alert("bitget sent 2 withdrawal(s) with neither a reference nor a timestamp",
                   {"count": 2, "example": {"currency": "BTC", "amount": "0.1", "address": BTC, "network": "BTC"}})

    text = redact(alert.render(), alert.evidence)

    assert BTC not in text and telegram.mask_address(BTC) in text


def test_emails_are_still_masked():
    text = redact("escalation to jane.doe@example.com failed", {})
    assert "jane.doe@example.com" not in text
    assert "ja…@example.com" in text


def test_the_channel_sends_the_redacted_text(monkeypatch):
    """Wiring: what reaches `send_sync` is the redacted text. The network guard
    and the sender are both replaced, so nothing leaves."""
    from guardian import net
    from guardian.alerts import telegram as adapter

    sent = []
    monkeypatch.setattr(net, "refuse_if_under_test", lambda what, target: None)
    monkeypatch.setattr(telegram, "send_sync",
                        lambda text, key=None, *, chat_id=None: sent.append((text, chat_id)) or telegram.SendResult(True))
    alert = _alert(f"Withdrawal to address never seen before: {BTC}", {"address": BTC})

    adapter.TelegramChannel("team-chat").send(alert)

    (text, chat_id), = sent
    assert BTC not in text and telegram.mask_address(BTC) in text
    assert chat_id == "team-chat"


def test_a_refused_send_still_raises_so_dispatch_counts_it_undelivered(monkeypatch):
    from guardian import net

    monkeypatch.setattr(net, "refuse_if_under_test", lambda what, target: None)
    monkeypatch.setattr(telegram, "send_sync",
                        lambda text, key=None, *, chat_id=None: telegram.SendResult(False, "HTTP 400: bad"))

    try:
        TelegramChannel().send(_alert("m", {}))
    except RuntimeError as exc:
        assert "HTTP 400" in str(exc)
    else:
        raise AssertionError("a failed send must raise, or dispatch records it as delivered")


# --- the rate limiter under threads ------------------------------------------


class _FakeBotApi:
    """Answers every sendMessage with 200 and counts them, thread-safely.

    The 20 ms pause stands in for the HTTP round trip. A real call releases
    the GIL while it waits, which is when the other threads run their
    rate-limit checks; an instantaneous fake would let the GIL serialise the
    calls by accident and hide the race this test is about."""

    def __init__(self):
        self.requests = []
        self._lock = threading.Lock()

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(json.loads(request.content)["text"])
        time.sleep(0.02)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


def test_threads_calling_send_sync_never_exceed_the_per_minute_cap(monkeypatch):
    """Twenty worker threads, a cap of five. The fake transport is the proof
    nothing left: every request it saw is counted, and the count is the cap."""
    fake = _FakeBotApi()
    monkeypatch.setattr(telegram, "transport_factory", lambda: httpx.MockTransport(fake.handler))
    monkeypatch.setattr(settings, "telegram_bot_token", "123456789:AAH-not-a-real-token-XYZ")
    monkeypatch.setattr(settings, "telegram_chat_id", "-1001234567890")
    monkeypatch.setattr(settings, "telegram_max_per_minute", 5)
    telegram.reset_state()
    try:
        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(20)

        def worker(i: int) -> None:
            barrier.wait()
            result = telegram.send_sync(f"message {i}", None)
            with results_lock:
                results.append(result)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(results) == 20, "every call returned (send_sync never raises)"
        assert len(fake.requests) == 5, f"the cap is 5 per minute; {len(fake.requests)} requests reached the bot API"
        assert sum(1 for r in results if r.ok) == 5
        assert sum(1 for r in results if r.error == "rate limited") == 15
        assert len(telegram._sent_at) == 5
    finally:
        telegram.reset_state()
