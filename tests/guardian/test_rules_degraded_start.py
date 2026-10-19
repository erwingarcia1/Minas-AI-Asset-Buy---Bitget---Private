"""Starting on a rejected config, rather than refusing to start.

I originally made an invalid config at startup raise, on the grounds that
"coming up with thresholds nobody has checked looks exactly like monitoring".
That reasoning was incomplete in a way that matters at exactly the wrong
moment.

The case it missed is an **upgrade**. A file the old code ran happily on can be
rejected by a schema that shipped after it. Refusing to start then converts a
working monitor into a dead one, during a deploy, on a service whose entire
purpose is to be watching. Dying loudly and not monitoring is still not
monitoring - and the customer whose account is being drained is not comforted
by the clarity of the log line.

Starting on defaults is strictly better on coverage: some detection beats none,
in every case. What made it the wrong trade *before* was that the only signal
was a log line nobody reads. That changed when G-49 (#53) landed an operator
paging route. The decision is revisited because its binding constraint went
away, not because the risk did.

So: never refuse. Use every setting in the file that validates, fall back to
defaults for the ones that don't, and raise a CRITICAL operator page saying so.
`python -m guardian.validate_config` is where fail-fast lives now - before the deploy,
not during it.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from guardian.config import RulesConfig
from guardian.engine import Engine
from api.config import settings


def _write(path, text, mtime):
    import os

    path.write_text(text)
    os.utime(path, (mtime, mtime))


class Recording:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


# --- it starts ---------------------------------------------------------------

def test_an_invalid_config_at_startup_does_not_refuse_to_start(tmp_path):
    """The whole point. This used to raise."""
    path = tmp_path / "rules.yaml"
    _write(path, 'detectors:\n  off_market:\n    max_deviation_pct: "ten"\n', 1_000_000)

    rules = RulesConfig(path)  # must not raise

    assert rules.is_valid is False
    assert "max_deviation_pct" in rules.validation_error


def test_malformed_yaml_at_startup_does_not_refuse_to_start(tmp_path):
    path = tmp_path / "rules.yaml"
    _write(path, "detectors:\n  off_market:\n   max_deviation_pct: [unclosed\n", 1_000_000)

    rules = RulesConfig(path)

    assert rules.is_valid is False
    assert rules.get_decimal("detectors", "off_market", "max_deviation_pct", default=Decimal("10")) == 10


def test_a_missing_config_file_does_not_refuse_to_start(tmp_path):
    rules = RulesConfig(tmp_path / "absent.yaml")

    assert rules.is_valid is False
    assert rules.get_seconds("polling", "fast_loop_seconds", default=45) == 45


# --- only the broken settings fall back --------------------------------------

def test_the_settings_that_validate_are_still_used(tmp_path):
    """All-or-nothing would throw away every good setting because of one typo,
    which is its own kind of surprise: an operator who mistyped `triage.
    max_events` would silently lose their detector tuning too."""
    path = tmp_path / "rules.yaml"
    _write(
        path,
        "polling:\n"
        '  fast_loop_seconds: "20"\n'
        "detectors:\n"
        "  off_market:\n"
        '    max_deviation_pct: "ten"\n'
        "  fee_anomaly:\n"
        '    multiplier: "7"\n',
        1_000_000,
    )

    rules = RulesConfig(path)

    assert rules.get_seconds("polling", "fast_loop_seconds", default=45) == 20
    assert rules.get_decimal("detectors", "fee_anomaly", "multiplier", default=Decimal("5")) == 7
    # ...and only the broken one falls back
    assert rules.get_decimal("detectors", "off_market", "max_deviation_pct", default=Decimal("10")) == 10


def test_an_unknown_key_is_dropped_without_taking_its_neighbours(tmp_path):
    path = tmp_path / "rules.yaml"
    _write(
        path,
        "detectors:\n"
        "  off_market:\n"
        '    max_deviaton_pct: "25"\n'   # misspelled
        "  fee_anomaly:\n"
        '    multiplier: "7"\n',
        1_000_000,
    )

    rules = RulesConfig(path)

    assert rules.get_decimal("detectors", "fee_anomaly", "multiplier", default=Decimal("5")) == 7
    assert rules.get_decimal("detectors", "off_market", "max_deviation_pct", default=Decimal("10")) == 10


def test_a_later_valid_edit_takes_over_completely(tmp_path):
    """Recovery has to be total, not a merge of the degraded state with the
    fixed file."""
    path = tmp_path / "rules.yaml"
    _write(path, 'detectors:\n  off_market:\n    max_deviation_pct: "ten"\n', 1_000_000)
    rules = RulesConfig(path)
    assert rules.is_valid is False

    _write(path, 'detectors:\n  off_market:\n    max_deviation_pct: "25"\n', 2_000_000)

    assert rules.get_decimal("detectors", "off_market", "max_deviation_pct") == 25
    assert rules.is_valid is True


def test_a_known_good_config_is_still_preferred_over_a_pruned_one(tmp_path):
    """The running case is unchanged and must stay that way: once a valid
    configuration has been seen, it beats a pruned version of a broken file,
    because it is both valid *and* what the operator actually asked for."""
    path = tmp_path / "rules.yaml"
    _write(path, 'detectors:\n  off_market:\n    max_deviation_pct: "25"\n', 1_000_000)
    rules = RulesConfig(path)
    assert rules.get_decimal("detectors", "off_market", "max_deviation_pct") == 25

    _write(path, 'detectors:\n  off_market:\n    max_deviation_pct: "ten"\n', 2_000_000)

    assert rules.get_decimal("detectors", "off_market", "max_deviation_pct") == 25, (
        "fell back to a default when a known-good value was available"
    )


# --- alerts still have somewhere to go ---------------------------------------

def test_alert_routing_falls_back_to_channels_rather_than_to_nothing(tmp_path, monkeypatch):
    """The trap in "start with defaults".

    `build_channels` reads routing with `default=[]`, so a pruned or missing
    routing table means *no channels for that severity* - alerts stored, dedup
    applied, and nothing delivered. That is a worse failure than the one this
    change is fixing, and it is silent. A CRITICAL with nowhere to go must not
    be a thing an invalid config can produce."""
    from guardian.channels import build_channels

    monkeypatch.setattr(settings, "telegram_bot_token", "t")
    monkeypatch.setattr(settings, "telegram_chat_id", "1")
    path = tmp_path / "rules.yaml"
    _write(path, "alerting:\n  routing:\n    CRITICAL: not-a-list\n", 1_000_000)
    rules = RulesConfig(path)

    channels = build_channels(rules)

    assert channels["CRITICAL"], "a CRITICAL alert had nowhere to go"


def test_routing_absent_entirely_still_reaches_a_channel(tmp_path, monkeypatch):
    from guardian.channels import build_channels

    monkeypatch.setattr(settings, "telegram_bot_token", "t")
    monkeypatch.setattr(settings, "telegram_chat_id", "1")
    path = tmp_path / "rules.yaml"
    _write(path, "polling:\n  fast_loop_seconds: \"45\"\n", 1_000_000)
    rules = RulesConfig(path)

    assert build_channels(rules)["CRITICAL"], "a CRITICAL alert had nowhere to go"


# --- and it is loud ----------------------------------------------------------

def test_running_on_a_rejected_config_pages_an_operator(db, cipher, tmp_path):
    """The page is what makes starting-anyway defensible. Without it this is
    just running quietly on the wrong thresholds."""
    from guardian.alerts.operator import OperatorAlerter, recent_operator_pages

    path = tmp_path / "rules.yaml"
    _write(path, 'detectors:\n  off_market:\n    max_deviation_pct: "ten"\n', 1_000_000)
    rules = RulesConfig(path)

    channel = Recording()
    alerter = OperatorAlerter(db, {"CRITICAL": [channel]}, rules)
    engine = Engine(db, cipher, rules, {}, operator_alerter=alerter)

    engine._check_config_health()

    pages = recent_operator_pages(db)
    assert len(pages) == 1
    assert pages[0]["severity"] == "CRITICAL"
    assert "max_deviation_pct" in pages[0]["message"]
    assert len(channel.sent) == 1


def test_a_healthy_config_pages_nobody(db, cipher, tmp_path):
    from guardian.alerts.operator import OperatorAlerter, recent_operator_pages

    path = tmp_path / "rules.yaml"
    _write(path, 'detectors:\n  off_market:\n    max_deviation_pct: "25"\n', 1_000_000)
    rules = RulesConfig(path)

    engine = Engine(
        db, cipher, rules, {},
        operator_alerter=OperatorAlerter(db, {"CRITICAL": [Recording()]}, rules),
    )
    for _ in range(5):
        engine._check_config_health()

    assert recent_operator_pages(db) == []


def test_one_rejection_pages_once_not_every_cycle(db, cipher, tmp_path):
    """The scheduler calls this every fast loop. Paging per cycle would bury
    the page it is trying to deliver."""
    from guardian.alerts.operator import OperatorAlerter, recent_operator_pages

    path = tmp_path / "rules.yaml"
    _write(path, 'detectors:\n  off_market:\n    max_deviation_pct: "ten"\n', 1_000_000)
    rules = RulesConfig(path)

    engine = Engine(
        db, cipher, rules, {},
        operator_alerter=OperatorAlerter(db, {"CRITICAL": [Recording()]}, rules),
    )
    for _ in range(10):
        engine._check_config_health()

    assert len(recent_operator_pages(db)) == 1


def test_a_new_rejection_pages_again(db, cipher, tmp_path):
    """A second, different mistake is news - it must not be suppressed as a
    repeat of the first, or an operator fixing one problem and introducing
    another hears nothing."""
    from guardian.alerts.operator import OperatorAlerter, recent_operator_pages

    path = tmp_path / "rules.yaml"
    _write(path, 'detectors:\n  off_market:\n    max_deviation_pct: "ten"\n', 1_000_000)
    rules = RulesConfig(path)

    engine = Engine(
        db, cipher, rules, {},
        operator_alerter=OperatorAlerter(db, {"CRITICAL": [Recording()]}, rules),
    )
    engine._check_config_health()

    _write(path, 'detectors:\n  fee_anomaly:\n    multiplier: "nope"\n', 2_000_000)
    engine._check_config_health()

    pages = recent_operator_pages(db)
    assert len(pages) == 2
    assert "multiplier" in pages[0]["message"]


def test_the_scheduler_actually_runs_the_config_health_check(db, cipher, tmp_path):
    """Wiring, not just capability. An alarm that exists and is never called is
    the same as no alarm - and asserting `_check_config_health` in isolation
    would not have caught it being dropped from the loop."""
    from guardian.alerts.operator import OperatorAlerter, recent_operator_pages

    path = tmp_path / "rules.yaml"
    _write(path, 'detectors:\n  off_market:\n    max_deviation_pct: "ten"\n', 1_000_000)
    rules = RulesConfig(path)

    engine = Engine(
        db, cipher, rules, {},
        operator_alerter=OperatorAlerter(db, {"CRITICAL": [Recording()]}, rules),
    )
    engine._scheduler_tick()

    assert len(recent_operator_pages(db)) == 1


def test_a_failing_health_check_does_not_kill_the_scheduler(db, cipher, tmp_path):
    """The metrics and health calls run on the scheduler thread every cycle. An
    exception there would stop all polling - the loudest possible failure
    expressed as total silence."""
    path = tmp_path / "rules.yaml"
    _write(path, 'detectors:\n  off_market:\n    max_deviation_pct: "25"\n', 1_000_000)
    rules = RulesConfig(path)

    class Exploding:
        def page(self, **kwargs):
            raise RuntimeError("paging is down")

        rejected_at = "x"

    engine = Engine(db, cipher, rules, {}, operator_alerter=Exploding())
    engine._rules = rules
    engine._paged_config_rejection = None

    def boom():
        raise RuntimeError("health check exploded")

    engine._check_config_health = boom
    engine._scheduler_tick()  # must not raise


# --- a file that cannot be read is one problem, not one per pass -------------

VALID = 'detectors:\n  off_market:\n    max_deviation_pct: "25"\n'


def test_an_unreadable_file_pages_once_not_every_pass(db, cipher, tmp_path):
    """A missing or unreadable file has no mtime to latch on, so every reload -
    and every threshold read is one - rejected it afresh with a new
    `rejected_at`. The health check keys its page on that stamp, so the worker
    paged CRITICAL, operator SMS included, every scheduler pass for as long as
    the file was gone."""
    from guardian.alerts.operator import OperatorAlerter, recent_operator_pages

    path = tmp_path / "rules.yaml"
    _write(path, VALID, 1_000_000)
    rules = RulesConfig(path)
    channel = Recording()
    engine = Engine(db, cipher, rules, {},
                    operator_alerter=OperatorAlerter(db, {"CRITICAL": [channel]}, rules))

    path.unlink()
    for _ in range(10):
        rules.get_decimal("detectors", "off_market", "max_deviation_pct")  # the reads a poll makes
        engine._scheduler_tick()

    pages = recent_operator_pages(db)
    assert len(pages) == 1 and len(channel.sent) == 1
    assert "cannot be read" in pages[0]["message"]
    assert rules.get_decimal("detectors", "off_market", "max_deviation_pct") == 25, (
        "the last valid configuration stays in force"
    )


def test_an_unreadable_file_is_logged_once_and_so_is_its_return(tmp_path, caplog):
    path = tmp_path / "rules.yaml"
    _write(path, VALID, 1_000_000)
    rules = RulesConfig(path)
    caplog.set_level(logging.INFO, logger="guardian.config")

    path.unlink()
    for _ in range(20):
        assert rules.is_valid is False
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1, (
        "one ERROR on the transition, then quiet while it stays broken"
    )

    _write(path, VALID.replace('"25"', '"30"'), 2_000_000)
    for _ in range(20):
        assert rules.is_valid is True
    assert rules.get_decimal("detectors", "off_market", "max_deviation_pct") == 30, (
        "recovery is the file on disk, not the values kept while it was gone"
    )
    assert len([r for r in caplog.records if "valid again" in r.getMessage()]) == 1
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1


def test_a_file_that_comes_back_broken_is_a_new_problem(db, cipher, tmp_path):
    """Latching the unreadable state must not swallow the next, different
    mistake: a file restored with a typo in it is news."""
    from guardian.alerts.operator import OperatorAlerter, recent_operator_pages

    path = tmp_path / "rules.yaml"
    _write(path, VALID, 1_000_000)
    rules = RulesConfig(path)
    engine = Engine(db, cipher, rules, {},
                    operator_alerter=OperatorAlerter(db, {"CRITICAL": [Recording()]}, rules))

    path.unlink()
    engine._check_config_health()
    _write(path, 'detectors:\n  off_market:\n    max_deviation_pct: "ten"\n', 2_000_000)
    engine._check_config_health()
    engine._check_config_health()

    pages = recent_operator_pages(db)
    assert len(pages) == 2
    assert "max_deviation_pct" in pages[0]["message"] and "cannot be read" in pages[1]["message"]


def test_a_file_missing_at_startup_is_picked_up_when_it_appears(tmp_path):
    path = tmp_path / "rules.yaml"
    rules = RulesConfig(path)
    assert rules.is_valid is False

    _write(path, VALID, 1_000_000)

    assert rules.is_valid is True
    assert rules.get_decimal("detectors", "off_market", "max_deviation_pct") == 25
