"""Tests for the token safety gate. ZERO network, ZERO credentials.

Every numbered rule is exercised in both directions, plus the failure modes
that would silently turn the gate into a rubber stamp: a missing GoPlus field,
a provider that raises, HTTP 429, the RugCheck score direction, and the
fraction-vs-percent tax units.
"""

import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from safety import dexscreener, goplus, honeypot_is, rugcheck, solana_mint  # noqa: E402
from safety.errors import (DexScreenerError, GoPlusError, HoneypotIsError,  # noqa: E402
                           MissingFieldError, RugCheckError, SolanaRPCError)
from safety.gate import (MUTABLE_FLAGS, Providers, Thresholds,  # noqa: E402
                         load_allowlist, screen_token)

BSC_TOKEN = "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82"   # CAKE, EIP-55 checksummed
SOL_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # USDC mint
ALLOWLIST = {"bsc": [BSC_TOKEN], "solana": [SOL_MINT]}
NOW = time.time()


class FakeResponse:
    """Mirrors tests/test_autobuy.py's FakeResponse."""

    def __init__(self, payload, status_code=200, headers=None, text=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is _BAD_JSON:
            raise ValueError("not json")
        return self._payload


_BAD_JSON = object()


# ---------- canned provider payloads (the "clean token" baseline) ----------

def goplus_bsc_ok(**overrides):
    data = {
        "is_open_source": "1",
        "is_honeypot": "0",
        "cannot_sell_all": "0",
        "cannot_buy": "0",
        "buy_tax": "0.02",      # FRACTION -> 2%
        "sell_tax": "0.03",     # FRACTION -> 3%
        "lp_holders": [
            {"address": "0x000000000000000000000000000000000000dead",
             "tag": "Burn Address", "is_locked": 1, "percent": "0.97"},
            {"address": "0xaaaa000000000000000000000000000000000001",
             "tag": "", "is_locked": 0, "percent": "0.03"},
        ],
        "holders": [
            {"address": "0xaaaa000000000000000000000000000000000002",
             "tag": "", "is_locked": 0, "percent": "0.05"},
            {"address": "0xaaaa000000000000000000000000000000000003",
             "tag": "", "is_locked": 0, "percent": "0.04"},
            {"address": "0xcccc000000000000000000000000000000000009",
             "tag": "PancakeSwap V2 pair", "is_locked": 0, "percent": "0.70"},
        ],
    }
    data.update({flag: "0" for flag in MUTABLE_FLAGS})
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not _ABSENT}


def goplus_sol_ok(**overrides):
    data = {
        "holders": [
            {"account": "Hold1111111111111111111111111111111111111111",
             "tag": "", "is_locked": 0, "percent": "0.06"},
            {"account": "Hold2222222222222222222222222222222222222222",
             "tag": "", "is_locked": 0, "percent": "0.04"},
        ],
        "lp_holders": [
            {"account": "Pool1111111111111111111111111111111111111111",
             "tag": "raydium pool", "is_locked": 1, "percent": "0.80"},
        ],
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not _ABSENT}


_ABSENT = object()  # marker: "GoPlus omitted this field entirely"


def honeypot_ok(**overrides):
    kwargs = dict(simulation_success=True, is_honeypot=False, risk_level=1,
                  risk="low", buy_tax_pct=2.0, sell_tax_pct=3.0, raw={})
    kwargs.update(overrides)
    return honeypot_is.HoneypotReport(**kwargs)


def rugcheck_ok(**overrides):
    kwargs = dict(
        score_normalised=5.0, risks=[], danger_risks=[], lp_locked_pct=100.0,
        lockers=[rugcheck.Locker(owner="Lock111111111111", unlock_date=int(NOW + 120 * 86400),
                                 usdc_locked=90_000.0, kind="raydium")],
        raw={},
    )
    kwargs.update(overrides)
    return rugcheck.RugCheckReport(**kwargs)


def pair_ok(**overrides):
    kwargs = dict(chain_id="bsc", pair_address="0xpair", dex_id="pancakeswap",
                  liquidity_usd=200_000.0, pair_created_at_ms=int((NOW - 48 * 3600) * 1000),
                  volume_h24_usd=400_000.0, txns_m5_buys=12, txns_m5_sells=5,
                  price_usd=1.0, raw={})
    kwargs.update(overrides)
    return dexscreener.PairSnapshot(**kwargs)


def mint_ok(**overrides):
    kwargs = dict(mint=SOL_MINT, program_id=solana_mint.TOKEN_PROGRAM,
                  mint_authority=None, freeze_authority=None, decimals=6,
                  is_initialized=True, extensions={}, raw={})
    kwargs.update(overrides)
    return solana_mint.MintReport(**kwargs)


def make_providers(goplus_evm=None, goplus_sol=None, honeypot=None,
                   rug=None, dex=None, mint=None):
    return Providers(
        goplus_evm=goplus_evm or (lambda a: goplus_bsc_ok()),
        goplus_solana=goplus_sol or (lambda a: goplus_sol_ok()),
        honeypot_is=honeypot or (lambda a: honeypot_ok()),
        rugcheck=rug or (lambda a: rugcheck_ok()),
        dexscreener=dex or (lambda a, c: pair_ok()),
        solana_mint=mint or (lambda a: mint_ok()),
    )


class GateTestCase(unittest.TestCase):
    """Shared helpers: run the gate with injected providers, assert on checks."""

    def screen_bsc(self, price_impact_bps=100, thresholds=None, address=BSC_TOKEN, **kw):
        return screen_token("bsc", address, price_impact_bps=price_impact_bps,
                            thresholds=thresholds, providers=make_providers(**kw),
                            allowlist=ALLOWLIST)

    def screen_sol(self, price_impact_bps=100, thresholds=None, address=SOL_MINT, **kw):
        return screen_token("solana", address, price_impact_bps=price_impact_bps,
                            thresholds=thresholds, providers=make_providers(**kw),
                            allowlist=ALLOWLIST)

    def assertPassed(self, verdict):
        self.assertTrue(verdict.passed, f"expected pass, got reasons: {verdict.reasons}")

    def assertRejected(self, verdict, check_name, detail_fragment=None):
        self.assertFalse(verdict.passed, "expected REJECT, gate passed the token")
        failed = [c for c in verdict.checks if not c.passed]
        names = [c.name for c in failed]
        self.assertTrue(any(check_name in n for n in names),
                        f"{check_name} not among failed checks {names}")
        if detail_fragment:
            hit = [c.detail for c in failed if check_name in c.name]
            self.assertTrue(any(detail_fragment in d for d in hit),
                            f"{detail_fragment!r} not in {hit}")


