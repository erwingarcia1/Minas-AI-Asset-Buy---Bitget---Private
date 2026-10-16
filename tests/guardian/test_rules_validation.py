"""G-12 (#16): a typo in a threshold must be rejected loudly, not silently
disable detection.

What actually happened before this, confirmed by probe against the real poller:

An operator writes `tolerance_pct: "0,5"` — a decimal comma, which is what half
the world's keyboards produce. `_to_decimal_leaves` can't parse it, so it stays
a string. Nothing complains at load. The next poll calls `get_decimal`, which
raises `InvalidOperation` from deep inside the detector section of
`run_fast_loop` — *after* the exchange fetches have already written
`last_success_at = now` into `poll_state`. The exception escapes `run_fast_loop`
(the try/except there only wraps the client calls) and is swallowed by the
worker's blanket `except Exception` in `engine.py`, which logs one line and
moves on.

Net effect: no detector runs, for any account, on any cycle — the config is
shared — while `consecutive_failures` stays 0, `detect_unreachable` never
fires, and the dashboard shows every account successfully polled seconds ago.
The system is not monitoring anything and says it is fine.

That is broader than "silently disabling the rule": one typo silently disables
everything.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from guardian.config import RulesConfig
from guardian.rules_schema import default_for
from tests.guardian.conftest import TEST_USER_ID

GOOD = """
polling:
  fast_loop_seconds: "45"
  slow_loop_seconds: "600"
  jitter_seconds: "10"
detectors:
  quantity_drop:
    tolerance_pct: "0.5"
    tolerance_absolute: "0.00000001"
  off_market:
    max_deviation_pct: "10"
  fee_anomaly:
    multiplier: "5"
    min_fee_floor: "1"
    ema_alpha: "0.2"
  withdrawal_burst:
    window_minutes: 60
    min_count: 3
alerting:
  cooldown_minutes:
    CRITICAL: "15"
    HIGH: "30"
    MEDIUM: "720"
    LOW: "1440"
