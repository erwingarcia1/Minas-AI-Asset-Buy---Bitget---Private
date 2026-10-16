from __future__ import annotations

from guardian.detectors.withdrawal import detect_new_withdrawals
from tests.guardian.fixtures.ccxt_responses import withdrawal_record


def test_new_withdrawal_triggers_critical():
    detections = detect_new_withdrawals([withdrawal_record(status="pending")])
    assert len(detections) == 1
    assert detections[0].rule == "withdrawal_initiated"
    assert detections[0].severity == "CRITICAL"


def test_no_new_withdrawals_triggers_nothing():
    assert detect_new_withdrawals([]) == []
