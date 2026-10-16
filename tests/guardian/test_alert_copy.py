"""Alert copy sends people to this platform, not to Guardian's old CLI.

Guardian shipped with a `sentinel` command line; its escalation text told a
customer to run `sentinel ack <id>`, which does not exist here and would read
as nonsense in the middle of the one message that most needs to be acted on.
Acknowledging happens in the AutoBuy app under Guardian (the pages arrive
with PR4). The scan reads the string literals that can reach a page or a
phone, so a comment or a docstring explaining the history cannot trip it.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from guardian.alerts.escalation import Escalator
from guardian.alerts.sms import format_sms_body
from tests.guardian.conftest import make_account

ROOT = Path(__file__).resolve().parent.parent.parent
ALERTS_DIR = ROOT / "guardian" / "alerts"


class Recording:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


def _visible_strings(path: Path):
    """Every string literal that is not a docstring, with its line number."""
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value


def _alert_sources() -> list[Path]:
    return sorted(p for p in ALERTS_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_scan_sees_the_alert_sources():
    files = _alert_sources()
    assert len(files) >= 8, f"only {len(files)} files under guardian/alerts - the scan is looking in the wrong place"
    assert any(p.name == "escalation.py" for p in files)


@pytest.mark.parametrize("path", _alert_sources(), ids=lambda p: p.name)
def test_no_alert_string_mentions_the_old_cli(path: Path):
    offences = [f"{path.name}:{lineno}: {text.strip()[:100]!r}"
                for lineno, text in _visible_strings(path) if "sentinel" in text.lower()]
    assert offences == [], "a message names a CLI this platform does not have:\n  " + "\n  ".join(offences)


def test_the_email_templates_do_not_mention_the_old_cli():
    """Guardian's own mail is built in guardian/alerts (email.py renders
    base.py's text; `guardian_alert` is the outbox's label, not a file), so
    this reads the platform's template directory as a whole."""
    files = sorted(p for p in (ROOT / "templates").rglob("*") if p.is_file())
    assert files, "no templates found - the scan is looking in the wrong place"
    offences = [str(p.relative_to(ROOT)) for p in files
                if "sentinel" in p.read_text(errors="ignore").lower()]
    assert offences == []


def test_the_escalation_tells_people_where_to_acknowledge(db):
    """The one message that used to carry the CLI, end to end: the escalation
    AlertMessage as the channels receive it, and the SMS body built from it."""
    account_id = make_account(db, "esc")
    stale = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_alerts (account_id, rule, severity, dedup_key, message, evidence_json, "
            "confidence, sent_at, delivered_to) VALUES (%s, 'withdrawal_initiated', 'CRITICAL', 'k-1', "
            "'0.5 BTC left the account', '{}', 'high', %s, 1)",
            (account_id, stale),
        )
    channel = Recording()

    assert Escalator([channel], unacknowledged_minutes=15).check(db) == 1

    (alert,) = channel.sent
    assert "sentinel" not in alert.message.lower()
    assert "acknowledge it in the AutoBuy app under Guardian" in alert.message
    assert "0.5 BTC left the account" in alert.message, "the original is still quoted"
    assert "sentinel" not in format_sms_body(alert).lower()