"""


def _write(path, text, mtime):
    """Writes the file and forces an mtime, since RulesConfig reloads on mtime
    change and a test can easily rewrite within the same clock tick."""
    path.write_text(text)
    import os

    os.utime(path, (mtime, mtime))


@pytest.fixture
def rules_path(tmp_path):
    path = tmp_path / "rules.yaml"
    _write(path, GOOD, 1_000_000)
    return path


# --- rejected at load, not at use -------------------------------------------

def test_a_bad_threshold_is_rejected_when_the_file_is_read(rules_path):
    """The whole point: the complaint arrives at load time, naming the setting,
    rather than as an InvalidOperation from inside a detector twenty minutes
    later.

    Rejection means *not used*, which is the property worth pinning - stronger
    than the exception this originally asserted. The process no longer refuses
    to start; see tests/test_rules_degraded_start.py for why."""
    _write(rules_path, GOOD.replace('tolerance_pct: "0.5"', 'tolerance_pct: "0,5"'), 2_000_000)

    rules = RulesConfig(rules_path)

    assert rules.is_valid is False
    assert "tolerance_pct" in rules.validation_error
    assert "0,5" in rules.validation_error
    assert rules.get("detectors", "quantity_drop", "tolerance_pct") == default_for(
        "detectors", "quantity_drop", "tolerance_pct"
    ), "the rejected value reached a reader instead of the declared fallback"


def test_startup_with_a_bad_config_reports_it_and_does_not_use_the_value(rules_path):
    _write(rules_path, "detectors:\n  fee_anomaly:\n    multiplier: nonsense\n", 2_000_000)

    rules = RulesConfig(rules_path)

    assert rules.is_valid is False
    assert "multiplier" in rules.validation_error
    assert rules.get_decimal(
        "detectors", "fee_anomaly", "multiplier", default=Decimal("5")
    ) == Decimal("5"), "a rejected multiplier was used instead of the default"


def test_malformed_yaml_is_reported_and_nothing_from_it_is_used(rules_path):
    _write(rules_path, "detectors:\n  quantity_drop:\n   tolerance_pct: [unclosed\n", 2_000_000)

    rules = RulesConfig(rules_path)

    assert rules.is_valid is False
    assert "YAML" in rules.validation_error
    assert rules.get("detectors", "quantity_drop", "tolerance_pct") == default_for(
        "detectors", "quantity_drop", "tolerance_pct"
    )


# --- a bad edit while running must not stop monitoring -----------------------

def test_a_bad_edit_while_running_keeps_the_last_good_values(rules_path):
    """Monitoring continues on the last configuration known to be valid. The
    alternative is what happened before: the bad value reaches a detector and
    takes the entire poll down with it."""
    rules = RulesConfig(rules_path)
    assert rules.get_decimal("detectors", "quantity_drop", "tolerance_pct") == Decimal("0.5")

    _write(rules_path, GOOD.replace('tolerance_pct: "0.5"', 'tolerance_pct: "0,5"'), 2_000_000)

    assert rules.get_decimal("detectors", "quantity_drop", "tolerance_pct") == Decimal("0.5")
    assert rules.is_valid is False
    assert "tolerance_pct" in rules.validation_error


def test_a_rejected_reload_is_logged_as_an_error(rules_path, caplog):
    rules = RulesConfig(rules_path)
    _write(rules_path, GOOD.replace('multiplier: "5"', 'multiplier: "five"'), 2_000_000)

    with caplog.at_level(logging.ERROR, logger="guardian.config"):
        rules.get_decimal("detectors", "fee_anomaly", "multiplier")

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a rejected config reload must be an ERROR, not a debug line"
    assert "multiplier" in errors[0].getMessage()


def test_a_rejected_reload_does_not_reflood_the_log(rules_path, caplog):
    """`get()` calls `reload()`, and one poll cycle reads a dozen thresholds
    across every account. Logging per read would bury the message it is trying
    to deliver."""
    rules = RulesConfig(rules_path)
    _write(rules_path, GOOD.replace('multiplier: "5"', 'multiplier: "five"'), 2_000_000)

    with caplog.at_level(logging.ERROR, logger="guardian.config"):
        for _ in range(50):
            rules.get_decimal("detectors", "fee_anomaly", "multiplier")

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, f"logged {len(errors)} times for one bad edit"


def test_fixing_the_file_recovers_without_a_restart(rules_path):
    """Hot reload is the feature this issue exists to protect. A rejected edit
    must not wedge the config until someone restarts the process."""
    rules = RulesConfig(rules_path)
    _write(rules_path, GOOD.replace('tolerance_pct: "0.5"', 'tolerance_pct: "0,5"'), 2_000_000)
    assert rules.is_valid is False

    _write(rules_path, GOOD.replace('tolerance_pct: "0.5"', 'tolerance_pct: "0.8"'), 3_000_000)

    assert rules.get_decimal("detectors", "quantity_drop", "tolerance_pct") == Decimal("0.8")
    assert rules.is_valid is True
    assert rules.validation_error is None


# --- the typo that parses fine --------------------------------------------

def test_a_misspelled_key_is_rejected_and_names_the_intended_one(rules_path):
    """The quieter typo: `tolerance_pc` parses as perfectly valid YAML. The
    reader falls through to its default, the operator believes the new value
    took effect, and nothing anywhere says otherwise."""
    _write(rules_path, GOOD.replace("tolerance_pct:", "tolerance_pc:"), 2_000_000)

    rules = RulesConfig(rules_path)

    message = rules.validation_error
    assert rules.is_valid is False
    assert "tolerance_pc" in message
    assert "tolerance_pct" in message, "should suggest the key that was meant"
    # Reported *and* not in force: the unrecognised key must not be served to
    # anything either. Asserting only the message would let a config that
    # complains and then hands the value out anyway pass.
    assert rules.get("detectors", "quantity_drop", "tolerance_pc") is None


def test_an_unknown_section_is_rejected(rules_path):
    _write(rules_path, GOOD + "\ndetector:\n  off_market:\n    max_deviation_pct: '5'\n", 2_000_000)
    rules = RulesConfig(rules_path)
    assert rules.is_valid is False
    assert "detector" in rules.validation_error
    assert rules.get("detector") is None, "the unknown section was still served"


# --- values that parse but mean "never fire" --------------------------------

@pytest.mark.parametrize(
    "original,broken,setting,path",
    [
        ('min_count: 3', 'min_count: 0', "min_count",
         ("detectors", "withdrawal_burst", "min_count")),
        ('multiplier: "5"', 'multiplier: "0"', "multiplier",
         ("detectors", "fee_anomaly", "multiplier")),
        ('ema_alpha: "0.2"', 'ema_alpha: "1.5"', "ema_alpha",
         ("detectors", "fee_anomaly", "ema_alpha")),
        ('ema_alpha: "0.2"', 'ema_alpha: "-0.2"', "ema_alpha",
         ("detectors", "fee_anomaly", "ema_alpha")),
        ('max_deviation_pct: "10"', 'max_deviation_pct: "-1"', "max_deviation_pct",
         ("detectors", "off_market", "max_deviation_pct")),
        ('tolerance_pct: "0.5"', 'tolerance_pct: "-0.5"', "tolerance_pct",
         ("detectors", "quantity_drop", "tolerance_pct")),
    ],
)
def test_a_value_that_breaks_its_own_formula_is_rejected(rules_path, original, broken, setting, path):
    """Each of these parses as a number and is nonsense where it is used:

    - a burst of fewer than one withdrawal is not a burst
    - `spent > baseline * 0` is true of every trade with a fee
    - the EMA `alpha*obs + (1-alpha)*prev` only converges for 0 < alpha <= 1
    - a negative deviation or tolerance threshold is exceeded by everything

    These are rejected because the formula that consumes them has no meaning at
    the value, which is a different thing from an opinion about how sensitive
    the detector ought to be. No range here narrows a detector's sensitivity."""
    _write(rules_path, GOOD.replace(original, broken), 2_000_000)
    rules = RulesConfig(rules_path)
    assert rules.is_valid is False
    assert setting in rules.validation_error
    # The half that matters at runtime. Being *told* about a value that then
    # reaches the detector anyway is not a rejection - a mutation that kept the
    # reporting and dropped the pruning passed every one of these cases before
    # this line existed.
    assert rules.get(*path) == default_for(*path), (
        f"the rejected {setting} was served to readers instead of its declared fallback"
    )


