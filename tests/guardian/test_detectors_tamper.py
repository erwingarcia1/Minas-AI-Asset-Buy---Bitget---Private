from __future__ import annotations

from guardian.detectors.tamper import detect_tamper


def test_threshold_crossed_triggers_high():
    detections = detect_tamper(consecutive_auth_failures=2, threshold=2)
    assert len(detections) == 1
    assert detections[0].rule == "monitoring_key_tamper_suspected"
    assert detections[0].severity == "HIGH"


def test_below_threshold_triggers_nothing():
    assert detect_tamper(consecutive_auth_failures=1, threshold=2) == []
