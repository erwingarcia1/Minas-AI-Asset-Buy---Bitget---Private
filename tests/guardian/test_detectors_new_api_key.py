from __future__ import annotations

from guardian.detectors.new_api_key import detect_permission_state_change


def test_first_observation_does_not_trigger():
    detections = detect_permission_state_change(None, {"authorities": "a", "ip_whitelist": "1.2.3.4"})
    assert detections == []


def test_unchanged_state_does_not_trigger():
    prev = {"authorities": "a", "ip_whitelist": "1.2.3.4"}
    curr = {"authorities": "a", "ip_whitelist": "1.2.3.4"}
    assert detect_permission_state_change(prev, curr) == []


def test_changed_authorities_triggers_proxy_alert():
    prev = {"authorities": "spot_account_r", "ip_whitelist": "1.2.3.4"}
    curr = {"authorities": "spot_account_r,spot_trade_w", "ip_whitelist": "1.2.3.4"}
    detections = detect_permission_state_change(prev, curr)
    assert len(detections) == 1
    assert detections[0].rule == "account_permission_state_changed"
    assert "proxy" in detections[0].confidence