def test_detection_keeps_running_after_a_bad_edit(db, cipher, rules_path, monkeypatch):
    """The behavioural proof, at the level the defect actually bit.

    Before this, the typo reached `get_decimal` inside `run_fast_loop` and
    raised straight out of the poll — past the try/except, which only wraps the
    client calls — leaving `consecutive_failures` at 0, `last_success_at` freshly
    stamped, and no detector run. A withdrawal arriving on the next cycle went
    unnoticed while every dashboard indicator stayed green.

    So: break the config, then steal from the account, and require the alert."""
    from unittest.mock import MagicMock

    from guardian.onboarding import onboard_account
    from guardian.poller import AccountPoller
    from tests.guardian.fixtures.ccxt_responses import account_info_response, withdrawal_record

    class Channel:
        def __init__(self):
            self.sent = []

        def send(self, alert):
            self.sent.append(alert)

    def client(withdrawals=None):
        c = MagicMock()
        c.fetch_deposits.return_value = []
        c.fetch_withdrawals.return_value = withdrawals or []
        c.fetch_closed_orders.return_value = []
        c.fetch_balance.return_value = {"total": {"BTC": 1.0}}
        c.fetch_account_info.return_value = account_info_response()
        c.fetch_tickers.return_value = {}
        c.fetch_transfers.return_value = []
        return c

    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: client())
    result = onboard_account(db, cipher, user_id=TEST_USER_ID, label="cfg-1", topology="independent", api_key="k", secret="s", passphrase="p"
    )
    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_accounts WHERE id = %s", (result.account_id,))
        account_row = cur.fetchone()

    rules = RulesConfig(rules_path)
    channel = Channel()
    poller = AccountPoller(
        db, cipher, rules, {"CRITICAL": [channel], "HIGH": [channel], "MEDIUM": [channel], "LOW": [channel]}
    )

    _write(rules_path, GOOD.replace('tolerance_pct: "0.5"', 'tolerance_pct: "0,5"'), 2_000_000)

    monkeypatch.setattr(
        "guardian.poller.make_client",
        lambda *a, **k: client(
            withdrawals=[withdrawal_record(order_id="wd-theft", address="bc1qattacker", amount="0.9")]
        ),
    )
    poller.run_fast_loop(account_row)

    assert "withdrawal_initiated" in {a.rule for a in channel.sent}, (
        "a mistyped threshold stopped the poll before any detector ran"
    )
    assert rules.is_valid is False, "and it should still be reported as rejected"


def test_the_rejection_is_exported_as_a_metric(rules_path):
    """The alarmable surface. Resting value 1, so a drop to 0 is a rule an
    operator can bind a page to — the same reasoning as
    `sentinel_untimestamped_records_total`."""
    from prometheus_client import CollectorRegistry

    from guardian.metrics import Metrics

    registry = CollectorRegistry()
    metrics = Metrics(registry=registry)
    rules = RulesConfig(rules_path)

    metrics.set_rules_config_valid(rules.is_valid)
    assert registry.get_sample_value("guardian_rules_config_valid") == 1

    _write(rules_path, GOOD.replace('multiplier: "5"', 'multiplier: "five"'), 2_000_000)
    metrics.set_rules_config_valid(rules.is_valid)
    assert registry.get_sample_value("guardian_rules_config_valid") == 0


def test_the_shipped_config_is_valid():
    """The schema is only useful if it describes the file the product actually
    ships with. A spec that rejects `guardian/rules.yaml` would stop the process
    from starting at all."""
    from pathlib import Path
    rules = RulesConfig(Path(__file__).resolve().parents[2] / "guardian" / "rules.yaml")
    assert rules.is_valid, rules.validation_error


def test_a_legitimate_retune_is_not_rejected(rules_path):
    """The guard against over-validating: quieting a noisy rule is the entire
    user story, so a wide-but-meaningful value has to be allowed through."""
    _write(rules_path, GOOD.replace('max_deviation_pct: "10"', 'max_deviation_pct: "75"'), 2_000_000)
    rules = RulesConfig(rules_path)
    assert rules.get_decimal("detectors", "off_market", "max_deviation_pct") == Decimal("75")
