"""The email outbox never writes a customer address in clear to the log (repository rule on
customer data in logs); the synchronous entry point the Guardian worker uses is covered here."""
import logging

from api.config import settings
from api.services import email_outbox


def test_enqueue_sync_masks_the_address_when_the_outbox_is_unavailable(monkeypatch, caplog):
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:9/1")   # nothing listens on port 9
    with caplog.at_level(logging.ERROR):
        ok = email_outbox.enqueue_sync("alice.customer@example.com", "Subject", "<p>x</p>", "x", template="two_factor")
    assert not ok
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "not queued" in text
    assert "alice.customer@example.com" not in text
    assert "al…@example.com" in text
