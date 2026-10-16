"""Recording what changed in the thresholds, and who changed it (G-12, #16).

The third criterion on #16, blocked until #54 gave the product operator
identities. It is worth having for a reason that is not compliance: a detection
threshold is the difference between an alert and silence, so an unrecorded
threshold change is an unrecorded change to what this product notices. If
`polling.fast_loop_seconds` goes from 45 to 3600 and nothing says so, the
account stops being watched in the only way that matters and every dashboard
still reads healthy.

Two paths, deliberately distinguishable in the log:

- `sentinel set-threshold --as <operator>` — attributable. The event names a
  person.
- an edit made directly to `rules.yaml` on disk — the reload notices the
  difference and records it with **no** actor. That is not a gap to be filled
  with a guess; "this changed and we cannot say who" is the finding.
"""
from __future__ import annotations

import pytest

from guardian.config import RulesConfig, settings_diff
from guardian.events import record_config_changes

BASE = """
polling:
  fast_loop_seconds: 45
alerting:
  cooldown_minutes:
    CRITICAL: 15
    HIGH: 30
"""


def _changes(db):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_config_changes ORDER BY id")
        return cur.fetchall()


@pytest.fixture
def rules_path(tmp_path):
    p = tmp_path / "rules.yaml"
    p.write_text(BASE)
    return p


# --- the diff itself ----------------------------------------------------------


def test_a_changed_threshold_is_reported_with_both_values(db):
    changes = settings_diff({"polling": {"fast_loop_seconds": 45}},
                            {"polling": {"fast_loop_seconds": 90}})

    assert changes == [("polling.fast_loop_seconds", "45", "90")]


def test_an_unchanged_file_reports_nothing(db):
    assert settings_diff({"a": {"b": 1}}, {"a": {"b": 1}}) == []


def test_an_added_and_a_removed_setting_are_both_reported(db):
    """A removed threshold is not a no-op: the value falls back to the schema
    default, which is a change to what the product does and usually an
    accident. It has to read as a change, not as an absence."""
    changes = settings_diff({"a": {"gone": 3}}, {"a": {"added": 7}})

    assert sorted(changes) == [("a.added", None, "7"), ("a.gone", "3", None)]


def test_nested_sections_are_reported_by_full_path(db):
    """`cooldown_minutes.CRITICAL` and `cooldown_minutes.HIGH` are different
    settings that differ only in their last segment. A diff keyed on the
    section would report one change where two happened."""
    changes = settings_diff(
        {"alerting": {"cooldown_minutes": {"CRITICAL": 15, "HIGH": 30}}},
        {"alerting": {"cooldown_minutes": {"CRITICAL": 60, "HIGH": 30}}},
    )

    assert changes == [("alerting.cooldown_minutes.CRITICAL", "15", "60")]


# --- an edit on disk ----------------------------------------------------------


def test_an_edit_on_disk_is_recorded_with_no_actor(db, rules_path):
    """The honest half. Nobody can say who edited a file on a server, so the
    event says nobody — `actor` stays NULL rather than being filled in with
    'system' or the process owner, either of which would make the log claim a
    person acted."""
    rules = RulesConfig(rules_path, on_change=lambda changes: _record(db, changes))
    rules_path.write_text(BASE.replace("fast_loop_seconds: 45", "fast_loop_seconds: 90"))

    rules.reload()

    rows = _changes(db)
    assert len(rows) == 1
    assert rows[0]["setting"] == "polling.fast_loop_seconds"
    assert (rows[0]["old_value"], rows[0]["new_value"]) == ("45", "90")
    assert rows[0]["actor"] is None


def test_the_first_load_is_not_reported_as_a_change(db, rules_path):
    """Starting the service is not somebody editing the thresholds. Reporting
    every setting as changed at boot would bury the one real change under a
    hundred, which is the same as not recording it."""
    RulesConfig(rules_path, on_change=lambda changes: _record(db, changes))

    assert _changes(db) == []


def test_a_rejected_file_records_no_change(db, rules_path):
    """A file that fails validation never takes effect, so nothing changed.
    Recording the attempted values as a change would put thresholds in the
    audit log that no detector ever used."""
    rules = RulesConfig(rules_path, on_change=lambda changes: _record(db, changes))
    rules_path.write_text("polling:\n  fast_loop_seconds: not-a-number\n")

    rules.reload()

    assert rules.is_valid is False
    assert _changes(db) == []


def test_touching_the_file_without_changing_it_records_nothing(db, rules_path):
    """mtime moves on every save, including the one where you looked at the
    file and changed nothing."""
    rules = RulesConfig(rules_path, on_change=lambda changes: _record(db, changes))
    rules_path.write_text(BASE + "\n#  a comment\n")

    rules.reload()

    assert _changes(db) == []


def test_a_broken_recorder_never_stops_the_reload(db, rules_path):
    """The values on disk taking effect matters more than the audit line. A
    recorder that raises must not leave the process running thresholds older
    than the file — that is the G-12 failure this whole issue is about,
    reintroduced through the logging."""
    def explode(changes):
        raise RuntimeError("no database today")

    rules = RulesConfig(rules_path, on_change=explode)
    rules_path.write_text(BASE.replace("fast_loop_seconds: 45", "fast_loop_seconds: 90"))

    rules.reload()

    assert rules.get_int("polling", "fast_loop_seconds") == 90


def _record(db, changes):
    record_config_changes(db, changes, actor=None)


# --- wired, not merely written -------------------------------------------------


# --- set-threshold -------------------------------------------------------------


def test_the_worker_passes_a_recorder_to_its_config():
    """The call site, pinned: a RulesConfig built without `on_change` records
    nothing, and every test of the recorder still passes."""
    import inspect

    from guardian import worker

    source = inspect.getsource(worker.main)
    assert "on_change=lambda changes: record_config_changes(db, changes)" in source
