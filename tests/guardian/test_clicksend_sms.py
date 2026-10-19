from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from guardian.alerts.base import AlertMessage
from guardian.alerts.clicksend import ClickSendSmsChannel
from guardian.alerts.sms import TwilioSmsChannel, build_sms_channel
from api.config import settings


def _alert():
    return AlertMessage(account_label="acct", rule="withdrawal_initiated", severity="CRITICAL",
                        confidence="high", message="0.5 BTC withdrawal", evidence={})


def test_clicksend_posts_expected_payload():
    with patch("guardian.net.post") as post:
        post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"response_code": "SUCCESS", "data": {"messages": [{"status": "SUCCESS"}]}},
        )
        ClickSendSmsChannel("user", "apikey", "MinasGuard", ["+16477216549"]).send(_alert())
        _, kwargs = post.call_args
        assert kwargs["auth"] == ("user", "apikey")
        assert kwargs["json"]["messages"][0]["to"] == "+16477216549"
        assert kwargs["json"]["messages"][0]["from"] == "MinasGuard"
        assert "Minas AI Guardian" in kwargs["json"]["messages"][0]["body"]
        assert kwargs["headers"]["Content-Type"] == "application/json"


def test_clicksend_raises_on_top_level_failure():
    with patch("guardian.net.post") as post:
        post.return_value = MagicMock(
            status_code=200, json=lambda: {"response_code": "ERROR", "response_msg": "bad auth"}
        )
        with pytest.raises(RuntimeError):
            ClickSendSmsChannel("u", "k", None, ["+1555"]).send(_alert())


def test_clicksend_raises_on_per_message_failure():
    with patch("guardian.net.post") as post:
        post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"response_code": "SUCCESS", "data": {"messages": [{"status": "INVALID_RECIPIENT"}]}},
        )
        with pytest.raises(RuntimeError):
            ClickSendSmsChannel("u", "k", None, ["+1555"]).send(_alert())


def test_factory_prefers_clicksend(monkeypatch):
    monkeypatch.setattr(settings, "clicksend_username", "u")
    monkeypatch.setattr(settings, "clicksend_api_key", "k")
    monkeypatch.setattr(settings, "twilio_account_sid", "sid")
    monkeypatch.setattr(settings, "twilio_auth_token", "tok")
    monkeypatch.setattr(settings, "twilio_from_number", "+1")
    assert isinstance(build_sms_channel(["+1555"]), ClickSendSmsChannel)


def test_factory_falls_back_to_twilio(monkeypatch):
    monkeypatch.setattr(settings, "clicksend_username", "")
    monkeypatch.setattr(settings, "clicksend_api_key", "")
    monkeypatch.setattr(settings, "twilio_account_sid", "sid")
    monkeypatch.setattr(settings, "twilio_auth_token", "tok")
    monkeypatch.setattr(settings, "twilio_from_number", "+1")
    assert isinstance(build_sms_channel(["+1555"]), TwilioSmsChannel)


CLICKSEND = ("clicksend_username", "clicksend_api_key")
TWILIO = ("twilio_account_sid", "twilio_auth_token", "twilio_from_number")


def test_factory_none_when_unconfigured(monkeypatch):
    for attr in CLICKSEND + TWILIO:
        monkeypatch.setattr(settings, attr, "")
    assert build_sms_channel(["+1555"]) is None
    assert build_sms_channel([]) is None


# --- half-configured providers (#70) ------------------------------------------
#
# Three surviving mutants lived here: `cs_user and cs_key` degrading to `or`,
# and either half of `sid and token and frm` degrading the same way. Nothing in
# the suite configured a provider half way, so nothing noticed. The code was
# right; there was simply nothing holding it there.
#
# The factory reads api.config.settings, never os.environ: a monkeypatch.setenv
# here reaches nothing (the first port of these tests did that, and passed with
# every credential blank). The provider is configured on the settings object.


@pytest.mark.parametrize("present,missing", [
    ("clicksend_username", "clicksend_api_key"),
    ("clicksend_api_key", "clicksend_username"),
])
def test_half_a_clicksend_credential_is_not_a_provider(monkeypatch, present, missing):
    for attr in TWILIO:
        monkeypatch.setattr(settings, attr, "")
    monkeypatch.setattr(settings, missing, "")
    monkeypatch.setattr(settings, present, "half")

    assert build_sms_channel(["+1555"]) is None


def test_a_half_configured_clicksend_does_not_shadow_a_working_twilio(monkeypatch):
    """The one that costs a CRITICAL page.

    ClickSend is *preferred* when its credentials are present, so "present"
    degrading from both-of-two to either-of-two means a stray
    CLICKSEND_USERNAME with no API key selects ClickSend - which then shadows a
    fully working Twilio fallback and fails at delivery time. SMS is
    CRITICAL-only in the default routing, so what does not arrive is the page
    that matters most, and the only trace is a delivery failure after the
    fact."""
    monkeypatch.setattr(settings, "clicksend_username", "u")
    monkeypatch.setattr(settings, "clicksend_api_key", "")
    monkeypatch.setattr(settings, "twilio_account_sid", "sid")
    monkeypatch.setattr(settings, "twilio_auth_token", "tok")
    monkeypatch.setattr(settings, "twilio_from_number", "+1")
    assert isinstance(build_sms_channel(["+1555"]), TwilioSmsChannel)


@pytest.mark.parametrize("missing", list(TWILIO))
def test_twilio_needs_all_three_credentials(monkeypatch, missing):
    """A lone TWILIO_FROM_NUMBER used to be enough to construct a channel with
    None for the SID and the token - a provider that exists, is chosen, and
    cannot send."""
    for attr in CLICKSEND:
        monkeypatch.setattr(settings, attr, "")
    for attr in TWILIO:
        monkeypatch.setattr(settings, attr, "set")
    monkeypatch.setattr(settings, missing, "")

    assert build_sms_channel(["+1555"]) is None
