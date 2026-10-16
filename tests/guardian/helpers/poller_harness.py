"""One fast-loop poll against a stubbed exchange, for tests that care about
what the poller *reports* rather than what a detector returns."""
from __future__ import annotations

from unittest.mock import MagicMock

from prometheus_client import CollectorRegistry

from guardian.alerts.operator import OperatorAlerter
from guardian.crypto import CredentialCipher
from guardian.metrics import Metrics
from guardian.onboarding import onboard_account
from guardian.poller import AccountPoller
from tests.guardian.fixtures.ccxt_responses import account_info_response
from tests.guardian.conftest import TEST_USER_ID


class _Recording:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


def poll_once_with_withdrawals(db, rules, withdrawals, label="acct-1", cipher=None):
    """Runs one fast loop with `withdrawals` returned by the exchange.

    Returns (operator_pages, metric_counts). Pages are read back out of the
    `operator_pages` table rather than from a spy, so the test covers the row
    that an operator would actually be shown."""
    import guardian.onboarding as onboarding_module
    import guardian.poller as poller_module

    def _client(returned_withdrawals):
        c = MagicMock()
        c.fetch_deposits.return_value = []
        c.fetch_withdrawals.return_value = returned_withdrawals
        c.fetch_closed_orders.return_value = []
        c.fetch_balance.return_value = {"total": {"BTC": 1.0}}
        c.fetch_account_info.return_value = account_info_response()
        c.fetch_tickers.return_value = {}
        c.fetch_transfers.return_value = []
        return c

    # Onboarding sees NO withdrawals. It baselines whatever the exchange
    # returns - that is its job - so an onboarding client that already knows
    # about these records would mark them seen, and the poll under test would
    # then find nothing new and detect nothing. The bug that hides is a real
    # one, so it is worth naming: the harness must not pre-seed the state the
    # test is about.
    original_onboard, original_poll = onboarding_module.make_client, poller_module.make_client
    onboarding_module.make_client = lambda *a, **k: _client([])
    poller_module.make_client = lambda *a, **k: _client(withdrawals)
    try:
        from cryptography.fernet import Fernet
        key_holder = cipher or CredentialCipher(Fernet.generate_key().decode())
        result = onboard_account(db, key_holder, user_id=TEST_USER_ID, label=label, topology="independent",
                                 api_key="k", secret="s", passphrase="p")
        with db.cursor() as cur:
            cur.execute("SELECT * FROM guardian_accounts WHERE id = %s", (result.account_id,))
            row = cur.fetchone()

        # A fresh registry per call: Metrics registers module-level collector
        # names, and a second instance against the global registry raises
        # DuplicateTimeseries the moment two tests both poll.
        metrics = Metrics(registry=CollectorRegistry())
        channels = {"CRITICAL": [_Recording()], "HIGH": [_Recording()],
                    "MEDIUM": [_Recording()], "LOW": [_Recording()]}
        alerter = OperatorAlerter(db, {"CRITICAL": [_Recording()], "HIGH": [_Recording()]}, rules)
        AccountPoller(db, key_holder, rules, channels, metrics=metrics,
                      operator_alerter=alerter).run_fast_loop(row)
    finally:
        onboarding_module.make_client = original_onboard
        poller_module.make_client = original_poll

    with db.cursor() as cur:
        cur.execute("SELECT rule, severity, message, evidence_json FROM guardian_operator_pages ORDER BY id")
        pages = [dict(r) for r in cur.fetchall()]
    for page in pages:
        page["evidence"] = page.pop("evidence_json")

    return pages, {"unidentifiable_withdrawals": metrics.unidentifiable_withdrawals_total()}
