from __future__ import annotations

from guardian.alerts.dispatch import dispatch
from guardian.detectors.base import Detection
from tests.guardian.conftest import make_account


class RecordingChannel:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


def test_alert_is_sent_once_and_deduped_within_cooldown(db):
    channel = RecordingChannel()
    channels = {"CRITICAL": [channel]}
    account_id = make_account(db)
    detection = Detection(
        rule="withdrawal_initiated", severity="CRITICAL", confidence="high", message="m",
        evidence={"order_id": "wd-1"},
    )

    sent_first = dispatch(db, channels, account_id, "a", detection, {"CRITICAL": 15})
    sent_second = dispatch(db, channels, account_id, "a", detection, {"CRITICAL": 15})

    # dispatch returns the stored alert's row id so delivery logs can link to it.
    assert isinstance(sent_first, int)
    assert sent_second is None
    assert len(channel.sent) == 1


def test_different_evidence_is_not_deduped_together(db):
    channel = RecordingChannel()
    channels = {"CRITICAL": [channel]}
    account_id = make_account(db)
    d1 = Detection(
        rule="withdrawal_initiated", severity="CRITICAL", confidence="high", message="m",
        evidence={"order_id": "wd-1"},
    )
    d2 = Detection(
        rule="withdrawal_initiated", severity="CRITICAL", confidence="high", message="m",
        evidence={"order_id": "wd-2"},
    )

    dispatch(db, channels, account_id, "a", d1, {"CRITICAL": 15})
    sent = dispatch(db, channels, account_id, "a", d2, {"CRITICAL": 15})

    assert isinstance(sent, int)
    assert len(channel.sent) == 2
