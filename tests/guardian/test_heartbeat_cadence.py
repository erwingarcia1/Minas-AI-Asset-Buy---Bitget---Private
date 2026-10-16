"""The heartbeat keeps its own cadence.

Redis holds a beat for HEARTBEAT_TTL_S (180 s) and the deploy check wants one
younger than 120 s. The pulse was the scheduler pass's one beat, so its
cadence was `polling.fast_loop_seconds` with no ceiling: raise that past the
TTL in rules.yaml - a legitimate edit - and /health called a working worker
stale. The scheduler now sleeps between passes in slices no longer than
HEARTBEAT_MAX_INTERVAL_S and beats between them; the poll cadence is
unchanged.
"""
from __future__ import annotations

import threading
import time

from guardian import engine as engine_module
from guardian.config import RulesConfig
from guardian.engine import HEARTBEAT_MAX_INTERVAL_S, Engine
from guardian.heartbeat import HEARTBEAT_TTL_S
from tests.guardian.conftest import RULES_YAML

NO_CHANNELS = {"CRITICAL": [], "HIGH": [], "MEDIUM": [], "LOW": []}


class RecordingHooks:
    def __init__(self, paused: bool = False):
        self.beats = []
        self._paused = paused

    def paused(self) -> bool:
        return self._paused

    def beat(self, snapshot: dict) -> None:
        self.beats.append(snapshot)

    def after_tick(self, paused: bool) -> None:
        pass


def test_the_cap_is_under_the_deploy_rule_and_the_ttl():
    assert HEARTBEAT_MAX_INTERVAL_S <= 60
    assert HEARTBEAT_MAX_INTERVAL_S < 120 < HEARTBEAT_TTL_S


def test_a_long_poll_interval_still_beats_every_slice(db, cipher, rules, monkeypatch):
    """A ten-minute interval, scaled down: the wait is five and a half slices
    long, so five beats land inside it."""
    hooks = RecordingHooks()
    engine = Engine(db, cipher, rules, NO_CHANNELS, hooks=hooks)
    monkeypatch.setattr(engine_module, "HEARTBEAT_MAX_INTERVAL_S", 0.02)

    engine._sleep_with_heartbeat(0.11)

    # Five expected; a slow box may oversleep a slice, never undersleep one.
    assert 3 <= len(hooks.beats) <= 6, f"{len(hooks.beats)} beats in a wait of 5.5 slices"
    assert all(beat["paused"] is False and "rules_valid" in beat for beat in hooks.beats)


def test_an_interval_within_one_slice_adds_no_beat(db, cipher, rules):
    """The pass's own beat is the pulse at the shipped 45 s; nothing changes
    for an interval the TTL already covers."""
    hooks = RecordingHooks()
    engine = Engine(db, cipher, rules, NO_CHANNELS, hooks=hooks)

    engine._sleep_with_heartbeat(0.05)

    assert hooks.beats == []


def test_the_beats_between_passes_carry_the_pause_state_the_pass_found(db, cipher, rules, monkeypatch):
    hooks = RecordingHooks(paused=True)
    engine = Engine(db, cipher, rules, NO_CHANNELS, hooks=hooks)
    engine._scheduler_tick()
    assert hooks.beats[0]["paused"] is True
    monkeypatch.setattr(engine_module, "HEARTBEAT_MAX_INTERVAL_S", 0.01)

    engine._sleep_with_heartbeat(0.05)

    assert len(hooks.beats) >= 3
    assert all(beat["paused"] is True for beat in hooks.beats)


def test_stop_ends_the_wait_at_once_with_no_beat_after_it(db, cipher, rules, monkeypatch):
    hooks = RecordingHooks()
    engine = Engine(db, cipher, rules, NO_CHANNELS, hooks=hooks)
    monkeypatch.setattr(engine_module, "HEARTBEAT_MAX_INTERVAL_S", 0.01)
    engine.stop()

    started = time.monotonic()
    engine._sleep_with_heartbeat(10)

    assert time.monotonic() - started < 1
    assert hooks.beats == []


def test_the_scheduler_loop_beats_while_waiting_for_a_long_interval(db, cipher, tmp_path, monkeypatch):
    """Wiring, not just capability: the loop itself, on a rules file whose
    poll interval is 600 s, must keep beating while it waits."""
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(RULES_YAML.replace('fast_loop_seconds: "45"', 'fast_loop_seconds: "600"'))
    rules = RulesConfig(rules_path)
    assert rules.get_seconds("polling", "fast_loop_seconds") == 600
    hooks = RecordingHooks()
    engine = Engine(db, cipher, rules, NO_CHANNELS, hooks=hooks)
    monkeypatch.setattr(engine_module, "HEARTBEAT_MAX_INTERVAL_S", 0.02)

    scheduler = threading.Thread(target=engine._scheduler_loop, daemon=True, name="scheduler")
    scheduler.start()
    time.sleep(0.3)
    engine.stop()
    scheduler.join(5)

    assert not scheduler.is_alive()
    assert len(hooks.beats) >= 4, f"{len(hooks.beats)} beats in 0.3 s with a 0.02 s slice: the loop is not slicing"
