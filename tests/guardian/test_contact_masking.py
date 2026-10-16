"""A customer's email and phone never reach the worker log in full.

CLAUDE.md: customer emails are masked in anything that leaves the system, and
the worker log leaves it. The contact router logged the recipient lists
verbatim when a delivery failed, and ClickSend's per-message failure quoted
the number in the exception text that the same log line (and dispatch's) then
printed. Emails are masked with the platform's own helper (two characters and
the domain); phones show their last two digits.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from api.config import settings
from guardian.alerts.base import AlertMessage
from guardian.alerts.contact import ContactRouter
from guardian.alerts.sms import mask_phone
from guardian.detectors.base import Detection

EMAIL = "jane.doe@example.com"
EMAIL_2 = "second.person@example.org"
PHONE = "+15551112222"
PHONE_2 = "+447700900123"


def _detection():
    return Detection(rule="withdrawal_initiated", severity="CRITICAL", confidence="high",
                     message="0.5 BTC left the account", evidence={"order_id": "wd-1"})


class _Refusing:
    """A transport that fails every delivery."""

    def __init__(self, *args, **kwargs):
        pass

    def send(self, alert):
        raise RuntimeError("provider said no")


@pytest.fixture
def failing_router(monkeypatch):
    monkeypatch.setattr("guardian.alerts.contact.OutboxEmailChannel", _Refusing)
    monkeypatch.setattr("guardian.alerts.contact.build_sms_channel", lambda recipients: _Refusing())
    monkeypatch.setattr(settings, "resend_api_key", "re_test_not_a_real_key")
    monkeypatch.setattr(settings, "resend_from_email", "alerts@test")
    return ContactRouter()


def test_mask_phone_keeps_the_last_two_digits_only():
    assert mask_phone(PHONE) == "…22"
    assert mask_phone("+1 (555) 111-2222") == "…22"
    assert mask_phone("") == "…"
    assert mask_phone(None) == "…"


def test_a_failed_email_and_sms_are_logged_with_masked_contacts(failing_router, caplog):
    caplog.set_level(logging.WARNING, logger="guardian.alerts.contact")

    failing_router.send(_detection(), "acct", [EMAIL], [PHONE])

    log = caplog.text
    assert EMAIL not in log
    assert PHONE not in log and PHONE.lstrip("+") not in log
    assert "ja…@example.com" in log and "…22" in log
    assert log.count("failed") >= 2, "both failures are still reported"


def test_no_log_line_under_guardian_alerts_carries_a_full_contact(failing_router, caplog):
    """Every logger in the package, not only the two lines that were wrong:
    the assertion is on the whole captured log at DEBUG."""
    caplog.set_level(logging.DEBUG)

    failing_router.send(_detection(), "acct", [EMAIL, EMAIL_2], [PHONE, PHONE_2])

    for secret in (EMAIL, EMAIL_2, PHONE, PHONE_2, PHONE.lstrip("+"), PHONE_2.lstrip("+")):
        assert secret not in caplog.text, f"{secret} reached the log"


def test_clicksend_error_text_masks_the_number():
    """The provider's per-message failure quoted the recipient. That text goes
    to the worker log (contact.py, dispatch.py) and into sms_deliveries, so
    the number is masked where the text is written."""
    from guardian.alerts.clicksend import ClickSendSmsChannel

    alert = AlertMessage(account_label="acct", rule="withdrawal_initiated", severity="CRITICAL",
                         confidence="high", message="m", evidence={})
    with patch("guardian.net.post") as post:
        post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"response_code": "SUCCESS",
                          "data": {"messages": [{"status": "INVALID_RECIPIENT", "to": "+16477216549"}]}},
        )
        with pytest.raises(RuntimeError) as exc:
            ClickSendSmsChannel("u", "k", None, ["+16477216549"]).send(alert)

    assert "+16477216549" not in str(exc.value) and "6477216549" not in str(exc.value)
    assert "…49" in str(exc.value) and "INVALID_RECIPIENT" in str(exc.value)
