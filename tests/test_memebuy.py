"""Tests for the memebuy orchestrator package.

ZERO network, ZERO credentials, ZERO real modules: the sibling packages
(payout/, chains/, safety/) are injected as recording fakes via memebuy.Deps.
The fakes raise AssertionError if they are ever handed dry_run=False while the
test expects simulation, so "no money-moving call happened" is enforced by the
fake rather than asserted after the fact.

Run:  python -m unittest tests.test_memebuy -v
"""

import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import types
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The orchestrator logs every refusal loudly on purpose; keep test output clean.
logging.getLogger("memebuy").addHandler(logging.NullHandler())
logging.getLogger("memebuy").propagate = False

import memebuy_cli  # noqa: E402
from memebuy import caps, config, credentials, orchestrator  # noqa: E402
from memebuy.config import ChainConfig, ConfigError, MemeBuyConfig  # noqa: E402
from memebuy.orchestrator import Deps, MemeBuyOrchestrator  # noqa: E402
from memebuy.state import MemeBuyState  # noqa: E402

try:  # the checksum helper needs safety/, whose __init__ pulls in `requests`
    from safety._address import to_checksum_address  # noqa: F401,E402

    HAS_SAFETY_ADDRESS = True
except Exception:  # pragma: no cover - depends on the environment
    HAS_SAFETY_ADDRESS = False

BSC_TOKEN = "0x0e09fabb73bd3ade0a17ecc321fd13a19e81ce82"
# safety.gate rejects all-lowercase EVM addresses (EIP-55 checksum required).
BSC_TOKEN_EIP55 = "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82"
BSC_OTHER_TOKEN = "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
BSC_WITHDRAW = "0x1111111111111111111111111111111111111111"
SOL_MINT = "So11111111111111111111111111111111111111112"
SOL_WITHDRAW = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"

ARMED_ENV = {"MEME_BUY_ARMED": "1"}


# ---------- fakes ----------


class FakePayout:
    """Records calls; refuses live calls unless the test opts in."""

    def __init__(self, live_allowed=False):
        self.calls = []
        self.live_allowed = live_allowed
        self.transfer_error = None
        self.withdraw_error = None
        self.on_transfer = None
        self.status_response = {"status": "success", "orderId": "W-1"}

    def _record(self, kind, dry_run, **kw):
        if not dry_run and not self.live_allowed:
            raise AssertionError("LIVE %s call made while simulating" % kind)
        self.calls.append(dict(kind=kind, dry_run=dry_run, **kw))

    def kinds(self):
        return [c["kind"] for c in self.calls]

    def transfer_sub_to_main(self, coin, amount, client_oid, dry_run):
        self._record("transfer", dry_run, coin=coin, amount=amount, client_oid=client_oid)
        if self.on_transfer:
            self.on_transfer()
        if self.transfer_error:
            raise self.transfer_error
        return {"transferId": "T-1"}

    def withdraw_onchain(self, coin, chain, address, amount, client_oid, allowlist, dry_run):
        self._record(
            "withdraw", dry_run, coin=coin, chain=chain, address=address,
            amount=amount, client_oid=client_oid, allowlist=list(allowlist),
        )
        if self.withdraw_error:
            raise self.withdraw_error
        return {"orderId": "W-1"}

    def get_withdrawal_status(self, client_oid, start_ms, end_ms):
        self.calls.append(dict(kind="status", dry_run=True, client_oid=client_oid))
        return self.status_response


class FakeSwap:
    """Stands in for chains.<chain>_swap.buy_token.

    The signature is spelled out on purpose: the orchestrator refuses a swap
    callee that only absorbs `dry_run` through a `**kwargs` tail, because such a
    tail accepts every keyword including one the callee then ignores.
    """

    def __init__(self, key="tx_hash", live_allowed=False):
        self.calls = []
        self.key = key
        self.live_allowed = live_allowed
        self.error = None

    def __call__(self, usdt_amount, slippage_bps, dry_run, rpc_url=None,
                 min_native_balance=None, token_address=None, recipient=None,
                 token_mint=None, mint=None, taker=None):
        kwargs = {k: v for k, v in locals().items() if k != "self"}
        if not dry_run and not self.live_allowed:
            raise AssertionError("LIVE swap call made while simulating")
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {self.key: "0xswap", "amount_out_min": 1}


class FakeBitgetSwap(FakeSwap):
    """chains.bitget_swap.buy_token: one aggregator, so it is told the chain.

    Like FakeSwap the signature is spelled out — a `**kwargs` tail would not
    prove the callee honours dry_run, and the orchestrator refuses one.
    """

    def __call__(self, usdt_amount, slippage_bps, dry_run, chain=None, gasless=False,
                 token_address=None, mint=None, token_mint=None, recipient=None,
                 taker=None, rpc_url=None, min_native_balance=None):
        kwargs = {k: v for k, v in locals().items() if k != "self"}
        if not dry_run and not self.live_allowed:
            raise AssertionError("LIVE swap call made while simulating")
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"backend": "bitget", self.key: "0xswapx"}


class RolePayout(FakePayout):
    """A payout double that models `account_role`, like the real BitgetPayout.

    Stage 1 must reach the SUB instance and stage 2 the MAIN one; each refuses
    the other's call exactly as payout/bitget_payout.py does, so a mix-up shows
    up here instead of after the transfer has already moved money.
    """

    def __init__(self, account_role, live_allowed=False):
        super().__init__(live_allowed=live_allowed)
        self.account_role = account_role

    def transfer_sub_to_main(self, coin, amount, client_oid, dry_run):
        if self.account_role != "sub":
            raise AssertionError("transfer signed with a %r key" % self.account_role)
        return super().transfer_sub_to_main(coin, amount, client_oid, dry_run)

    def withdraw_onchain(self, coin, chain, address, amount, client_oid, allowlist, dry_run):
        if self.account_role != "main":
            raise AssertionError("withdrawal signed with a %r key" % self.account_role)
        return super().withdraw_onchain(
            coin, chain, address, amount, client_oid, allowlist, dry_run
        )


class FakeVerdict:
    def __init__(self, passed=True, reasons=None):
        self.passed = passed
        self.checks = {"honeypot": "ok"}
        self.reasons = reasons or []


class ThinVerdict:
    """A verdict object missing `passed` entirely — must fail closed."""

    checks = {}
    reasons = []


class FakeGate:
    def __init__(self, verdict=None, error=None):
        self.calls = []
        self.verdict = verdict if verdict is not None else FakeVerdict(True)
        self.error = error

    def __call__(self, chain, address, price_impact_bps=None):
        self.calls.append((chain, address, price_impact_bps))
        if self.error:
            raise self.error
        return self.verdict


class FakeClock:
    """Virtual time: sleeping advances the clock, so no test ever waits."""

    def __init__(self, start=1_700_000_000.0):
        self.now = start
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += float(seconds)


class FakeProbe:
    """Destination-wallet balance: 0 at baseline, then funded."""

    def __init__(self, values=(0.0, 1000.0)):
        self.values = list(values)
        self.calls = 0

    def __call__(self, chain, address):
        self.calls += 1
        idx = min(self.calls - 1, len(self.values) - 1)
        return self.values[idx]


# ---------- fixtures ----------


def raw_config(**over):
    block = {
        "armed": False,
        "dry_run": True,
        "per_trade_usdt_cap": 50.0,
        "daily_usdt_cap": 100.0,
        "kill_switch_file": "/tmp/does-not-exist-meme-buy-stop",
        "chains": {
            "bsc": {
                "enabled": True,
                "withdraw_address": BSC_WITHDRAW,
                "rpc_url": "https://bsc-dataseed.example/",
                "min_native_balance": 0.01,
                "slippage_bps": 300,
                "token_allowlist": [BSC_TOKEN],
            },
            "solana": {
                "enabled": True,
                "withdraw_address": SOL_WITHDRAW,
                "rpc_url": "https://api.mainnet-beta.example/",
                "min_native_balance": 0.02,
                "slippage_bps": 300,
                "token_allowlist": [SOL_MINT],
            },
        },
    }
    chains_over = over.pop("chains_over", {})
    block.update(over)
    for name, patch in chains_over.items():
        block["chains"][name].update(patch)
    return {"meme_buy": block}