# ---------- baseline ----------

class TestCleanToken(GateTestCase):
    def test_clean_bsc_token_passes(self):
        self.assertPassed(self.screen_bsc())

    def test_clean_solana_token_passes(self):
        self.assertPassed(self.screen_sol(dex=lambda a, c: pair_ok(chain_id="solana")))

    def test_verdict_is_structured_not_a_bool(self):
        verdict = self.screen_bsc()
        self.assertIsInstance(verdict.checks, list)
        self.assertTrue(all({"name", "passed", "detail"} <= set(c.keys())
                            for c in verdict.to_dict()["checks"]))
        self.assertIn("01_allowlist", [c.name for c in verdict.checks])


# ---------- rules 1 and 2 ----------

class TestAllowlistAndFormat(GateTestCase):
    def test_rule1_address_in_allowlist_passes(self):
        self.assertTrue(any(c.name == "01_allowlist" and c.passed
                            for c in self.screen_bsc().checks))

    def test_rule1_address_not_in_allowlist_rejects(self):
        other = "0x1F9840a85d5aF5bf1D1762F925BDADdC4201F984"
        verdict = screen_token("bsc", other, price_impact_bps=10,
                               providers=make_providers(), allowlist=ALLOWLIST)
        self.assertRejected(verdict, "01_allowlist", "NOT in")

    def test_rule1_no_allowlist_configured_rejects(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            verdict = screen_token("bsc", BSC_TOKEN, price_impact_bps=10,
                                   providers=make_providers())
        self.assertRejected(verdict, "01_allowlist")

    def test_rule1_rejects_before_touching_the_network(self):
        def explode(*a, **kw):
            raise AssertionError("provider must not be called after an allowlist reject")
        verdict = screen_token("bsc", "0x1F9840a85d5aF5bf1D1762F925BDADdC4201F984",
                               price_impact_bps=10, allowlist=ALLOWLIST,
                               providers=make_providers(goplus_evm=explode, dex=explode))
        self.assertRejected(verdict, "01_allowlist")

    def test_rule1_allowlist_file_is_read_from_a_path_env_var(self):
        path = os.path.join(os.path.dirname(__file__), "_tmp_allowlist.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(ALLOWLIST, fh)
        try:
            with mock.patch.dict(os.environ, {"SAFETY_ALLOWLIST_FILE": path}):
                self.assertIn(BSC_TOKEN, load_allowlist()["bsc"])
        finally:
            os.remove(path)

    def test_rule1_comment_keys_are_ignored(self):
        self.assertEqual(load_allowlist({"_comment": "notes", "bsc": [BSC_TOKEN]}),
                         {"bsc": {BSC_TOKEN}})

    def test_rule1_shipped_example_allowlist_rejects_everything(self):
        example = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "safety", "allowlist.example.json")
        self.assertEqual(load_allowlist(example), {"bsc": set(), "solana": set()})

    def test_rule2_bad_evm_checksum_rejects(self):
        bad = BSC_TOKEN.lower()  # valid hex, wrong EIP-55 casing
        verdict = screen_token("bsc", bad, price_impact_bps=10,
                               providers=make_providers(),
                               allowlist={"bsc": [bad], "solana": []})
        self.assertRejected(verdict, "02_address_format")

    def test_rule2_bad_solana_mint_rejects(self):
        bad = "not-base58-0OIl"
        verdict = screen_token("solana", bad, price_impact_bps=10,
                               providers=make_providers(), allowlist={"solana": [bad]})
        self.assertRejected(verdict, "02_address_format")

    def test_unsupported_chain_rejects(self):
        self.assertRejected(screen_token("ethereum", BSC_TOKEN, 10,
                                         providers=make_providers(), allowlist=ALLOWLIST),
                            "00_chain_supported")


# ---------- rules 3-7 (GoPlus BSC) ----------

class TestGoPlusBsc(GateTestCase):
    def test_rule3_closed_source_rejects(self):
        self.assertRejected(
            self.screen_bsc(goplus_evm=lambda a: goplus_bsc_ok(is_open_source="0")),
            "03_goplus_open_source")

    def test_rule4_honeypot_flag_rejects(self):
        self.assertRejected(
            self.screen_bsc(goplus_evm=lambda a: goplus_bsc_ok(is_honeypot="1")),
            "04_goplus_honeypot_flags", "is_honeypot=1")

    def test_rule4_cannot_sell_all_rejects(self):
        self.assertRejected(
            self.screen_bsc(goplus_evm=lambda a: goplus_bsc_ok(cannot_sell_all="1")),
            "04_goplus_honeypot_flags")

    def test_rule4_cannot_buy_rejects(self):
        self.assertRejected(
            self.screen_bsc(goplus_evm=lambda a: goplus_bsc_ok(cannot_buy="1")),
            "04_goplus_honeypot_flags")

    def test_rule4_missing_cannot_sell_all_rejects_not_passes(self):
        # The CAKE case: GoPlus simply omits the key. This MUST NOT read as safe.
        verdict = self.screen_bsc(
            goplus_evm=lambda a: goplus_bsc_ok(cannot_sell_all=_ABSENT))
        self.assertRejected(verdict, "04_goplus_honeypot_flags",
                            "field cannot_sell_all missing")

    def test_rule5_high_sell_tax_rejects(self):
        self.assertRejected(
            self.screen_bsc(goplus_evm=lambda a: goplus_bsc_ok(sell_tax="0.15")),
            "05_goplus_taxes")

    def test_rule5_tax_at_the_limit_passes(self):
        self.assertPassed(
            self.screen_bsc(goplus_evm=lambda a: goplus_bsc_ok(buy_tax="0.10",
                                                               sell_tax="0.10")))

    def test_rule5_fractions_are_not_confused_with_percents(self):
        # "0.1" is 10% and passes; "10" would be 1000% and must NOT be read as 10%.
        self.assertPassed(self.screen_bsc(
            goplus_evm=lambda a: goplus_bsc_ok(buy_tax="0.1", sell_tax="0.1")))
        self.assertRejected(self.screen_bsc(
            goplus_evm=lambda a: goplus_bsc_ok(buy_tax="10", sell_tax="10")),
            "05_goplus_taxes")

    def test_rule5_missing_buy_tax_rejects(self):
        self.assertRejected(
            self.screen_bsc(goplus_evm=lambda a: goplus_bsc_ok(buy_tax=_ABSENT)),
            "05_goplus_taxes", "field buy_tax missing")

    def test_rule5_empty_string_tax_rejects(self):
        self.assertRejected(
            self.screen_bsc(goplus_evm=lambda a: goplus_bsc_ok(sell_tax="")),
            "05_goplus_taxes", "field sell_tax missing")

    def test_rule6_every_mutable_flag_rejects_when_set(self):
        for flag in MUTABLE_FLAGS:
            with self.subTest(flag=flag):
                self.assertRejected(
                    self.screen_bsc(goplus_evm=lambda a, f=flag: goplus_bsc_ok(**{f: "1"})),
                    "06_goplus_mutable_flags", flag)

    def test_rule6_missing_mutable_flag_rejects(self):
        self.assertRejected(
            self.screen_bsc(goplus_evm=lambda a: goplus_bsc_ok(hidden_owner=_ABSENT)),
            "06_goplus_mutable_flags", "field hidden_owner missing")

    def test_rule7_unlocked_lp_rejects(self):
        unlocked = [{"address": "0xaaaa000000000000000000000000000000000001",
                     "tag": "", "is_locked": 0, "percent": "0.94"},
                    {"address": "0x000000000000000000000000000000000000dead",
                     "tag": "Burn", "is_locked": 1, "percent": "0.06"}]
        self.assertRejected(
            self.screen_bsc(goplus_evm=lambda a: goplus_bsc_ok(lp_holders=unlocked)),
            "07_lp_locked_or_burned")

    def test_rule7_burned_lp_counts_as_locked(self):
        burned = [{"address": "0x000000000000000000000000000000000000dead",
                   "tag": "", "is_locked": 0, "percent": "0.99"}]
        self.assertPassed(
            self.screen_bsc(goplus_evm=lambda a: goplus_bsc_ok(lp_holders=burned)))

    def test_rule7_missing_lp_holders_rejects(self):
        # Verified live: even CAKE came back with no lp_holders at all.
        self.assertRejected(
            self.screen_bsc(goplus_evm=lambda a: goplus_bsc_ok(lp_holders=_ABSENT)),
            "07_lp_locked_or_burned", "field lp_holders missing")


# ---------- rule 8 (honeypot.is) ----------

class TestHoneypotIsRule(GateTestCase):
    def test_rule8_simulation_failure_rejects(self):
        self.assertRejected(
            self.screen_bsc(honeypot=lambda a: honeypot_ok(simulation_success=False)),
            "08_honeypot_is_simulation", "simulation failed")

    def test_rule8_honeypot_rejects(self):
        self.assertRejected(
            self.screen_bsc(honeypot=lambda a: honeypot_ok(is_honeypot=True)),
            "08_honeypot_is_simulation", "flagged honeypot")

    def test_rule8_risk_level_boundary(self):
        self.assertPassed(self.screen_bsc(honeypot=lambda a: honeypot_ok(risk_level=11)))
        self.assertRejected(self.screen_bsc(honeypot=lambda a: honeypot_ok(risk_level=12)),
                            "08_honeypot_is_simulation")


# ---------- rules 9-12 (Solana) ----------

class TestSolanaRules(GateTestCase):
    def sol(self, **kw):
        kw.setdefault("dex", lambda a, c: pair_ok(chain_id="solana"))
        return self.screen_sol(**kw)

    def test_rule9_live_mint_authority_rejects(self):
        self.assertRejected(self.sol(mint=lambda a: mint_ok(mint_authority="Auth111")),
                            "09_solana_authorities", "mintAuthority")

    def test_rule9_live_freeze_authority_rejects(self):
        self.assertRejected(self.sol(mint=lambda a: mint_ok(freeze_authority="Auth111")),
                            "09_solana_authorities", "freezeAuthority")

    def test_rule9_both_null_passes(self):
        self.assertPassed(self.sol())

    def test_rule10_dangerous_extensions_reject(self):
        cases = {
            "permanentDelegate": {"delegate": "Del111"},
            "transferHook": {"programId": "Hook111", "authority": None},
            "transferFeeConfig": {"transferFeeConfigAuthority": "Fee111"},
            "defaultAccountState": {"accountState": "frozen"},
            "pausable": {"paused": False},
            "nonTransferable": {},
            "someBrandNewExtension": {},
        }
        for name, state in cases.items():
            with self.subTest(extension=name):
                self.assertRejected(
                    self.sol(mint=lambda a, n=name, s=state: mint_ok(extensions={n: s})),
                    "10_token2022_extensions")

    def test_rule10_benign_extensions_pass(self):
        self.assertPassed(self.sol(mint=lambda a: mint_ok(
            program_id=solana_mint.TOKEN_2022_PROGRAM,
            extensions={"metadataPointer": {"authority": None}, "tokenMetadata": {}})))

    def test_rule10_unknown_token_program_rejects(self):
        self.assertRejected(self.sol(mint=lambda a: mint_ok(program_id="EvilProgram1111")),
                            "10_token2022_extensions", "unknown token program")

    def test_rule11_danger_risk_rejects(self):
        self.assertRejected(
            self.sol(rug=lambda a: rugcheck_ok(danger_risks=["Freeze Authority still enabled"])),
            "11_rugcheck_risk", "danger risks")

    def test_rule11_score_direction_higher_is_worse(self):
        # Pinned on purpose: USDC scores 1, BONK 7, a live-authority token 76.
        self.assertPassed(self.sol(rug=lambda a: rugcheck_ok(score_normalised=1.0)))
        self.assertPassed(self.sol(rug=lambda a: rugcheck_ok(score_normalised=20.0)))
        self.assertRejected(self.sol(rug=lambda a: rugcheck_ok(score_normalised=76.0)),
                            "11_rugcheck_risk", "higher is worse")
        # If the comparison were inverted, the clean token would be the reject.
        clean = self.sol(rug=lambda a: rugcheck_ok(score_normalised=1.0))
        self.assertTrue(any(c.name == "11_rugcheck_risk" and c.passed for c in clean.checks))

    def test_rule12_low_lp_locked_pct_rejects(self):
        self.assertRejected(self.sol(rug=lambda a: rugcheck_ok(lp_locked_pct=80.0)),
                            "12_rugcheck_lp_lock", "lpLockedPct")

    def test_rule12_missing_lp_locked_pct_rejects(self):
        self.assertRejected(self.sol(rug=lambda a: rugcheck_ok(lp_locked_pct=None)),
                            "12_rugcheck_lp_lock", "field lpLockedPct missing")

    def test_rule12_imminent_unlock_rejects_even_at_100_pct(self):
        soon = rugcheck.Locker(owner="Lock111111111111", unlock_date=int(NOW + 3600),
                               usdc_locked=1.0, kind="raydium")
        self.assertRejected(
            self.sol(rug=lambda a: rugcheck_ok(lp_locked_pct=100.0, lockers=[soon])),
            "12_rugcheck_lp_lock", "unlocks in")

    def test_rule12_locker_without_unlock_date_rejects(self):
        blank = rugcheck.Locker(owner="Lock111111111111", unlock_date=None,
                                usdc_locked=1.0, kind="")
        self.assertRejected(
            self.sol(rug=lambda a: rugcheck_ok(lockers=[blank])),
            "12_rugcheck_lp_lock", "no unlock date")


# ---------- rule 13 (holders, both chains) ----------

class TestHolderConcentration(GateTestCase):
    def test_rule13_whale_rejects_on_bsc(self):
        holders = [{"address": "0xaaaa000000000000000000000000000000000002",
                    "tag": "", "is_locked": 0, "percent": "0.31"}]
        self.assertRejected(
            self.screen_bsc(goplus_evm=lambda a: goplus_bsc_ok(holders=holders)),
            "13_holder_concentration")

    def test_rule13_lp_vault_is_excluded_from_concentration(self):
        holders = [{"address": "0x000000000000000000000000000000000000dead",
                    "tag": "Burn", "is_locked": 1, "percent": "0.90"},
                   {"address": "0xaaaa000000000000000000000000000000000002",
                    "tag": "", "is_locked": 0, "percent": "0.05"}]
        self.assertPassed(
            self.screen_bsc(goplus_evm=lambda a: goplus_bsc_ok(holders=holders)))

    def test_rule13_missing_holders_rejects(self):
        self.assertRejected(
            self.screen_bsc(goplus_evm=lambda a: goplus_bsc_ok(holders=_ABSENT)),
            "13_holder_concentration", "field holders missing")

    def test_rule13_missing_percent_rejects(self):
        holders = [{"address": "0xaaaa000000000000000000000000000000000002", "tag": ""}]
        self.assertRejected(
            self.screen_bsc(goplus_evm=lambda a: goplus_bsc_ok(holders=holders)),
            "13_holder_concentration", "field holders[].percent missing")

    def test_rule13_whale_rejects_on_solana(self):
        holders = [{"account": "Hold1111111111111111111111111111111111111111",
                    "tag": "", "is_locked": 0, "percent": "0.44"}]
        self.assertRejected(
            self.screen_sol(goplus_sol=lambda a: goplus_sol_ok(holders=holders),
                            dex=lambda a, c: pair_ok(chain_id="solana")),
            "13_holder_concentration")


# ---------- rules 14-16 (market) ----------

class TestMarketRules(GateTestCase):
    def test_rule14_thin_liquidity_rejects(self):
        self.assertRejected(self.screen_bsc(dex=lambda a, c: pair_ok(
            liquidity_usd=49_000.0, volume_h24_usd=60_000.0)), "14_liquidity")

    def test_rule14_liquidity_at_the_floor_passes(self):
        self.assertPassed(self.screen_bsc(dex=lambda a, c: pair_ok(
            liquidity_usd=50_000.0, volume_h24_usd=100_000.0)))

    def test_rule14_missing_liquidity_rejects(self):
        self.assertRejected(self.screen_bsc(dex=lambda a, c: pair_ok(liquidity_usd=None)),
                            "14_liquidity", "field liquidity.usd missing")

    def test_rule15_new_pair_rejects(self):
        self.assertRejected(self.screen_bsc(dex=lambda a, c: pair_ok(
            pair_created_at_ms=int((NOW - 3600) * 1000))), "15_pair_age")

    def test_rule15_missing_pair_created_at_rejects(self):
        self.assertRejected(self.screen_bsc(dex=lambda a, c: pair_ok(pair_created_at_ms=None)),
                            "15_pair_age", "field pairCreatedAt missing")

    def test_rule16_low_volume_rejects(self):
        self.assertRejected(self.screen_bsc(dex=lambda a, c: pair_ok(
            liquidity_usd=200_000.0, volume_h24_usd=20_000.0)), "16_volume_and_flow")

    def test_rule16_volume_liquidity_ratio_bounds(self):
        self.assertRejected(self.screen_bsc(dex=lambda a, c: pair_ok(
            liquidity_usd=200_000.0, volume_h24_usd=80_000.0)),   # ratio 0.4
            "16_volume_and_flow", "volume/liquidity")
        self.assertRejected(self.screen_bsc(dex=lambda a, c: pair_ok(
            liquidity_usd=200_000.0, volume_h24_usd=2_500_000.0)),  # ratio 12.5
            "16_volume_and_flow", "volume/liquidity")

    def test_rule16_zero_sells_rejects(self):
        # Many buys, zero sells: a honeypot tell independent of any security API.
        self.assertRejected(self.screen_bsc(dex=lambda a, c: pair_ok(
            txns_m5_buys=250, txns_m5_sells=0)), "16_volume_and_flow", "m5 sells 0")

    def test_rule16_missing_txns_rejects(self):
        self.assertRejected(self.screen_bsc(dex=lambda a, c: pair_ok(txns_m5_sells=None)),
                            "16_volume_and_flow", "field txns.m5.buys/sells missing")


# ---------- rule 17 (price impact) ----------

class TestPriceImpact(GateTestCase):
    def test_rule17_missing_price_impact_rejects(self):
        self.assertRejected(self.screen_bsc(price_impact_bps=None), "17_price_impact",
                            "not supplied")

    def test_rule17_high_impact_rejects(self):
        self.assertRejected(self.screen_bsc(price_impact_bps=501), "17_price_impact")

    def test_rule17_impact_at_the_limit_passes(self):
        self.assertPassed(self.screen_bsc(price_impact_bps=500))

    def test_rule17_negative_impact_rejects(self):
        self.assertRejected(self.screen_bsc(price_impact_bps=-1), "17_price_impact")


# ---------- fail-closed behaviour ----------

class TestFailsClosed(GateTestCase):
    def _boom(self, *a, **kw):
        raise RuntimeError("provider exploded")

    def test_provider_exception_rejects(self):
        for kw, expected in (({"goplus_evm": self._boom}, "goplus_fetch"),
                             ({"honeypot": self._boom}, "08_honeypot_is_simulation"),
                             ({"dex": self._boom}, "dexscreener_fetch")):
            with self.subTest(provider=list(kw)[0]):
                self.assertRejected(self.screen_bsc(**kw), expected)

    def test_solana_provider_exception_rejects(self):
        for kw, expected in (({"mint": self._boom}, "solana_mint_fetch"),
                             ({"rug": self._boom}, "rugcheck_fetch"),
                             ({"goplus_sol": self._boom}, "goplus_fetch")):
            with self.subTest(provider=list(kw)[0]):
                self.assertRejected(
                    self.screen_sol(dex=lambda a, c: pair_ok(chain_id="solana"), **kw),
                    expected)

    def test_dependent_checks_are_recorded_as_failed_not_skipped(self):
        verdict = self.screen_bsc(goplus_evm=self._boom)
        names = {c.name for c in verdict.checks if not c.passed}
        for rule in ("03_goplus_open_source", "04_goplus_honeypot_flags",
                     "05_goplus_taxes", "06_goplus_mutable_flags",
                     "07_lp_locked_or_burned", "13_holder_concentration"):
            self.assertIn(rule, names)

    def test_rate_limit_429_rejects(self):
        def limited(*a, **kw):
            raise GoPlusError("rate_limited", "HTTP 429 from GoPlus", 429)
        self.assertRejected(self.screen_bsc(goplus_evm=limited), "goplus_fetch", "429")

    def test_dexscreener_429_rejects(self):
        def limited(*a, **kw):
            raise DexScreenerError("rate_limited", "HTTP 429", 429)
        self.assertRejected(self.screen_bsc(dex=limited), "dexscreener_fetch", "429")

    def test_timeout_rejects(self):
        import requests

        def timing_out(*a, **kw):
            raise requests.Timeout("read timed out")
        self.assertRejected(self.screen_bsc(dex=timing_out), "dexscreener_fetch")

    def test_gate_never_raises_on_garbage_providers(self):
        garbage = Providers(
            goplus_evm=lambda a: "not a dict",
            goplus_solana=lambda a: None,
            honeypot_is=lambda a: object(),
            rugcheck=lambda a: 42,
            dexscreener=lambda a, c: None,
            solana_mint=lambda a: "nope",
        )
        for chain, address in (("bsc", BSC_TOKEN), ("solana", SOL_MINT)):
            verdict = screen_token(chain, address, price_impact_bps="oops",
                                   providers=garbage, allowlist=ALLOWLIST)
            self.assertFalse(verdict.passed)
            self.assertTrue(verdict.reasons)

    def test_gate_never_raises_on_bad_arguments(self):
        for args in ((None, None), ("bsc", None), ("", ""), (123, 456)):
            verdict = screen_token(*args, providers=make_providers(), allowlist=ALLOWLIST)
            self.assertFalse(verdict.passed)

    def test_gate_never_raises_when_allowlist_is_garbage(self):
        verdict = screen_token("bsc", BSC_TOKEN, 10, providers=make_providers(),
                               allowlist={"bsc": "0xdeadbeef"})
        self.assertRejected(verdict, "01_allowlist")

    def test_a_check_that_never_ran_cannot_pass(self):
        from safety.gate import _finalise, _Ledger
        ledger = _Ledger()
        ledger.add("01_allowlist", True, "ok")
        verdict = _finalise(ledger, ("01_allowlist", "17_price_impact"))
        self.assertFalse(verdict.passed)
        self.assertIn("gate_incomplete", [c.name for c in verdict.checks])

    def test_every_numbered_rule_appears_in_a_passing_verdict(self):
        bsc = {c.name.split("_")[0] for c in self.screen_bsc().checks}
        self.assertTrue({f"{n:02d}" for n in list(range(1, 9)) + [13, 14, 15, 16, 17]} <= bsc)
        sol = {c.name.split("_")[0] for c in
               self.screen_sol(dex=lambda a, c: pair_ok(chain_id="solana")).checks}
        self.assertTrue({f"{n:02d}" for n in [1, 2, 9, 10, 11, 12, 13, 14, 15, 16, 17]} <= sol)

    def test_thresholds_are_configurable(self):
        strict = Thresholds(min_liquidity_usd=1_000_000.0)
        self.assertRejected(self.screen_bsc(thresholds=strict), "14_liquidity")
        loose = Thresholds(min_liquidity_usd=1.0, min_volume_h24_usd=1.0,
                           min_pair_age_seconds=0, max_volume_liquidity_ratio=1e9)
        self.assertPassed(self.screen_bsc(thresholds=loose, dex=lambda a, c: pair_ok(
            liquidity_usd=2_000.0, volume_h24_usd=9_000.0,
            pair_created_at_ms=int((NOW - 60) * 1000))))


# ---------- provider modules (mocked at requests.Session.request) ----------

class TestGoPlusProvider(unittest.TestCase):
    def test_evm_result_is_keyed_by_lowercased_address(self):
        payload = {"code": 1, "message": "OK",
                   "result": {BSC_TOKEN.lower(): {"is_open_source": "1"}}}
        with mock.patch("requests.Session.request",
                        return_value=FakeResponse(payload)) as req:
            data = goplus.fetch_evm(BSC_TOKEN)
        self.assertEqual(data["is_open_source"], "1")
        self.assertIn("/token_security/56", req.call_args[0][1])
        self.assertEqual(req.call_args[1]["params"]["contract_addresses"], BSC_TOKEN.lower())

    def test_solana_uses_its_own_endpoint(self):
        payload = {"code": 1, "result": {SOL_MINT: {"freezable": {"status": "0"}}}}
        with mock.patch("requests.Session.request",
                        return_value=FakeResponse(payload)) as req:
            data = goplus.fetch_solana(SOL_MINT)
        self.assertIn("/solana/token_security", req.call_args[0][1])
        self.assertEqual(goplus.solana_authority_status(data, "freezable"), "0")

    def test_missing_solana_field_raises(self):
        with self.assertRaises(GoPlusError):
            goplus.solana_authority_status({}, "freezable")

    def test_http_429_raises(self):
        with mock.patch("requests.Session.request", return_value=FakeResponse({}, 429)):
            with self.assertRaises(GoPlusError) as ctx:
                goplus.fetch_evm(BSC_TOKEN)
        self.assertEqual(ctx.exception.http_status, 429)

    def test_business_code_2_partial_data_raises(self):
        payload = {"code": 2, "message": "data not ready", "result": {}}
        with mock.patch("requests.Session.request", return_value=FakeResponse(payload)):
            with self.assertRaises(GoPlusError):
                goplus.fetch_evm(BSC_TOKEN)

    def test_unknown_address_in_result_raises(self):
        payload = {"code": 1, "result": {"0xsomethingelse": {}}}
        with mock.patch("requests.Session.request", return_value=FakeResponse(payload)):
            with self.assertRaises(GoPlusError):
                goplus.fetch_evm(BSC_TOKEN)

    def test_no_retry_on_failure(self):
        with mock.patch("requests.Session.request",
                        return_value=FakeResponse({}, 500)) as req:
            with self.assertRaises(GoPlusError):
                goplus.fetch_evm(BSC_TOKEN)
        self.assertEqual(req.call_count, 1)


class TestHoneypotIsProvider(unittest.TestCase):
    def test_parses_simulation(self):
        payload = {"simulationSuccess": True, "honeypotResult": {"isHoneypot": False},
                   "summary": {"risk": "low", "riskLevel": 3},
                   "simulationResult": {"buyTax": 1.0, "sellTax": 2.0}}
        with mock.patch("requests.Session.request", return_value=FakeResponse(payload)):
            report = honeypot_is.fetch(BSC_TOKEN)
        self.assertTrue(report.simulation_success)
        self.assertEqual(report.risk_level, 3)

    def test_missing_risk_level_raises_missing_field(self):
        payload = {"simulationSuccess": True, "honeypotResult": {"isHoneypot": False},
                   "summary": {}}
        with mock.patch("requests.Session.request", return_value=FakeResponse(payload)):
            with self.assertRaises(MissingFieldError) as ctx:
                honeypot_is.fetch(BSC_TOKEN)
        self.assertIn("field summary.riskLevel missing", str(ctx.exception))

    def test_429_raises(self):
        with mock.patch("requests.Session.request", return_value=FakeResponse({}, 429)):
            with self.assertRaises(HoneypotIsError):
                honeypot_is.fetch(BSC_TOKEN)


class TestRugCheckProvider(unittest.TestCase):
    def test_parses_score_and_lockers(self):
        summary = {"score": 1200, "score_normalised": 7,
                   "risks": [{"name": "Low amount of LP Providers", "level": "warn"},
                             {"name": "Freeze Authority", "level": "danger"}]}
        report = {"markets": [{"lp": {"lpLockedPct": 99.5, "lpLockedUSD": 120000}}],
                  "lockers": {"acct1": {"owner": "Own111", "unlockDate": 1893456000,
                                        "usdcLocked": 120000, "type": "raydium"}}}
        responses = [FakeResponse(summary), FakeResponse(report)]
        with mock.patch("requests.Session.request", side_effect=responses):
            parsed = rugcheck.fetch(SOL_MINT)
        self.assertEqual(parsed.score_normalised, 7)
        self.assertEqual(parsed.danger_risks, ["Freeze Authority"])
        self.assertEqual(parsed.lp_locked_pct, 99.5)
        self.assertEqual(parsed.lockers[0].unlock_date, 1893456000)

    def test_score_direction_constant_is_documented(self):
        self.assertTrue(rugcheck.SCORE_HIGHER_IS_WORSE)

    def test_missing_score_raises(self):
        with mock.patch("requests.Session.request",
                        return_value=FakeResponse({"risks": []})):
            with self.assertRaises(MissingFieldError):
                rugcheck.fetch(SOL_MINT)

    def test_429_raises(self):
        with mock.patch("requests.Session.request", return_value=FakeResponse({}, 429)):
            with self.assertRaises(RugCheckError):
                rugcheck.fetch(SOL_MINT)


class TestDexScreenerProvider(unittest.TestCase):
    def _payload(self):
        return {"pairs": [
            {"chainId": "bsc", "pairAddress": "0xshallow", "dexId": "pancakeswap",
             "liquidity": {"usd": 10_000}, "pairCreatedAt": 1700000000000,
             "volume": {"h24": 1000}, "txns": {"m5": {"buys": 1, "sells": 1}}},
            {"chainId": "bsc", "pairAddress": "0xdeep", "dexId": "pancakeswap",
             "liquidity": {"usd": 900_000}, "pairCreatedAt": 1700000000000,
             "volume": {"h24": 500_000}, "txns": {"m5": {"buys": 9, "sells": 4}}},
            {"chainId": "solana", "pairAddress": "SolPair", "dexId": "raydium",
             "liquidity": {"usd": 5_000_000}, "pairCreatedAt": 1700000000000,
             "volume": {"h24": 1}, "txns": {"m5": {"buys": 0, "sells": 0}}},
        ]}

    def test_picks_deepest_pair_on_the_requested_chain(self):
        with mock.patch("requests.Session.request",
                        return_value=FakeResponse(self._payload())):
            snapshot = dexscreener.fetch(BSC_TOKEN, "bsc")
        self.assertEqual(snapshot.pair_address, "0xdeep")
        self.assertEqual(snapshot.txns_m5_sells, 4)

    def test_no_pairs_raises(self):
        with mock.patch("requests.Session.request",
                        return_value=FakeResponse({"pairs": None})):
            with self.assertRaises(DexScreenerError):
                dexscreener.fetch(BSC_TOKEN, "bsc")

    def test_429_raises_and_does_not_retry(self):
        with mock.patch("requests.Session.request",
                        return_value=FakeResponse({}, 429, {"Retry-After": "30"})) as req:
            with self.assertRaises(DexScreenerError) as ctx:
                dexscreener.fetch(BSC_TOKEN, "bsc")
        self.assertEqual(ctx.exception.http_status, 429)
        self.assertEqual(req.call_count, 1)


class TestSolanaMintProvider(unittest.TestCase):
    def _payload(self, info=None, owner=solana_mint.TOKEN_PROGRAM):
        info = info if info is not None else {
            "mintAuthority": None, "freezeAuthority": None,
            "decimals": 6, "isInitialized": True, "supply": "1"}
        return {"jsonrpc": "2.0", "id": 1, "result": {
            "value": {"owner": owner, "lamports": 1,
                      "data": {"parsed": {"type": "mint", "info": info},
                               "program": "spl-token"}}}}

    def test_parses_null_authorities(self):
        with mock.patch("requests.Session.request",
                        return_value=FakeResponse(self._payload())):
            report = solana_mint.fetch(SOL_MINT)
        self.assertIsNone(report.mint_authority)
        self.assertIsNone(report.freeze_authority)
        self.assertEqual(solana_mint.dangerous_extensions(report), [])

    def test_absent_authority_key_raises_missing_field(self):
        with mock.patch("requests.Session.request", return_value=FakeResponse(
                self._payload(info={"decimals": 6, "isInitialized": True}))):
            with self.assertRaises(MissingFieldError) as ctx:
                solana_mint.fetch(SOL_MINT)
        self.assertIn("field mintAuthority missing", str(ctx.exception))

    def test_rpc_error_raises(self):
        payload = {"jsonrpc": "2.0", "id": 1,
                   "error": {"code": -32602, "message": "Invalid param"}}
        with mock.patch("requests.Session.request", return_value=FakeResponse(payload)):
            with self.assertRaises(SolanaRPCError):
                solana_mint.fetch(SOL_MINT)

    def test_429_raises(self):
        with mock.patch("requests.Session.request", return_value=FakeResponse({}, 429)):
            with self.assertRaises(SolanaRPCError):
                solana_mint.fetch(SOL_MINT)

    def test_rpc_url_never_prints_itself(self):
        url = solana_mint.RedactedUrl("https://rpc.example/?api-key=super-secret")
        self.assertEqual(repr(url), "<redacted>")
        self.assertEqual(str(url), "<redacted>")
        self.assertNotIn("super-secret", f"{url!r} {url}")

    def test_rpc_url_is_read_from_a_file_path_env_var(self):
        path = os.path.join(os.path.dirname(__file__), "_tmp_rpc_url")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("https://rpc.example/?api-key=super-secret\n")
        try:
            with mock.patch.dict(os.environ, {"SOLANA_RPC_URL_FILE": path}):
                self.assertTrue(solana_mint.rpc_url().value.endswith("super-secret"))
        finally:
            os.remove(path)

    def test_network_error_message_scrubs_the_endpoint(self):
        import requests
        secret = "https://rpc.example/?api-key=super-secret"
        with mock.patch("requests.Session.request",
                        side_effect=requests.ConnectionError(f"failed to reach {secret}")):
            with self.assertRaises(SolanaRPCError) as ctx:
                solana_mint.fetch(SOL_MINT, endpoint=solana_mint.RedactedUrl(secret))
        self.assertNotIn("super-secret", str(ctx.exception))


class TestRegressionsFailOpen(GateTestCase):
    """Each of these PASSED the gate before the fix. All are silent fail-opens."""

    def sol(self, **kw):
        kw.setdefault("dex", lambda a, c: pair_ok(chain_id="solana"))
        return self.screen_sol(**kw)

    def test_extension_with_unreadable_state_is_dangerous(self):
        # A node that cannot parse the extension returns the name with no or
        # partial state.  permanentDelegate = your tokens can be taken; transferHook =
        # your sell can be blocked.  "No state" must never read as "inert".
        for state in (None, {}, "uninitialized", []):
            for name in ("permanentDelegate", "transferHook", "transferFeeConfig"):
                with self.subTest(extension=name, state=state):
                    self.assertRejected(
                        self.sol(mint=lambda a, n=name, s=state: mint_ok(
                            program_id=solana_mint.TOKEN_2022_PROGRAM,
                            extensions={n: s})),
                        "10_token2022_extensions")

    def test_absent_state_key_is_not_defaulted_to_empty(self):
        payload = {"jsonrpc": "2.0", "id": 1, "result": {"value": {
            "owner": solana_mint.TOKEN_2022_PROGRAM,
            "data": {"parsed": {"type": "mint", "info": {
                "mintAuthority": None, "freezeAuthority": None, "decimals": 6,
                "isInitialized": True,
                "extensions": [{"extension": "permanentDelegate"}]}}}}}}
        with mock.patch("requests.Session.request", return_value=FakeResponse(payload)):
            report = solana_mint.fetch(SOL_MINT)
        self.assertIsNone(report.extensions["permanentDelegate"])
        self.assertTrue(solana_mint.dangerous_extensions(report))

    def test_unlocked_tag_does_not_count_as_locked_lp(self):
        # "Unlocked" contains "lock": substring matching used to count 99% of a
        # completely unlocked LP as locked and pass rule 7.
        lp = [{"address": "0xaaaa000000000000000000000000000000000001",
               "tag": "Unlocked team wallet", "is_locked": 0, "percent": "0.99"}]
        self.assertRejected(
            self.screen_bsc(goplus_evm=lambda a: goplus_bsc_ok(lp_holders=lp)),
            "07_lp_locked_or_burned")

    def test_holder_tag_containing_a_hint_substring_is_still_counted(self):
        # "Alpaca" contains "lp": a 60% whale used to be dropped from rule 13.
        holders = [{"address": "0xaaaa000000000000000000000000000000000002",
                    "tag": "Alpaca Finance: Whale", "is_locked": 0, "percent": "0.60"}]
        self.assertRejected(
            self.screen_bsc(goplus_evm=lambda a: goplus_bsc_ok(holders=holders)),
            "13_holder_concentration")

    def test_real_pool_tags_are_still_excluded(self):
        for tag in ("PancakeSwap V2 pair", "Uniswap V3 Pool", "Burn Address",
                    "Null Address", "PinkLock Locker", "Beefy Vault"):
            with self.subTest(tag=tag):
                holders = [{"address": "0xcccc000000000000000000000000000000000009",
                            "tag": tag, "is_locked": 0, "percent": "0.70"},
                           {"address": "0xaaaa000000000000000000000000000000000002",
                            "tag": "", "is_locked": 0, "percent": "0.05"}]
                self.assertPassed(
                    self.screen_bsc(goplus_evm=lambda a: goplus_bsc_ok(holders=holders)))

    def test_mutable_flag_with_an_unexpected_value_is_not_clear(self):
        # Only a literal "0" means "no such permission". ``== "1"`` let "2" and
        # "true" through as if the flag were unset.
        for value in ("1", "2", "true", "yes"):
            with self.subTest(value=value):
                self.assertRejected(
                    self.screen_bsc(goplus_evm=lambda a, v=value:
                                    goplus_bsc_ok(transfer_pausable=v)),
                    "06_goplus_mutable_flags")

    def test_percent_outside_the_0_to_1_fraction_range_rejects(self):
        # If GoPlus ever switched to percents, "5" would read as 500% and one
        # locked holder would satisfy the 95% LP rule on its own.
        lp = [{"address": "0xdead0000000000000000000000000000000000aa",
               "tag": "Locker", "is_locked": 1, "percent": "5"}]
        self.assertRejected(
            self.screen_bsc(goplus_evm=lambda a: goplus_bsc_ok(lp_holders=lp)),
            "07_lp_locked_or_burned", "not a 0..1 fraction")

    def test_price_impact_is_not_truncated_towards_the_pass(self):
        self.assertRejected(self.screen_bsc(price_impact_bps=500.99), "17_price_impact")
        self.assertRejected(self.screen_bsc(price_impact_bps=float("nan")), "17_price_impact")
        self.assertRejected(self.screen_bsc(price_impact_bps=float("inf")), "17_price_impact")
        self.assertRejected(self.screen_bsc(price_impact_bps=True), "17_price_impact")
        self.assertPassed(self.screen_bsc(price_impact_bps=500.0))

    def test_solana_goplus_lookup_is_case_sensitive(self):
        # base58 is case-sensitive; folding case could return another mint's record.
        payload = {"code": 1, "result": {SOL_MINT.lower(): {"holders": []}}}
        with mock.patch("requests.Session.request", return_value=FakeResponse(payload)):
            with self.assertRaises(GoPlusError):
                goplus.fetch_solana(SOL_MINT)

    def test_implausible_pair_created_at_rejects(self):
        # A seconds-epoch value would date a pair minted five minutes ago to
        # 1970 and let rule 15 wave through every brand-new launch.
        for value in (1, -1, int(NOW), int((NOW + 86400) * 1000), 1_420_070_399_000):
            with self.subTest(pairCreatedAt=value):
                self.assertRejected(
                    self.screen_bsc(dex=lambda a, c, v=value: pair_ok(pair_created_at_ms=v)),
                    "15_pair_age")

    def test_non_finite_txn_counts_reject(self):
        # NaN compares False against everything, so "sells < 1" would not fire.
        self.assertRejected(
            self.screen_bsc(dex=lambda a, c: pair_ok(txns_m5_sells=float("nan"))),
            "16_volume_and_flow")
        self.assertRejected(
            self.screen_bsc(dex=lambda a, c: pair_ok(txns_m5_buys=float("inf"))),
            "16_volume_and_flow")

    def test_rugcheck_picks_the_deepest_pool_not_the_most_locked(self):
        report = {"markets": [
            {"lp": {"lpLockedPct": 0.0, "baseUSD": 900_000, "quoteUSD": 900_000,
                    "lpLockedUSD": 0}},                      # the real pool, unlocked
            {"lp": {"lpLockedPct": 100.0, "baseUSD": 50, "quoteUSD": 50,
                    "lpLockedUSD": 10_000_000}},             # decoy claiming a huge lock
        ]}
        self.assertEqual(rugcheck._primary_market(report)["lp"]["lpLockedPct"], 0.0)


class TestRpcEndpointSecrecy(unittest.TestCase):
    """The RPC url embeds an API key. It must not reach a message, log or verdict."""

    KEYED_URLS = ("https://mainnet.helius-rpc.com/?api-key=SUPERSECRETKEY",
                  "https://solana-mainnet.g.alchemy.com/v2/SUPERSECRETKEY",
                  "https://user:SUPERSECRETKEY@rpc.example/v1")

    def _realistic_connection_error(self, url):
        import requests
        from urllib.parse import urlsplit
        split = urlsplit(url)
        tail = split.path + (("?" + split.query) if split.query else "")
        return requests.ConnectionError(
            f"HTTPSConnectionPool(host={split.hostname!r}, port=443): Max retries "
            f"exceeded with url: {tail} (Caused by NewConnectionError('...'))")

    def test_key_is_absent_from_str_repr_args_and_a_log_record(self):
        import logging
        for url in self.KEYED_URLS:
            with self.subTest(url=url):
                exc = self._realistic_connection_error(url)
                with mock.patch("requests.Session.request", side_effect=exc):
                    with self.assertRaises(SolanaRPCError) as ctx:
                        solana_mint.fetch(SOL_MINT,
                                          endpoint=solana_mint.RedactedUrl(url))
                err = ctx.exception
                record = logging.LogRecord("t", logging.ERROR, __file__, 1,
                                           "gate: %s", (err,), None).getMessage()
                for rendered in (str(err), repr(err), repr(err.args), f"{err}", record):
                    self.assertNotIn("SUPERSECRETKEY", rendered)

    def test_key_is_absent_from_the_gate_verdict(self):
        url = self.KEYED_URLS[0]
        exc = self._realistic_connection_error(url)

        def mint_provider(mint):
            return solana_mint.fetch(mint, endpoint=solana_mint.RedactedUrl(url))

        with mock.patch("requests.Session.request", side_effect=exc):
            verdict = screen_token("solana", SOL_MINT, price_impact_bps=100,
                                   providers=make_providers(mint=mint_provider),
                                   allowlist=ALLOWLIST)
        self.assertFalse(verdict.passed)
        self.assertNotIn("SUPERSECRETKEY", json.dumps(verdict.to_dict(), default=str))


class TestAddressHelpers(unittest.TestCase):
    def test_eip55_vectors(self):
        from safety._address import is_checksum_address, keccak256, to_checksum_address
        self.assertEqual(
            keccak256(b"").hex(),
            "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        self.assertEqual(to_checksum_address("0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"),
                         "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed")
        self.assertTrue(is_checksum_address("0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359"))
        self.assertFalse(is_checksum_address("0xfb6916095ca1df60bb79ce92ce3ea74c37c5d359"))
        self.assertFalse(is_checksum_address("0xnothex"))

    def test_solana_mint_validation(self):
        from safety._address import is_valid_solana_mint
        self.assertTrue(is_valid_solana_mint(SOL_MINT))
        self.assertFalse(is_valid_solana_mint("0OIl"))
        self.assertFalse(is_valid_solana_mint(BSC_TOKEN))


if __name__ == "__main__":
    unittest.main()
