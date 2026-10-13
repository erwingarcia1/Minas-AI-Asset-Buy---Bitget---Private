from __future__ import annotations

from guardian.detectors.unseen_address import detect_unseen_address
from tests.guardian.fixtures.ccxt_responses import withdrawal_record


def test_unseen_address_triggers_critical():
    record = withdrawal_record(address="bc1qneveraddress")
    detections = detect_unseen_address([record], known_addresses={"bc1qexistingaddress"})
    assert len(detections) == 1
    assert detections[0].rule == "withdrawal_to_unseen_address"
    assert detections[0].severity == "CRITICAL"


def test_known_address_triggers_nothing():
    record = withdrawal_record(address="bc1qexistingaddress")
    detections = detect_unseen_address([record], known_addresses={"bc1qexistingaddress"})
    assert detections == []


def test_missing_address_degrades_instead_of_silently_passing():
    record = withdrawal_record()
    record["address"] = None
    detections = detect_unseen_address([record], known_addresses=set())
    assert len(detections) == 1
    assert detections[0].rule == "unseen_address_check_degraded"
    assert detections[0].severity == "LOW"