class Fixture(unittest.TestCase):
    """Temp dir, temp journal, fully-wired fakes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "data", "meme_buy.sqlite3")
        self.kill_file = os.path.join(self.tmp.name, "STOP")
        self.state = MemeBuyState(self.db_path)
        self.payout = FakePayout()
        self.bsc = FakeSwap("tx_hash")
        self.sol = FakeSwap("signature")
        self.gate = FakeGate()
        self.probe = FakeProbe()
        self.clock = FakeClock()

    def deps(self):
        return Deps(
            payout=self.payout,
            bsc_buy=self.bsc,
            solana_buy=self.sol,
            screen_token=self.gate,
            balance_probe=self.probe,
        )

    def cfg(self, **over):
        over.setdefault("kill_switch_file", self.kill_file)
        over.setdefault("db_path", self.db_path)
        return config.load_meme_buy_config(raw_config(**over))

    def live_cfg(self, **over):
        over.setdefault("armed", True)
        over.setdefault("dry_run", False)
        return self.cfg(**over)

    def orch(self, cfg=None, env=None, live=False):
        if cfg is None:
            cfg = self.live_cfg() if live else self.cfg()
        if env is None:
            env = ARMED_ENV if live else {}
        if live:
            self.payout.live_allowed = True
            self.bsc.live_allowed = True
            self.sol.live_allowed = True
        return MemeBuyOrchestrator(
            cfg,
            state=self.state,
            deps=self.deps(),
            env=env,
            clock=self.clock.time,
            sleep=self.clock.sleep,
        )


# ---------- caps: pure ----------


class TestCaps(unittest.TestCase):
    def test_per_trade_cap(self):
        self.assertTrue(caps.check_per_trade_cap(10, 10))
        self.assertFalse(caps.check_per_trade_cap(10.01, 10))
        self.assertFalse(caps.check_per_trade_cap(0, 10))
        self.assertFalse(caps.check_per_trade_cap(-1, 10))
        self.assertFalse(caps.check_per_trade_cap(10, 0))
        self.assertFalse(caps.check_per_trade_cap(None, 10))
        self.assertFalse(caps.check_per_trade_cap("10", 10))
        self.assertFalse(caps.check_per_trade_cap(float("nan"), 10))
        self.assertFalse(caps.check_per_trade_cap(float("inf"), 10))

    def test_daily_cap(self):
        self.assertTrue(caps.check_daily_cap(10, 90, 100))
        self.assertFalse(caps.check_daily_cap(10.01, 90, 100))
        self.assertFalse(caps.check_daily_cap(10, None, 100))  # unknown spend != safe
        self.assertFalse(caps.check_daily_cap(10, -1, 100))

    def test_address_allowlist(self):
        self.assertTrue(caps.check_address_allowlist(BSC_WITHDRAW, [BSC_WITHDRAW]))
        self.assertTrue(
            caps.check_address_allowlist(BSC_WITHDRAW.upper().replace("0X", "0x"), [BSC_WITHDRAW])
        )
        self.assertFalse(caps.check_address_allowlist(BSC_WITHDRAW, []))
        self.assertFalse(caps.check_address_allowlist(BSC_WITHDRAW, None))
        self.assertFalse(caps.check_address_allowlist("", [BSC_WITHDRAW]))
        self.assertFalse(caps.check_address_allowlist(BSC_TOKEN, [BSC_WITHDRAW]))
        # Solana is case-sensitive: a case-flipped address is a different wallet.
        self.assertFalse(caps.check_address_allowlist(SOL_WITHDRAW.lower(), [SOL_WITHDRAW]))

    def test_token_allowlist(self):
        allow = {"bsc": [BSC_TOKEN], "solana": [SOL_MINT]}
        self.assertTrue(caps.check_token_allowlist("bsc", BSC_TOKEN.upper().replace("0X", "0x"), allow))
        self.assertTrue(caps.check_token_allowlist("solana", SOL_MINT, [SOL_MINT]))
        self.assertFalse(caps.check_token_allowlist("bsc", BSC_OTHER_TOKEN, allow))
        self.assertFalse(caps.check_token_allowlist("bsc", SOL_MINT, allow))
        self.assertFalse(caps.check_token_allowlist("ethereum", BSC_TOKEN, allow))
        self.assertFalse(caps.check_token_allowlist("bsc", BSC_TOKEN, {"bsc": []}))

    def test_kill_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "STOP")
            self.assertFalse(caps.kill_switch_engaged(path))
            open(path, "w").close()
            self.assertTrue(caps.kill_switch_engaged(path))
        # Unconfigured kill switch == no way to halt == engaged.
        self.assertTrue(caps.kill_switch_engaged(""))
        self.assertTrue(caps.kill_switch_engaged(None))


# ---------- config validation ----------


class TestConfig(unittest.TestCase):
    def test_defaults_are_safe(self):
        cfg = config.load_meme_buy_config(
            {"meme_buy": {
                "per_trade_usdt_cap": 10, "daily_usdt_cap": 20,
                "kill_switch_file": "/tmp/stop", "chains": {},
            }}
        )
        self.assertFalse(cfg.armed)
        self.assertTrue(cfg.dry_run)

    def test_valid_config_loads(self):
        cfg = config.load_meme_buy_config(raw_config())
        self.assertEqual(cfg.chain("bsc").slippage_bps, 300)
        self.assertEqual(cfg.chain("bsc").withdraw_allowlist, (BSC_WITHDRAW,))

    def _reject(self, **over):
        with self.assertRaises(ConfigError):
            config.load_meme_buy_config(raw_config(**over))

    def test_rejects_armed_with_empty_token_allowlist(self):
        self._reject(armed=True, chains_over={"bsc": {"token_allowlist": []}})

    def test_rejects_armed_with_no_enabled_chain(self):
        self._reject(
            armed=True,
            chains_over={"bsc": {"enabled": False}, "solana": {"enabled": False}},
        )

    def test_rejects_bad_withdraw_address_format(self):
        self._reject(chains_over={"bsc": {"withdraw_address": SOL_WITHDRAW}})
        self._reject(chains_over={"solana": {"withdraw_address": BSC_WITHDRAW}})
        self._reject(chains_over={"bsc": {"withdraw_address": "0x1234"}})

    def test_rejects_withdraw_address_outside_its_own_allowlist(self):
        self._reject(chains_over={"bsc": {"withdraw_allowlist": [BSC_TOKEN]}})

    def test_rejects_non_positive_caps(self):
        self._reject(per_trade_usdt_cap=0)
        self._reject(per_trade_usdt_cap=-1)
        self._reject(daily_usdt_cap=0)
        self._reject(per_trade_usdt_cap=200)  # per-trade above daily

    def test_rejects_insane_slippage(self):
        self._reject(chains_over={"bsc": {"slippage_bps": 2501}})
        self._reject(chains_over={"bsc": {"slippage_bps": 0}})
        self._reject(chains_over={"bsc": {"slippage_bps": 3.5}})

    def test_rejects_missing_kill_switch_and_typos(self):
        raw = raw_config()
        del raw["meme_buy"]["kill_switch_file"]
        with self.assertRaises(ConfigError):
            config.load_meme_buy_config(raw)
        self._reject(per_trade_usdt_capp=5)  # unknown key must not be ignored
        self._reject(chains_over={"bsc": {"token_allowlist": ["not-an-address"]}})

    def test_rejects_unsupported_chain(self):
        raw = raw_config()
        raw["meme_buy"]["chains"]["ethereum"] = {"enabled": False}
        with self.assertRaises(ConfigError):
            config.load_meme_buy_config(raw)

    def test_env_arm_ack(self):
        self.assertTrue(config.env_arm_acknowledged({"MEME_BUY_ARMED": "1"}))
        self.assertFalse(config.env_arm_acknowledged({"MEME_BUY_ARMED": "true"}))
        self.assertFalse(config.env_arm_acknowledged({}))


# ---------- state journal ----------


class TestState(Fixture):
    def test_daily_cap_survives_restart(self):
        run_id = self.state.begin_run("oid-a", "bsc", BSC_TOKEN, 40.0, dry_run=False)
        self.state.mark_committed(run_id)
        self.assertEqual(self.state.spent_last_24h(), 40.0)

        reopened = MemeBuyState(self.db_path)  # simulated process restart
        self.assertEqual(reopened.spent_last_24h(), 40.0)

    def test_uncommitted_and_dry_runs_do_not_count(self):
        self.state.begin_run("oid-b", "bsc", BSC_TOKEN, 40.0, dry_run=False)
        dry = self.state.begin_run("oid-c", "bsc", BSC_TOKEN, 40.0, dry_run=True)
        self.state.mark_committed(dry)
        self.assertEqual(self.state.spent_last_24h(), 0.0)

    def test_old_spend_falls_out_of_window(self):
        run_id = self.state.begin_run("oid-d", "bsc", BSC_TOKEN, 40.0, dry_run=False)
        self.state.mark_committed(run_id)
        future = self.state.spent_last_24h(now=__import__("time").time() + 25 * 3600)
        self.assertEqual(future, 0.0)

    def test_duplicate_client_oid_rejected(self):
        self.state.begin_run("oid-e", "bsc", BSC_TOKEN, 1.0)
        from memebuy.state import DuplicateClientOid

        with self.assertRaises(DuplicateClientOid):
            self.state.begin_run("oid-e", "bsc", BSC_TOKEN, 1.0)
        self.assertIsNotNone(self.state.find_run_by_client_oid("oid-e"))


# ---------- orchestrator ----------


class TestOrchestratorDryRun(Fixture):
    def test_unarmed_makes_no_live_calls(self):
        result = self.orch().run_once("bsc", BSC_TOKEN, 25.0)
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.dry_run)
        self.assertEqual(self.payout.kinds(), ["transfer", "withdraw"])
        self.assertTrue(all(c["dry_run"] for c in self.payout.calls))
        self.assertTrue(all(c["dry_run"] for c in self.bsc.calls))
        # No RPC polling and no money accounted against the live daily cap.
        self.assertEqual(self.probe.calls, 0)
        self.assertEqual(self.state.spent_last_24h(), 0.0)

    def test_armed_config_without_env_ack_still_dry_runs(self):
        # Hot-reloading a config to armed=true must not arm a running process.
        orch = MemeBuyOrchestrator(
            self.live_cfg(), state=self.state, deps=self.deps(), env={}
        )
        self.assertFalse(orch.armed)
        self.assertTrue(orch.dry_run)
        result = orch.run_once("bsc", BSC_TOKEN, 25.0)
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(all(c["dry_run"] for c in self.payout.calls))

    def test_solana_path_uses_solana_swap(self):
        result = self.orch().run_once("solana", SOL_MINT, 25.0)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.stage, orchestrator.STAGE_OUTCOME)
        self.assertEqual(len(self.sol.calls), 1)
        self.assertEqual(self.sol.calls[0]["token_mint"], SOL_MINT)
        self.assertEqual(len(self.bsc.calls), 0)
        self.assertEqual(self.payout.calls[1]["chain"], "SOL")

    def test_every_stage_is_journaled_before_and_after(self):
        result = self.orch().run_once("bsc", BSC_TOKEN, 25.0)
        phases = {(e["stage"], e["phase"]) for e in self.state.events(result.run_id)}
        for stage in (0, 1, 2, 3, 4, 5, 7):
            self.assertIn((stage, "begin"), phases, "no begin journal for stage %d" % stage)
        for stage in (1, 2, 3, 4, 5):
            self.assertIn((stage, "end"), phases, "no end journal for stage %d" % stage)


class TestGuardrails(Fixture):
    def test_kill_switch_halts_mid_pipeline(self):
        self.payout.on_transfer = lambda: open(self.kill_file, "w").close()
        result = self.orch().run_once("bsc", BSC_TOKEN, 25.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "killed")
        self.assertEqual(self.payout.kinds(), ["transfer"])  # never reached withdraw
        self.assertEqual(len(self.bsc.calls), 0)
        self.assertEqual(self.state.get_run(result.run_id)["status"], "killed")

    def test_kill_switch_blocks_before_stage_zero(self):
        open(self.kill_file, "w").close()
        result = self.orch().run_once("bsc", BSC_TOKEN, 25.0)
        self.assertEqual(result.status, "killed")
        self.assertEqual(result.stage, orchestrator.STAGE_SELECT)
        self.assertEqual(self.payout.calls, [])

    def test_per_trade_cap_blocks_before_any_call(self):
        result = self.orch().run_once("bsc", BSC_TOKEN, 50.01)
        self.assertFalse(result.ok)
        self.assertIn("per_trade", result.reason)
        self.assertEqual(self.payout.calls, [])
        self.assertIsNone(self.state.find_run_by_client_oid(result.client_oid or ""))

    def test_daily_cap_blocks_using_persisted_spend(self):
        seeded = self.state.begin_run("seed", "bsc", BSC_TOKEN, 80.0, dry_run=False)
        self.state.mark_committed(seeded)
        result = self.orch(live=True).run_once("bsc", BSC_TOKEN, 25.0)
        self.assertFalse(result.ok)
        self.assertIn("daily", result.reason)
        self.assertEqual(self.payout.calls, [])

    def test_daily_cap_still_blocks_after_restart(self):
        seeded = self.state.begin_run("seed", "bsc", BSC_TOKEN, 80.0, dry_run=False)
        self.state.mark_committed(seeded)
        fresh_state = MemeBuyState(self.db_path)  # new object, same file
        orch = MemeBuyOrchestrator(
            self.live_cfg(), state=fresh_state, deps=self.deps(), env=ARMED_ENV
        )
        self.payout.live_allowed = True
        result = orch.run_once("bsc", BSC_TOKEN, 25.0)
        self.assertFalse(result.ok)
        self.assertIn("daily", result.reason)
        self.assertEqual(self.payout.calls, [])

    def test_non_allowlisted_token_aborts(self):
        result = self.orch().run_once("bsc", BSC_OTHER_TOKEN, 25.0)
        self.assertFalse(result.ok)
        self.assertIn("allowlisted", result.reason)
        self.assertEqual(self.payout.calls, [])

    def test_malformed_token_aborts(self):
        result = self.orch().run_once("bsc", "not-an-address", 25.0)
        self.assertFalse(result.ok)
        self.assertEqual(self.payout.calls, [])

    def test_non_allowlisted_withdraw_address_aborts(self):
        # Simulates a config assembled in code that bypassed load-time checks.
        bad = ChainConfig(
            name="bsc", enabled=True,
            withdraw_address="0x2222222222222222222222222222222222222222",
            withdraw_allowlist=(BSC_WITHDRAW,),
            rpc_url="https://bsc.example/", min_native_balance=0.01,
            slippage_bps=300, token_allowlist=(BSC_TOKEN,),
        )
        cfg = MemeBuyConfig(
            armed=True, dry_run=False, per_trade_usdt_cap=50.0, daily_usdt_cap=100.0,
            kill_switch_file=self.kill_file, db_path=self.db_path,
            chains={"bsc": bad},
        )
        orch = MemeBuyOrchestrator(cfg, state=self.state, deps=self.deps(), env=ARMED_ENV)
        self.payout.live_allowed = True
        result = orch.run_once("bsc", BSC_TOKEN, 25.0)
        self.assertFalse(result.ok)
        self.assertIn("allowlist", result.reason)
        self.assertEqual(self.payout.calls, [])

    def test_disabled_chain_aborts(self):
        cfg = self.cfg(chains_over={"solana": {"enabled": False}})
        orch = MemeBuyOrchestrator(cfg, state=self.state, deps=self.deps(), env={})
        result = orch.run_once("solana", SOL_MINT, 10.0)
        self.assertFalse(result.ok)
        self.assertEqual(self.payout.calls, [])


class TestSafetyGate(Fixture):
    def test_gate_reject_aborts_before_swap(self):
        self.gate.verdict = FakeVerdict(False, ["honeypot: sell simulation failed"])
        result = self.orch().run_once("bsc", BSC_TOKEN, 25.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "gate_rejected")
        self.assertEqual(result.stage, orchestrator.STAGE_GATE)
        self.assertEqual(len(self.bsc.calls), 0)
        row = self.state.get_run(result.run_id)
        self.assertIn("honeypot", row["gate_verdict"])

    def test_gate_missing_passed_field_fails_closed(self):
        self.gate.verdict = ThinVerdict()
        result = self.orch().run_once("bsc", BSC_TOKEN, 25.0)
        self.assertEqual(result.status, "gate_rejected")
        self.assertEqual(len(self.bsc.calls), 0)

    def test_gate_exception_fails_closed(self):
        self.gate.error = RuntimeError("HTTP 429 from GoPlus")
        result = self.orch().run_once("bsc", BSC_TOKEN, 25.0)
        self.assertEqual(result.status, "gate_rejected")
        self.assertEqual(len(self.bsc.calls), 0)

    def test_gate_runs_immediately_before_swap(self):
        self.orch().run_once("bsc", BSC_TOKEN, 25.0)
        self.assertEqual(len(self.gate.calls), 1)
        chain, address, impact = self.gate.calls[0]
        self.assertEqual(chain, "bsc")
        self.assertEqual(address.lower(), BSC_TOKEN)
        self.assertIsNone(impact)

    @unittest.skipUnless(HAS_SAFETY_ADDRESS, "safety._address not importable")
    def test_gate_gets_an_eip55_checksummed_bsc_address(self):
        # caps canonicalises EVM addresses to lowercase; safety.gate's
        # 02_address_format check rejects all-lowercase on purpose.  Handing it
        # the canonical form would fail every BSC screen — after the withdrawal.
        self.orch().run_once("bsc", BSC_TOKEN, 25.0)
        self.assertEqual(self.gate.calls[0][1], BSC_TOKEN_EIP55)


class TestLiveMode(Fixture):
    def test_live_run_moves_money_and_records_spend(self):
        orch = self.orch(live=True)
        self.assertTrue(orch.armed)
        self.assertFalse(orch.dry_run)
        result = orch.run_once("bsc", BSC_TOKEN, 25.0)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.payout.kinds(), ["transfer", "withdraw"])
        self.assertTrue(all(not c["dry_run"] for c in self.payout.calls))
        self.assertEqual(self.payout.calls[1]["address"], BSC_WITHDRAW)
        self.assertEqual(self.payout.calls[1]["allowlist"], [BSC_WITHDRAW])
        self.assertEqual(self.state.spent_last_24h(), 25.0)
        self.assertGreaterEqual(self.probe.calls, 2)  # baseline + arrival poll

    def test_arrival_timeout_aborts_before_swap(self):
        self.probe = FakeProbe(values=(0.0,))  # money never shows up
        orch = self.orch(live=True)
        result = orch.run_once("bsc", BSC_TOKEN, 25.0)
        self.assertEqual(result.status, "arrival_timeout")
        self.assertEqual(len(self.bsc.calls), 0)
        self.assertEqual(len(self.gate.calls), 0)

    def test_swap_spends_only_what_actually_arrived(self):
        # 25 requested, the exchange keeps 1 USDT of withdrawal fee: the router
        # must be asked for 24, not 25 (which would revert, or overspend a
        # wallet that happens to hold unrelated USDT).
        self.probe = FakeProbe(values=(0.0, 24.0))
        orch = self.orch(live=True)
        result = orch.run_once("bsc", BSC_TOKEN, 25.0)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.bsc.calls[0]["usdt_amount"], 24.0)
        self.assertEqual(self.payout.calls[1]["amount"], 25.0)  # withdrawal is pre-fee
        self.assertEqual(self.state.spent_last_24h(), 25.0)  # cap counts the pre-fee sum

    def test_missing_balance_probe_fails_closed(self):
        deps = Deps(
            payout=self.payout, bsc_buy=self.bsc, solana_buy=self.sol,
            screen_token=self.gate, balance_probe=None,
        )
        orch = MemeBuyOrchestrator(
            self.live_cfg(), state=self.state, deps=deps, env=ARMED_ENV,
            clock=self.clock.time, sleep=self.clock.sleep,
        )
        self.payout.live_allowed = True
        result = orch.run_once("bsc", BSC_TOKEN, 25.0)
        # No chains.bsc_swap.get_usdt_balance exists -> arrival unprovable -> stop.
        self.assertFalse(result.ok)
        self.assertEqual(len(self.bsc.calls), 0)


class TestIdempotency(Fixture):
    def test_crash_after_transfer_does_not_resend(self):
        self.payout.transfer_error = RuntimeError("connection reset after send")
        orch = self.orch(live=True)
        first = orch.run_once("bsc", BSC_TOKEN, 25.0, client_oid="fixed-oid")
        self.assertFalse(first.ok)
        row = self.state.find_run_by_client_oid("fixed-oid")
        self.assertEqual(row["transfer_client_oid"], "fixed-oid-t")  # persisted pre-call
        self.assertIsNotNone(row["committed_at"])

        # Restart: fresh state object, fresh orchestrator, same DB and same oid.
        self.payout.transfer_error = None
        resumed_state = MemeBuyState(self.db_path)
        orch2 = MemeBuyOrchestrator(
            self.live_cfg(), state=resumed_state, deps=self.deps(), env=ARMED_ENV,
            clock=self.clock.time, sleep=self.clock.sleep,
        )
        second = orch2.run_once("bsc", BSC_TOKEN, 25.0, client_oid="fixed-oid")
        self.assertEqual(second.status, "resume_blocked")
        self.assertEqual(self.payout.kinds().count("transfer"), 1)  # never resent
        self.assertEqual(resumed_state.spent_last_24h(), 25.0)  # counted exactly once

    def test_resume_after_withdrawal_looks_status_up(self):
        self.payout.withdraw_error = RuntimeError("timeout waiting for response")
        orch = self.orch(live=True)
        orch.run_once("bsc", BSC_TOKEN, 25.0, client_oid="oid-w")
        self.assertEqual(self.payout.kinds().count("withdraw"), 1)

        self.payout.withdraw_error = None
        second = self.orch(live=True).run_once("bsc", BSC_TOKEN, 25.0, client_oid="oid-w")
        self.assertIn("status", self.payout.kinds())
        self.assertEqual(self.payout.kinds().count("withdraw"), 1)  # not resent
        self.assertTrue(second.ok, second.reason)
        self.assertEqual(len(self.bsc.calls), 1)

    def test_duplicate_client_oid_error_is_not_a_retry(self):
        err = RuntimeError("bitget error 43118: clientOid duplicate")
        err.code = "43118"
        self.payout.transfer_error = err
        result = self.orch(live=True).run_once("bsc", BSC_TOKEN, 25.0)
        # 43118 means already accepted: continue, never resend.
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.payout.kinds().count("transfer"), 1)

    def test_client_oid_cannot_be_reused_for_another_target(self):
        orch = self.orch()
        orch.run_once("bsc", BSC_TOKEN, 25.0, client_oid="shared-oid")
        calls_before = len(self.payout.calls)
        result = orch.run_once("solana", SOL_MINT, 25.0, client_oid="shared-oid")
        self.assertEqual(result.status, "resume_blocked")
        self.assertEqual(len(self.payout.calls), calls_before)

    def test_disarmed_process_refuses_to_resume_a_live_run(self):
        # A live run dies at the transfer stage (payout/ not deployed yet), so
        # the row sits with dry_run=0.  The operator then restarts WITHOUT
        # MEME_BUY_ARMED and retries the same oid: the resumed run must not
        # inherit the row's live mode and start moving real money.
        deps = Deps(payout=None, bsc_buy=self.bsc, solana_buy=self.sol,
                    screen_token=self.gate, balance_probe=self.probe)
        live = MemeBuyOrchestrator(
            self.live_cfg(), state=self.state, deps=deps, env=ARMED_ENV,
            clock=self.clock.time, sleep=self.clock.sleep,
        )
        live.run_once("bsc", BSC_TOKEN, 25.0, client_oid="mode-oid")
        row = self.state.find_run_by_client_oid("mode-oid")
        self.assertEqual(row["dry_run"], 0)
        # The oid is journalled BEFORE the send attempt (see
        # test_crash_after_transfer_does_not_resend), so it is set even though
        # payout was absent and nothing left the process.  That the send never
        # happened is enforced by the fake, which records every call.
        self.assertEqual(self.payout.calls, [])

        disarmed = self.orch()  # env={}, dry config -> dry_run property True
        self.assertTrue(disarmed.dry_run)
        result = disarmed.run_once("bsc", BSC_TOKEN, 25.0, client_oid="mode-oid")
        self.assertEqual(result.status, "resume_blocked")
        self.assertEqual(self.payout.calls, [])
        self.assertEqual(self.bsc.calls, [])

    def test_cross_mode_resume_is_refused_even_when_nothing_was_sent(self):
        # Complements the test above.  There the resume is stopped by the
        # already-submitted guard, so it never reaches the mode decision.  Here
        # the live row has nothing submitted, so that guard cannot fire -- and
        # the resume must STILL be refused, because the row is live and this
        # process is not.  Refusing outright is safer than resuming in either
        # mode: it never continues a live run unattended, and never reports a
        # simulation as if it had completed the live one.
        self.state.begin_run("unsent-oid", "bsc", BSC_TOKEN, 25.0, dry_run=False)
        row = self.state.find_run_by_client_oid("unsent-oid")
        self.assertEqual(row["dry_run"], 0)
        self.assertIsNone(row["transfer_client_oid"])

        disarmed = self.orch()  # env={}, dry config -> dry_run property True
        self.assertTrue(disarmed.dry_run)
        result = disarmed.run_once("bsc", BSC_TOKEN, 25.0, client_oid="unsent-oid")

        self.assertFalse(result.ok)
        self.assertIn("mode", result.reason)
        self.assertEqual(self.payout.calls, [])
        self.assertEqual(self.bsc.calls, [])
        self.assertEqual(self.state.spent_last_24h(), 0.0)

    def test_same_mode_resume_proceeds_and_stays_in_that_mode(self):
        # The permitted resume path: row and process agree on the mode.  Every
        # downstream call must carry dry_run=True -- the fakes raise on a live
        # call, so a mode leak fails here rather than in production.
        self.state.begin_run("dry-oid", "bsc", BSC_TOKEN, 25.0, dry_run=True)
        disarmed = self.orch()
        result = disarmed.run_once("bsc", BSC_TOKEN, 25.0, client_oid="dry-oid")

        self.assertTrue(result.ok, result.reason)
        self.assertTrue(self.payout.calls, "the resume should have proceeded")
        for call in self.payout.calls + self.bsc.calls:
            self.assertTrue(call["dry_run"], call)
        self.assertEqual(self.state.spent_last_24h(), 0.0)  # simulation spends nothing

    def test_swap_is_not_replayed_on_resume(self):
        # The swap response is lost (timeout after broadcast).  The tx may be in
        # a block; there is no per-oid swap lookup, so a resume must not repeat it.
        self.bsc.live_allowed = True
        self.bsc.error = RuntimeError("read timeout after broadcast")
        first = self.orch(live=True).run_once("bsc", BSC_TOKEN, 25.0, client_oid="swap-oid")
        self.assertFalse(first.ok)
        self.assertEqual(len(self.bsc.calls), 1)

        self.bsc.error = None
        self.probe = FakeProbe(values=(0.0, 1000.0))
        second = self.orch(live=True).run_once("bsc", BSC_TOKEN, 25.0, client_oid="swap-oid")
        self.assertEqual(second.status, "resume_blocked")
        self.assertEqual(len(self.bsc.calls), 1)  # never a second trade

    def test_completed_run_is_not_replayed(self):
        orch = self.orch()
        first = orch.run_once("bsc", BSC_TOKEN, 25.0, client_oid="done-oid")
        self.assertTrue(first.ok)
        calls_before = len(self.payout.calls)
        second = orch.run_once("bsc", BSC_TOKEN, 25.0, client_oid="done-oid")
        self.assertEqual(second.status, "already_completed")
        self.assertEqual(len(self.payout.calls), calls_before)


class TestContainment(Fixture):
    def test_broken_sibling_does_not_propagate(self):
        deps = Deps(payout=object(), screen_token=self.gate)  # payout lacks methods
        orch = MemeBuyOrchestrator(self.cfg(), state=self.state, deps=deps, env={})
        result = orch.run_once("bsc", BSC_TOKEN, 25.0)  # must not raise
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "failed")

    def test_swap_exception_does_not_propagate(self):
        self.bsc.error = ValueError("router reverted")
        result = self.orch().run_once("bsc", BSC_TOKEN, 25.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.state.get_run(result.run_id)["status"], "failed")

    def test_swap_signature_without_dry_run_is_refused(self):
        def buy_token(token_address, usdt_amount, slippage_bps):  # no dry_run!
            raise AssertionError("must never be called")

        deps = Deps(
            payout=self.payout, bsc_buy=buy_token, screen_token=self.gate,
            balance_probe=self.probe,
        )
        orch = MemeBuyOrchestrator(self.cfg(), state=self.state, deps=deps, env={})
        result = orch.run_once("bsc", BSC_TOKEN, 25.0)
        self.assertFalse(result.ok)
        self.assertIn("dry_run", result.reason)

    def test_swap_requiring_key_material_is_refused(self):
        def buy_token(w3, key, token_address, usdt_amount, slippage_bps, dry_run=True):
            raise AssertionError("must never be called")

        deps = Deps(
            payout=self.payout, bsc_buy=buy_token, screen_token=self.gate,
            balance_probe=self.probe,
        )
        orch = MemeBuyOrchestrator(self.cfg(), state=self.state, deps=deps, env={})
        result = orch.run_once("bsc", BSC_TOKEN, 25.0)
        self.assertFalse(result.ok)
        self.assertIn("w3", result.reason)

    def test_swap_absorbing_dry_run_in_var_kwargs_is_refused(self):
        # `**kwargs` accepts dry_run and proves nothing about honouring it.
        sent = []

        def buy_token(token_address, usdt_amount, **kwargs):
            sent.append(kwargs)  # dry_run lands here and is ignored -> live trade
            return {"tx_hash": "0xreal"}

        deps = Deps(
            payout=self.payout, bsc_buy=buy_token, screen_token=self.gate,
            balance_probe=self.probe,
        )
        orch = MemeBuyOrchestrator(self.cfg(), state=self.state, deps=deps, env={})
        result = orch.run_once("bsc", BSC_TOKEN, 25.0)
        self.assertFalse(result.ok)
        self.assertIn("dry_run", result.reason)
        self.assertEqual(sent, [])

    def test_var_kwargs_is_fine_when_dry_run_is_a_real_parameter(self):
        seen = {}

        def buy_token(token_address, usdt_amount, slippage_bps, dry_run=True, **extra):
            seen.update(dry_run=dry_run, extra=extra)
            return {"tx_hash": "0xabc"}

        deps = Deps(
            payout=self.payout, bsc_buy=buy_token, screen_token=self.gate,
            balance_probe=self.probe,
        )
        orch = MemeBuyOrchestrator(self.cfg(), state=self.state, deps=deps, env={})
        result = orch.run_once("bsc", BSC_TOKEN, 25.0)
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(seen["dry_run"])
        self.assertIn("rpc_url", seen["extra"])  # the tail still gets the hints

    def test_uninspectable_swap_callable_is_refused(self):
        deps = Deps(
            payout=self.payout, bsc_buy=len, screen_token=self.gate,
            balance_probe=self.probe,
        )
        orch = MemeBuyOrchestrator(self.cfg(), state=self.state, deps=deps, env={})
        result = orch.run_once("bsc", BSC_TOKEN, 25.0)
        self.assertFalse(result.ok)

    def test_rpc_url_credentials_are_never_logged(self):
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("memebuy.orchestrator")
        handler = Capture()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.removeHandler, handler)

        cfg = self.cfg(chains_over={
            "bsc": {"rpc_url": "https://bsc-mainnet.example/v2/SUPER-SECRET-KEY"}
        })
        MemeBuyOrchestrator(
            cfg, state=self.state, deps=self.deps(), env={}
        ).run_once("bsc", BSC_TOKEN, 25.0)
        joined = "\n".join(records)
        self.assertNotIn("SUPER-SECRET-KEY", joined)
        self.assertIn("bsc-mainnet.example", joined)  # still identifiable

    def test_swap_kwargs_are_filtered_to_the_signature(self):
        seen = {}

        def buy_token(token_address, usdt_amount, slippage_bps, recipient, dry_run=True):
            seen.update(locals())
            return {"tx_hash": "0xabc"}

        deps = Deps(
            payout=self.payout, bsc_buy=buy_token, screen_token=self.gate,
            balance_probe=self.probe,
        )
        orch = MemeBuyOrchestrator(self.cfg(), state=self.state, deps=deps, env={})
        result = orch.run_once("bsc", BSC_TOKEN, 25.0)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(seen["token_address"], BSC_TOKEN)
        self.assertEqual(seen["recipient"], BSC_WITHDRAW)
        self.assertTrue(seen["dry_run"])

    def test_module_level_run_once_never_raises(self):
        result = orchestrator.run_once(
            self.cfg(), "bsc", BSC_TOKEN, 25.0, state=self.state, deps=self.deps(), env={}
        )
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.dry_run)


# ---------- pluggable swap backend ----------


class TestSwapBackendConfig(unittest.TestCase):
    def test_default_backend_is_native(self):
        cfg = config.load_meme_buy_config(raw_config())
        self.assertEqual(cfg.chain("bsc").swap_backend, config.SWAP_BACKEND_NATIVE)
        self.assertFalse(cfg.chain("bsc").gasless)

    def test_bitget_backend_is_accepted(self):
        cfg = config.load_meme_buy_config(
            raw_config(chains_over={"bsc": {"swap_backend": "  BitGet ", "gasless": True}})
        )
        self.assertEqual(cfg.chain("bsc").swap_backend, config.SWAP_BACKEND_BITGET)
        self.assertTrue(cfg.chain("bsc").gasless)

    def test_unknown_backend_is_rejected(self):
        for value in ("uniswap", "", "NATIVE ROUTER", 1, None, True):
            with self.assertRaises(ConfigError):
                config.load_meme_buy_config(
                    raw_config(chains_over={"bsc": {"swap_backend": value}})
                )

    def test_gasless_with_native_backend_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            config.load_meme_buy_config(raw_config(chains_over={"bsc": {"gasless": True}}))
        self.assertIn("gasless", str(ctx.exception))
        # Explicit native is just as wrong as the default.
        with self.assertRaises(ConfigError):
            config.load_meme_buy_config(
                raw_config(chains_over={"solana": {"swap_backend": "native", "gasless": True}})
            )

    def test_gasless_must_be_a_boolean(self):
        with self.assertRaises(ConfigError):
            config.load_meme_buy_config(
                raw_config(chains_over={"bsc": {"swap_backend": "bitget", "gasless": "yes"}})
            )

    def test_backend_is_validated_even_on_a_disabled_chain(self):
        # A typo must surface now, not on the day somebody flips enabled=true.
        with self.assertRaises(ConfigError):
            config.load_meme_buy_config(
                raw_config(chains_over={"solana": {"enabled": False, "swap_backend": "jupyter"}})
            )


class TestCredentialPrefixConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = config.load_meme_buy_config(raw_config())
        self.assertEqual(cfg.bitget_sub_prefix, "BITGET_SUB")
        self.assertEqual(cfg.bitget_main_prefix, "BITGET_MAIN")

    def test_custom_prefixes(self):
        cfg = config.load_meme_buy_config(
            raw_config(bitget_sub_prefix="MINAS_SUB1", bitget_main_prefix="MINAS_MAIN")
        )
        self.assertEqual(cfg.bitget_sub_prefix, "MINAS_SUB1")
        self.assertEqual(cfg.bitget_main_prefix, "MINAS_MAIN")

    def test_rejects_prefixes_that_are_not_env_var_names(self):
        for bad in ("lower", "HAS SPACE", "9LEADING", "", None, 7):
            with self.assertRaises(ConfigError):
                config.load_meme_buy_config(raw_config(bitget_main_prefix=bad))

    def test_rejects_one_key_playing_both_roles(self):
        with self.assertRaises(ConfigError):
            config.load_meme_buy_config(
                raw_config(bitget_sub_prefix="BITGET_X", bitget_main_prefix="BITGET_X")
            )


class TestSwapBackendSelection(Fixture):
    def setUp(self):
        super().setUp()
        self.bitget = FakeBitgetSwap("tx_hash")

    def deps(self):
        return Deps(
            payout=self.payout,
            bsc_buy=self.bsc,
            solana_buy=self.sol,
            bitget_buy=self.bitget,
            screen_token=self.gate,
            balance_probe=self.probe,
        )

    def test_native_backend_uses_the_native_router(self):
        result = self.orch().run_once("bsc", BSC_TOKEN, 25.0)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(len(self.bsc.calls), 1)
        self.assertEqual(len(self.bitget.calls), 0)
        # The native routers know nothing about these two keywords.
        self.assertNotIn("chain", self.bsc.calls[0])
        self.assertNotIn("gasless", self.bsc.calls[0])

    def test_bitget_backend_uses_the_bitget_router(self):
        cfg = self.cfg(chains_over={"bsc": {"swap_backend": "bitget", "gasless": True}})
        result = MemeBuyOrchestrator(
            cfg, state=self.state, deps=self.deps(), env={}
        ).run_once("bsc", BSC_TOKEN, 25.0)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(len(self.bitget.calls), 1)
        self.assertEqual(len(self.bsc.calls), 0)
        call = self.bitget.calls[0]
        self.assertEqual(call["chain"], "bsc")
        self.assertTrue(call["gasless"])
        self.assertEqual(call["token_address"], BSC_TOKEN)
        self.assertTrue(call["dry_run"])

    def test_backend_is_per_chain(self):
        cfg = self.cfg(chains_over={"solana": {"swap_backend": "bitget"}})
        orch = MemeBuyOrchestrator(cfg, state=self.state, deps=self.deps(), env={})
        self.assertTrue(orch.run_once("bsc", BSC_TOKEN, 5.0, client_oid="a").ok)
        self.assertTrue(orch.run_once("solana", SOL_MINT, 5.0, client_oid="b").ok)
        self.assertEqual(len(self.bsc.calls), 1)  # native chain untouched
        self.assertEqual(len(self.bitget.calls), 1)
        self.assertEqual(self.bitget.calls[0]["chain"], "solana")
        self.assertEqual(self.bitget.calls[0]["mint"], SOL_MINT)
        self.assertFalse(self.bitget.calls[0]["gasless"])

    def test_missing_bitget_module_is_a_clean_abort_not_a_crash(self):
        # chains/bitget_swap.py is a separate deliverable and may not be
        # deployed.  Its absence must abort this run, never fall back to
        # another router and never take the process down.
        self.addCleanup(sys.modules.pop, "chains.bitget_swap", None)
        sys.modules["chains.bitget_swap"] = None  # makes the import raise
        cfg = self.cfg(chains_over={"bsc": {"swap_backend": "bitget"}})
        deps = Deps(payout=self.payout, bsc_buy=self.bsc, screen_token=self.gate,
                    balance_probe=self.probe)
        result = MemeBuyOrchestrator(cfg, state=self.state, deps=deps, env={}).run_once(
            "bsc", BSC_TOKEN, 25.0
        )
        self.assertFalse(result.ok)
        self.assertIn("bitget_swap", result.reason)
        self.assertEqual(len(self.bsc.calls), 0)  # no silent fallback

    def test_an_unrecognised_backend_aborts_instead_of_using_the_native_router(self):
        # REGRESSION: _swap_fn only special-cased the exact string "bitget" and
        # let everything else fall through to the NATIVE (live) router, so a
        # hand-built ChainConfig (both it and SWAP_BACKENDS are public API) with
        # "BITGET" or a typo silently routed the trade to PancakeSwap/Jupiter.
        import dataclasses

        base = self.cfg()
        for bogus in ("BITGET", "bitgt", "jupiter", "native "):
            with self.subTest(bogus):
                broken = dataclasses.replace(base.chains["bsc"], swap_backend=bogus)
                cfg = dataclasses.replace(base, chains=dict(base.chains, bsc=broken))
                result = MemeBuyOrchestrator(
                    cfg, state=MemeBuyState(os.path.join(self.tmp.name, "u.sqlite3")),
                    deps=self.deps(), env={},
                ).run_once("bsc", BSC_TOKEN, 25.0, client_oid="u-%s" % bogus.strip())
                self.assertFalse(result.ok)
                self.assertIn("refusing to guess a router", result.reason)
                self.assertEqual(len(self.bsc.calls), 0)
                self.assertEqual(len(self.bitget.calls), 0)

    def test_bitget_module_is_imported_lazily_when_present(self):
        import types

        module = types.ModuleType("chains.bitget_swap")
        recorded = {}

        def buy_token(usdt_amount, slippage_bps, dry_run=True, chain=None,
                      token_address=None, mint=None, gasless=False, **extra):
            recorded.update(locals())
            return {"backend": "bitget", "tx_hash": "0xswapx"}

        module.buy_token = buy_token
        sys.modules["chains.bitget_swap"] = module
        self.addCleanup(sys.modules.pop, "chains.bitget_swap", None)

        cfg = self.cfg(chains_over={"bsc": {"swap_backend": "bitget", "gasless": True}})
        deps = Deps(payout=self.payout, screen_token=self.gate, balance_probe=self.probe)
        result = MemeBuyOrchestrator(cfg, state=self.state, deps=deps, env={}).run_once(
            "bsc", BSC_TOKEN, 25.0
        )
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(recorded["chain"], "bsc")
        self.assertTrue(recorded["gasless"])
        self.assertTrue(recorded["dry_run"])

    def test_real_bitget_swap_signature_accepts_what_we_send(self):
        # Contract guard: chains/bitget_swap.py is written independently, so
        # catch a keyword rename here rather than at stage 5, after the
        # irreversible withdrawal.  No call is made — only binding is checked.
        try:
            from chains import bitget_swap
        except Exception:  # pragma: no cover - module not deployed here
            self.skipTest("chains.bitget_swap is not importable in this environment")
        cfg = self.cfg(chains_over={"bsc": {"swap_backend": "bitget", "gasless": True}})
        orch = MemeBuyOrchestrator(cfg, state=self.state, deps=self.deps(), env={})
        wanted = dict(
            usdt_amount=25.0, slippage_bps=300, rpc_url="https://x.example/",
            min_native_balance=0.0, dry_run=True, token_address=BSC_TOKEN,
            recipient=BSC_WITHDRAW, chain="bsc", gasless=True,
        )
        bound = orch._bind_swap_kwargs(
            bitget_swap.buy_token, wanted, "bsc", "chains.bitget_swap.buy_token"
        )
        for key in ("usdt_amount", "slippage_bps", "dry_run", "chain", "gasless",
                    "token_address"):
            self.assertIn(key, bound, "bitget_swap.buy_token dropped %r" % key)
        self.assertTrue(bound["dry_run"])

    def test_bitget_backend_still_must_prove_it_honours_dry_run(self):
        # Switching backends must not become a way around the dry_run guard.
        def buy_token(usdt_amount, slippage_bps, chain=None, **kwargs):  # no dry_run!
            raise AssertionError("must never be called")

        cfg = self.cfg(chains_over={"bsc": {"swap_backend": "bitget"}})
        deps = Deps(payout=self.payout, bitget_buy=buy_token, screen_token=self.gate,
                    balance_probe=self.probe)
        result = MemeBuyOrchestrator(cfg, state=self.state, deps=deps, env={}).run_once(
            "bsc", BSC_TOKEN, 25.0
        )
        self.assertFalse(result.ok)
        self.assertIn("dry_run", result.reason)
        self.assertIn("chains.bitget_swap.buy_token", result.reason)


# ---------- two-key Bitget credential model ----------


@contextlib.contextmanager
def patched_payout_module(test, payout_cls):
    """Swap payout.bitget_payout for a double, so no test ever signs or dials out."""
    module = types.ModuleType("payout.bitget_payout")
    module.BitgetPayout = payout_cls
    real = sys.modules.get("payout.bitget_payout")
    sys.modules["payout.bitget_payout"] = module
    try:
        yield module
    finally:
        if real is not None:
            sys.modules["payout.bitget_payout"] = real
        else:
            sys.modules.pop("payout.bitget_payout", None)


def write_credentials(directory, prefix, secret="SUPER-SECRET-VALUE"):
    """Three credential files; returns the env mapping that points at them."""
    env = {}
    for suffix in ("API_KEY_FILE", "API_SECRET_FILE", "API_PASSPHRASE_FILE"):
        path = os.path.join(directory, "%s_%s" % (prefix, suffix))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("%s-%s" % (secret, suffix))
        env["%s_%s" % (prefix, suffix)] = path
    return env


class TestTwoKeyPayout(Fixture):
    def setUp(self):
        super().setUp()
        self.sub = RolePayout("sub")
        self.main = RolePayout("main")

    def deps(self):
        return Deps(
            payout_sub=self.sub,
            payout_main=self.main,
            bsc_buy=self.bsc,
            solana_buy=self.sol,
            screen_token=self.gate,
            balance_probe=self.probe,
        )

    def test_sub_key_transfers_and_main_key_withdraws(self):
        result = self.orch().run_once("bsc", BSC_TOKEN, 25.0)
        self.assertTrue(result.ok, result.reason)
        self.assertIsNot(self.sub, self.main)  # two DIFFERENT credentials
        self.assertEqual(self.sub.kinds(), ["transfer"])
        self.assertEqual(self.main.kinds(), ["withdraw"])
        self.assertEqual(self.sub.account_role, "sub")
        self.assertEqual(self.main.account_role, "main")

    def test_live_run_keeps_the_roles_apart(self):
        self.sub.live_allowed = True
        self.main.live_allowed = True
        self.bsc.live_allowed = True
        orch = MemeBuyOrchestrator(
            self.live_cfg(), state=self.state, deps=self.deps(), env=ARMED_ENV,
            clock=self.clock.time, sleep=self.clock.sleep,
        )
        result = orch.run_once("bsc", BSC_TOKEN, 25.0)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.sub.kinds(), ["transfer"])
        self.assertEqual(self.main.kinds(), ["withdraw"])

    def test_a_sub_key_in_the_main_slot_is_refused_before_withdrawing(self):
        deps = self.deps()
        deps.payout_main = RolePayout("sub")  # the wrong credential entirely
        result = MemeBuyOrchestrator(
            self.cfg(), state=self.state, deps=deps, env={}
        ).run_once("bsc", BSC_TOKEN, 25.0)
        self.assertFalse(result.ok)
        self.assertIn("not interchangeable", result.reason)
        self.assertEqual(deps.payout_main.calls, [])  # never called

    def test_a_role_mix_up_is_refused_at_stage_0_before_the_transfer(self):
        # REGRESSION: the role was only checked when stage 2 asked for the MAIN
        # client, i.e. AFTER stage 1 had already moved real money.  One object
        # standing in for both roles must be refused before anything is sent.
        one_key = RolePayout("sub", live_allowed=True)
        self.bsc.live_allowed = True
        result = MemeBuyOrchestrator(
            self.live_cfg(), state=self.state,
            deps=Deps(payout=one_key, bsc_buy=self.bsc, screen_token=self.gate,
                      balance_probe=self.probe),
            env=ARMED_ENV, clock=self.clock.time, sleep=self.clock.sleep,
        ).run_once("bsc", BSC_TOKEN, 25.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, orchestrator.STAGE_SELECT)
        self.assertIn("not interchangeable", result.reason)
        self.assertEqual(one_key.calls, [])  # nothing moved, not even stage 1

    def test_both_roles_are_resolved_before_stage_1_even_in_a_dry_run(self):
        deps = self.deps()
        deps.payout_sub = RolePayout("main")  # swapped by mistake
        result = MemeBuyOrchestrator(
            self.cfg(), state=self.state, deps=deps, env={}
        ).run_once("bsc", BSC_TOKEN, 25.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, orchestrator.STAGE_SELECT)
        self.assertEqual(deps.payout_sub.calls, [])
        self.assertEqual(self.main.calls, [])
        self.assertEqual(len(self.bsc.calls), 0)

    def test_a_main_key_in_the_sub_slot_is_refused_before_transferring(self):
        deps = self.deps()
        deps.payout_sub = RolePayout("main")
        result = MemeBuyOrchestrator(
            self.cfg(), state=self.state, deps=deps, env={}
        ).run_once("bsc", BSC_TOKEN, 25.0)
        self.assertFalse(result.ok)
        self.assertEqual(deps.payout_sub.calls, [])
        self.assertEqual(self.main.calls, [])  # never got as far as stage 2

    def test_production_builds_two_clients_from_two_prefixes(self):
        built = []

        class Recorder:
            @staticmethod
            def from_env(account_role, prefix, **kwargs):
                built.append(dict(role=account_role, prefix=prefix, **kwargs))
                return RolePayout(account_role, live_allowed=True)

        cfg = self.live_cfg(bitget_sub_prefix="MINAS_SUB", bitget_main_prefix="MINAS_MAIN")
        env = dict(ARMED_ENV)
        env.update(write_credentials(self.tmp.name, "MINAS_SUB"))
        env.update(write_credentials(self.tmp.name, "MINAS_MAIN"))
        deps = Deps(bsc_buy=self.bsc, screen_token=self.gate, balance_probe=self.probe)
        self.bsc.live_allowed = True
        with patched_payout_module(self, Recorder):
            result = MemeBuyOrchestrator(
                cfg, state=self.state, deps=deps, env=env,
                clock=self.clock.time, sleep=self.clock.sleep,
            ).run_once("bsc", BSC_TOKEN, 25.0)

        self.assertTrue(result.ok, result.reason)
        self.assertEqual([b["role"] for b in built], ["sub", "main"])
        self.assertEqual([b["prefix"] for b in built], ["MINAS_SUB", "MINAS_MAIN"])
        # payout/ refuses to withdraw without a per-call ceiling; it is the cap.
        self.assertEqual(built[1]["max_withdrawal"], cfg.per_trade_usdt_cap)
        self.assertNotIn("max_withdrawal", built[0])


class TestMainCredentialPrecheck(Fixture):
    """Missing MAIN credentials must fail BEFORE stage 1, not between 1 and 2."""

    def test_live_run_without_main_credentials_never_transfers(self):
        deps = Deps(payout_sub=self.payout, bsc_buy=self.bsc, screen_token=self.gate,
                    balance_probe=self.probe)
        env = dict(ARMED_ENV)  # no BITGET_MAIN_* at all
        result = MemeBuyOrchestrator(
            self.live_cfg(), state=self.state, deps=deps, env=env,
            clock=self.clock.time, sleep=self.clock.sleep,
        ).run_once("bsc", BSC_TOKEN, 25.0)

        self.assertFalse(result.ok)
        self.assertIn("BITGET_MAIN_API_KEY_FILE", result.reason)
        self.assertEqual(self.payout.calls, [])  # stage 1 never ran
        self.assertEqual(self.state.spent_last_24h(), 0.0)

    def test_unreadable_credential_file_is_not_treated_as_present(self):
        env = dict(ARMED_ENV)
        env.update(write_credentials(self.tmp.name, "BITGET_MAIN"))
        env["BITGET_MAIN_API_SECRET_FILE"] = os.path.join(self.tmp.name, "no-such-file")
        deps = Deps(payout_sub=self.payout, bsc_buy=self.bsc, screen_token=self.gate,
                    balance_probe=self.probe)
        result = MemeBuyOrchestrator(
            self.live_cfg(), state=self.state, deps=deps, env=env,
            clock=self.clock.time, sleep=self.clock.sleep,
        ).run_once("bsc", BSC_TOKEN, 25.0)
        self.assertFalse(result.ok)
        self.assertIn("BITGET_MAIN_API_SECRET_FILE", result.reason)
        self.assertEqual(self.payout.calls, [])

    def test_dry_run_needs_no_credentials(self):
        # A simulation signs nothing, so it must not demand key files — and it
        # must not call from_env(), which would fail on a box with no secrets.
        built = []

        class Recorder:
            def __init__(self, account_role="sub", **kwargs):
                built.append(dict(via="ctor", role=account_role, **kwargs))
                self.account_role = account_role

            @staticmethod
            def from_env(account_role, prefix, **kwargs):
                raise AssertionError("a dry run must not read credential files")

            def transfer_sub_to_main(self, coin, amount, client_oid, dry_run):
                assert dry_run and self.account_role == "sub"
                return {"transferId": "T-1"}

            def withdraw_onchain(self, coin, chain, address, amount, client_oid,
                                 allowlist, dry_run):
                assert dry_run and self.account_role == "main"
                return {"orderId": "W-1"}

        with patched_payout_module(self, Recorder):
            deps = Deps(bsc_buy=self.bsc, screen_token=self.gate, balance_probe=self.probe)
            result = MemeBuyOrchestrator(
                self.cfg(), state=self.state, deps=deps, env={}
            ).run_once("bsc", BSC_TOKEN, 25.0)

        self.assertTrue(result.ok, result.reason)
        self.assertEqual([b["role"] for b in built], ["sub", "main"])

    def test_custom_prefix_is_the_one_checked(self):
        deps = Deps(payout_sub=self.payout, bsc_buy=self.bsc, screen_token=self.gate,
                    balance_probe=self.probe)
        cfg = self.live_cfg(bitget_main_prefix="MINAS_MAIN")
        result = MemeBuyOrchestrator(
            cfg, state=self.state, deps=deps, env=dict(ARMED_ENV),
            clock=self.clock.time, sleep=self.clock.sleep,
        ).run_once("bsc", BSC_TOKEN, 25.0)
        self.assertIn("MINAS_MAIN_API_KEY_FILE", result.reason)


class TestCredentialInspection(unittest.TestCase):
    def test_never_opens_the_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = write_credentials(tmp, "BITGET_MAIN", secret="LEAKME")
            creds = credentials.inspect_role("main", "BITGET_MAIN", env)
            self.assertTrue(creds.complete)
            self.assertEqual(creds.missing, ())
            blob = repr(creds) + " " + " ".join(f.describe() for f in creds.files)
            self.assertNotIn("LEAKME", blob)

    def test_unset_env_var_is_incomplete(self):
        creds = credentials.inspect_role("sub", "BITGET_SUB", {})
        self.assertFalse(creds.complete)
        self.assertEqual(len(creds.missing), 3)

    def test_roles_state_what_they_may_do(self):
        sub = credentials.inspect_role("sub", "BITGET_SUB", {})
        main = credentials.inspect_role("main", "BITGET_MAIN", {})
        self.assertIn("transfer", " ".join(sub.may()))
        self.assertIn("withdraw", " ".join(sub.may_not()))
        self.assertIn("withdraw", " ".join(main.may()))
        self.assertIn("transfer", " ".join(main.may_not()))

    def test_unknown_role_is_rejected(self):
        with self.assertRaises(ValueError):
            credentials.inspect_role("parent", "BITGET_MAIN", {})


class TestCheckCredentialsCli(unittest.TestCase):
    """--check-credentials: no secrets on stdout, and provably no network."""

    SECRET = "DO-NOT-PRINT-THIS-KEY"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = os.path.join(self.tmp.name, "config.json")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(raw_config(kill_switch_file="/tmp/stop"), handle)

        # Any socket use at all fails the test: this command must be offline.
        import socket

        def no_network(*args, **kwargs):
            raise AssertionError("--check-credentials made a network call")

        for name in ("socket", "create_connection"):
            patched = unittest.mock.patch.object(socket, name, no_network)
            patched.start()
            self.addCleanup(patched.stop)

    def _run(self, env):
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = memebuy_cli.main(["--config", self.config_path, "--check-credentials"])
        return code, buffer.getvalue()

    def test_reports_readable_files_without_printing_any_secret(self):
        env = {}
        env.update(write_credentials(self.tmp.name, "BITGET_SUB", self.SECRET))
        env.update(write_credentials(self.tmp.name, "BITGET_MAIN", self.SECRET))
        code, out = self._run(env)

        self.assertEqual(code, 0)
        self.assertNotIn(self.SECRET, out)
        self.assertIn("BITGET_SUB_API_KEY_FILE", out)
        self.assertIn("BITGET_MAIN_API_PASSPHRASE_FILE", out)
        self.assertIn("READY", out)
        # It must say plainly what each role is allowed to do.
        self.assertIn("may NOT", out)
        self.assertIn("IRREVERSIBLE", out)

    def test_missing_credentials_are_named_and_exit_nonzero(self):
        env = write_credentials(self.tmp.name, "BITGET_SUB", self.SECRET)
        code, out = self._run(env)
        self.assertEqual(code, 1)
        self.assertNotIn(self.SECRET, out)
        self.assertIn("INCOMPLETE", out)
        self.assertIn("BITGET_MAIN_API_KEY_FILE", out)

    def test_honours_configured_prefixes(self):
        raw = raw_config(
            kill_switch_file="/tmp/stop",
            bitget_sub_prefix="MINAS_SUB",
            bitget_main_prefix="MINAS_MAIN",
        )
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(raw, handle)
        code, out = self._run({})
        self.assertEqual(code, 1)
        self.assertIn("MINAS_SUB_API_KEY_FILE", out)
        self.assertIn("MINAS_MAIN_API_KEY_FILE", out)

    def test_a_secret_pasted_into_the_variable_is_never_echoed(self):
        # REGRESSION: describe() printed the variable's value verbatim whenever
        # it named no file.  The classic misconfiguration is pasting the SECRET
        # into {PREFIX}_API_SECRET_FILE instead of a path — and then that value
        # IS the credential, published to stdout, the CI log and the journal.
        env = write_credentials(self.tmp.name, "BITGET_SUB", self.SECRET)
        env.update(write_credentials(self.tmp.name, "BITGET_MAIN", self.SECRET))
        env["BITGET_MAIN_API_SECRET_FILE"] = self.SECRET  # pasted, not a path
        code, out = self._run(env)
        self.assertEqual(code, 1)
        self.assertNotIn(self.SECRET, out)
        self.assertIn("BITGET_MAIN_API_SECRET_FILE", out)
        self.assertIn("NOT SHOWN", out)

    def test_the_orchestrators_refusal_message_does_not_echo_it_either(self):
        env = write_credentials(self.tmp.name, "BITGET_SUB", self.SECRET)
        env.update(write_credentials(self.tmp.name, "BITGET_MAIN", self.SECRET))
        env["BITGET_MAIN_API_KEY_FILE"] = self.SECRET
        env["MEME_BUY_ARMED"] = "1"
        cfg = config.load_meme_buy_config(
            raw_config(armed=True, dry_run=False, kill_switch_file="/tmp/stop",
                       db_path=os.path.join(self.tmp.name, "j.sqlite3"))
        )
        clock = FakeClock()
        result = MemeBuyOrchestrator(
            cfg, deps=Deps(bsc_buy=FakeSwap("tx_hash", live_allowed=True),
                           screen_token=FakeGate(), balance_probe=FakeProbe()),
            env=env, clock=clock.time, sleep=clock.sleep,
        ).run_once("bsc", BSC_TOKEN, 25.0)
        self.assertFalse(result.ok)
        self.assertNotIn(self.SECRET, result.reason)
        self.assertIn("BITGET_MAIN_API_KEY_FILE", result.reason)


if __name__ == "__main__":
    unittest.main()
