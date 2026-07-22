"""Unit tests for chains/bitget_swap.py (Bitget Wallet SwapX back-end).

ZERO network, ZERO credentials. HTTP is faked at the ``requests.Session``
boundary and ``eth_account``/``solders`` are injected into ``sys.modules``, so
this file runs on a host that has neither package installed.

WHAT IS DELIBERATELY NOT TESTED
    The SwapX wire format. Nobody has ever called this API, so asserting that
    "the request body really is ``{chainIndex: ...}``" would only encode a
    guess as a fact. Every assertion below is about OUR logic - dry-run
    containment, signer selection, chain routing, error mapping, the gasless
    minimum, the symbol ban, slippage bounds, response normalisation, and the
    signed/unsigned failure split.

WHEN THE REAL API IS FIRST EXERCISED
    Correct ``bitget_swap.WIRE`` (and ``PATHS``/``CHAIN_INDEX``). ``FakeVenue``
    below builds its canned payloads THROUGH ``WIRE``, so these tests follow
    the correction automatically instead of needing a second edit.
"""

import inspect
import json
import logging
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
logging.getLogger("chains").addHandler(logging.NullHandler())
logging.getLogger("chains").propagate = False

from chains import bitget_swap as bs  # noqa: E402
from chains.errors import (  # noqa: E402
    ChainError, InsufficientGas, QuoteStale, SwapReverted,
)
from chains.wallet_keys import SecretKey  # noqa: E402

W = bs.WIRE

TOKEN_BSC = "0x1111111111111111111111111111111111111111"
WALLET_BSC = "0x2222222222222222222222222222222222222222"
MINT_SOL = "So11111111111111111111111111111111111111112"
WALLET_SOL = "TakerWa11etPubkey1111111111111111111111111"
EVM_KEY = SecretKey("0x" + "ab" * 32, label="test")     # not funded; never leaves this file
SOL_KEY = SecretKey("5" + "K" * 43, label="test")


# ---------- fake signing stacks (installed into sys.modules per test) ----------

class FakeAccount:
    """Records which eth_account entry point was used, and with what."""

    calls = []

    @classmethod
    def reset(cls):
        cls.calls = []

    @classmethod
    def sign_transaction(cls, tx, private_key=None):
        cls.calls.append(("sign_transaction", tx))
        return types.SimpleNamespace(raw_transaction=b"\xde\xad\xbe\xef")

    @classmethod
    def sign_typed_data(cls, private_key, full_message=None):
        cls.calls.append(("sign_typed_data", full_message))
        return types.SimpleNamespace(signature=b"\xca\xfe\xba\xbe")


class FakeKeypair:
    @classmethod
    def from_bytes(cls, raw):
        return cls()

    @classmethod
    def from_base58_string(cls, text):
        return cls()


class FakeVersionedTransaction:
    """VersionedTransaction is immutable: signing must RECONSTRUCT, never mutate."""

    reconstructed = []

    def __init__(self, message, signers=None):
        self.message, self.signers = message, signers
        if signers is not None:
            FakeVersionedTransaction.reconstructed.append((message, signers))

    @classmethod
    def from_bytes(cls, raw):
        return cls(f"message-of-{len(raw)}-bytes")

    def __bytes__(self):
        return b"signed-solana-tx"


def install_fake_signers():
    """Put fake eth_account/solders on sys.modules; return an undo callable."""
    FakeAccount.reset()
    FakeVersionedTransaction.reconstructed = []
    saved = {name: sys.modules.get(name) for name in
             ("eth_account", "solders", "solders.keypair", "solders.transaction")}

    eth_account = types.ModuleType("eth_account")
    eth_account.Account = FakeAccount
    solders = types.ModuleType("solders")
    solders.__path__ = []
    keypair_mod = types.ModuleType("solders.keypair")
    keypair_mod.Keypair = FakeKeypair
    tx_mod = types.ModuleType("solders.transaction")
    tx_mod.VersionedTransaction = FakeVersionedTransaction

    sys.modules.update({"eth_account": eth_account, "solders": solders,
                        "solders.keypair": keypair_mod, "solders.transaction": tx_mod})

    def undo():
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    return undo


# ---------- fake venue ----------

def _envelope(data, code=None):
    return {W["envelope_code"][0]: W["success_codes"][0] if code is None else code,
            W["envelope_msg"][0]: "success",
            W["envelope_data"][0]: data}


def quote_data(expected_out=1000, **extra):
    data = {W["expected_out"][0]: str(expected_out),
            W["route"][0]: [{"dexName": "PancakeSwapV3"}],
            W["price_impact"][0]: "0.12"}
    data.update(extra)
    return data


def order_data(chain="bsc", order_id="ORD-1", **extra):
    data = {W["order_id"][0]: order_id, W["min_out"][0]: "990"}
    if chain == "bsc":
        data[W["evm_tx"][0]] = {"to": TOKEN_BSC, "data": "0xabcdef", "value": 0}
        data[W["typed_data"][0]] = {"domain": {}, "types": {}, "message": {}}
    else:
        data[W["solana_tx"][0]] = "AQAAAA=="
    data.update(extra)
    return data


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload, self.status_code = payload, status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeVenue:
    """Records every HTTP call and replies from a per-path script."""

    def __init__(self, **paths):
        self.calls = []
        self.script = {
            "quote": _envelope(quote_data()),
            "make_order": _envelope(order_data()),
            "submit_order": _envelope({W["order_id"][0]: "ORD-1",
                                       W["tx_hash"][0]: "0xfeedface"}),
            "get_order": _envelope({W["order_id"][0]: "ORD-1",
                                    W["tx_hash"][0]: "0xfeedface",
                                    W["order_status"][0]: W["success_order_status"][0]}),
        }
        self.script.update(paths)

    def _name_for(self, url):
        for name, path in bs.PATHS.items():
            if url.endswith(path):
                return name
        raise AssertionError(f"unexpected URL {url}")

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        name = self._name_for(url)
        self.calls.append({"name": name, "method": method, "url": url,
                           "params": params, "body": json, "headers": headers})
        reply = self.script[name]
        if isinstance(reply, Exception):
            raise reply
        # A bare dict is the JSON body; anything else is already a response object.
        return FakeResponse(reply) if isinstance(reply, dict) else reply

    # convenience
    def names(self):
        return [c["name"] for c in self.calls]


def bsc_kwargs(**over):
    kwargs = dict(usdt_amount=10, slippage_bps=100, token_address=TOKEN_BSC,
                  recipient=WALLET_BSC, key=EVM_KEY)
    kwargs.update(over)
    return kwargs


def sol_kwargs(**over):
    kwargs = dict(usdt_amount=10, slippage_bps=100, mint=MINT_SOL,
                  taker=WALLET_SOL, key=SOL_KEY)
    kwargs.update(over)
    return kwargs


def client_for(venue, **over):
    return bs.BitgetSwapClient(session=venue, **over)


class SwapXTestCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(install_fake_signers())


# ---------- dry run containment ----------

class TestDryRunNeverSpends(SwapXTestCase):
    def test_dry_run_quotes_only_and_signs_nothing(self):
        venue = FakeVenue()
        result = bs.buy_token(dry_run=True, client=client_for(venue), **bsc_kwargs())
        self.assertEqual(venue.names(), ["quote"])          # no make/submit
        self.assertEqual(FakeAccount.calls, [])             # nothing signed
        self.assertEqual(FakeVersionedTransaction.reconstructed, [])
        self.assertEqual(result["status"], "dry_run")
        self.assertIs(result["dry_run"], True)
        self.assertIsNone(result["tx_hash"])
        self.assertIsNone(result["signature"])

    def test_dry_run_is_the_default(self):
        venue = FakeVenue()
        result = bs.buy_token(client=client_for(venue), **bsc_kwargs())
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(venue.names(), ["quote"])
        self.assertIs(inspect.signature(bs.buy_token).parameters["dry_run"].default, True)

    def test_dry_run_solana_signs_nothing_either(self):
        venue = FakeVenue(make_order=_envelope(order_data("solana")))
        result = bs.buy_token(dry_run=True, client=client_for(venue), **sol_kwargs())
        self.assertEqual(venue.names(), ["quote"])
        self.assertEqual(FakeVersionedTransaction.reconstructed, [])
        self.assertEqual(result["chain"], "solana")

    def test_dry_run_reports_the_call_it_would_have_made(self):
        venue = FakeVenue()
        result = bs.buy_token(dry_run=True, client=client_for(venue), **bsc_kwargs())
        self.assertIn(bs.PATHS["make_order"], result["intended_call"]["endpoint"])
        self.assertIn("body", result["intended_call"])

    def test_non_bool_dry_run_is_refused_before_anything_happens(self):
        venue = FakeVenue()
        for value in (None, 0, 1, "false", ""):
            with self.subTest(value=value):
                with self.assertRaises(bs.BitgetSwapError) as ctx:
                    bs.buy_token(dry_run=value, client=client_for(venue), **bsc_kwargs())
                self.assertEqual(ctx.exception.code, "BAD_DRY_RUN")
        self.assertEqual(venue.calls, [])


# ---------- chain routing / signer selection ----------

class TestChainRouting(SwapXTestCase):
    def test_bsc_signs_a_transaction_with_eth_account(self):
        venue = FakeVenue()
        result = bs.buy_token(dry_run=False, client=client_for(venue), **bsc_kwargs())
        self.assertEqual([c[0] for c in FakeAccount.calls], ["sign_transaction"])
        self.assertEqual(FakeVersionedTransaction.reconstructed, [])
        self.assertEqual(result["chain"], "bsc")

    def test_solana_reconstructs_and_signs_with_solders(self):
        venue = FakeVenue(make_order=_envelope(order_data("solana")))
        result = bs.buy_token(dry_run=False, client=client_for(venue), **sol_kwargs())
        self.assertEqual(FakeAccount.calls, [])
        self.assertEqual(len(FakeVersionedTransaction.reconstructed), 1)
        # Reconstruct-and-sign, never sign in place.
        message, signers = FakeVersionedTransaction.reconstructed[0]
        self.assertTrue(str(message).startswith("message-of-"))
        self.assertEqual(len(signers), 1)
        self.assertEqual(result["chain"], "solana")

    def test_gasless_evm_signs_typed_data_not_a_transaction(self):
        venue = FakeVenue()
        bs.buy_token(dry_run=False, gasless=True, client=client_for(venue, gasless=True),
                     **bsc_kwargs(usdt_amount=25))
        self.assertEqual([c[0] for c in FakeAccount.calls], ["sign_typed_data"])

    def test_chain_is_inferred_from_the_identifier_given(self):
        self.assertEqual(bs.resolve_chain(None, TOKEN_BSC, None), "bsc")
        self.assertEqual(bs.resolve_chain(None, None, MINT_SOL), "solana")
        self.assertEqual(bs.resolve_chain("SOLANA", None, None), "solana")

    def test_ambiguous_identifiers_are_refused_rather_than_guessed(self):
        with self.assertRaises(bs.BitgetSwapError) as ctx:
            bs.resolve_chain(None, TOKEN_BSC, MINT_SOL)
        self.assertEqual(ctx.exception.code, "BAD_CHAIN")

    def test_unknown_chain_is_refused(self):
        with self.assertRaises(bs.BitgetSwapError):
            bs.resolve_chain("ethereum", None, None)

    def test_wallet_must_match_the_chain(self):
        venue = FakeVenue()
        with self.assertRaises(bs.BitgetSwapError) as ctx:
            bs.buy_token(client=client_for(venue), **bsc_kwargs(recipient=WALLET_SOL))
        self.assertEqual(ctx.exception.code, "BAD_WALLET")

    def test_a_wallet_is_required(self):
        venue = FakeVenue()
        kwargs = bsc_kwargs()
        kwargs.pop("recipient")
        with self.assertRaises(bs.BitgetSwapError) as ctx:
            bs.buy_token(client=client_for(venue), **kwargs)
        self.assertEqual(ctx.exception.code, "NO_WALLET")


# ---------- input validation ----------

class TestSymbolsAreNeverResolved(SwapXTestCase):
    def test_symbol_shaped_input_is_refused(self):
        for chain, symbol in (("bsc", "CAKE"), ("bsc", "USDT"), ("solana", "BONK"),
                              ("solana", "JUP"), ("bsc", "0xdeadbeef")):
            with self.subTest(chain=chain, symbol=symbol):
                with self.assertRaises(bs.BitgetSwapError) as ctx:
                    bs.validate_token(chain, symbol)
                self.assertEqual(ctx.exception.code, "BAD_TOKEN")

    def test_homoglyph_mint_is_refused(self):
        with self.assertRaises(bs.BitgetSwapError):
            bs.validate_token("solana", "So1111111111111111111111111111111111111111а")

    def test_buying_the_input_usdt_is_refused(self):
        with self.assertRaises(bs.BitgetSwapError) as ctx:
            bs.validate_token("bsc", bs.USDT["bsc"])
        self.assertEqual(ctx.exception.code, "SAME_TOKEN")

    def test_a_symbol_never_reaches_the_network(self):
        venue = FakeVenue()
        with self.assertRaises(bs.BitgetSwapError):
            bs.buy_token(client=client_for(venue), **bsc_kwargs(token_address="CAKE"))
        self.assertEqual(venue.calls, [])


class TestSlippageBound(SwapXTestCase):
    def test_out_of_range_is_refused(self):
        for value in (0, -1, bs.MAX_SLIPPAGE_BPS + 1, 100000):
            with self.subTest(value=value):
                with self.assertRaises(bs.BitgetSwapError) as ctx:
                    bs.validate_slippage(value)
                self.assertEqual(ctx.exception.code, "SLIPPAGE_RANGE")

    def test_non_int_is_refused_because_a_float_widens_the_floor(self):
        for value in (1.5, "100", True, None):
            with self.subTest(value=value):
                with self.assertRaises(bs.BitgetSwapError) as ctx:
                    bs.validate_slippage(value)
                self.assertEqual(ctx.exception.code, "SLIPPAGE_TYPE")

    def test_in_range_passes(self):
        self.assertEqual(bs.validate_slippage(bs.MAX_SLIPPAGE_BPS), bs.MAX_SLIPPAGE_BPS)

    def test_bps_never_render_as_scientific_notation_on_the_wire(self):
        # Decimal.normalize() emits "1E-4" style strings for some values, which a
        # venue would parse as 1, i.e. 100% slippage. Every legal bps must be
        # plain decimal text.
        for value in range(1, bs.MAX_SLIPPAGE_BPS + 1):
            rendered = bs._bps_to_fraction(value)
            self.assertNotIn("E", rendered.upper(), value)
            self.assertEqual(float(rendered), value / 10_000)

    def test_bad_slippage_never_reaches_the_network(self):
        venue = FakeVenue()
        with self.assertRaises(bs.BitgetSwapError):
            bs.buy_token(client=client_for(venue), **bsc_kwargs(slippage_bps=0))
        self.assertEqual(venue.calls, [])


class TestAmount(SwapXTestCase):
    def test_non_positive_amount_is_refused(self):
        venue = FakeVenue()
        for value in (0, -1, "0", None):
            with self.subTest(value=value):
                with self.assertRaises(bs.BitgetSwapError):
                    bs.buy_token(client=client_for(venue), **bsc_kwargs(usdt_amount=value))
        self.assertEqual(venue.calls, [])

    def test_bsc_usdt_is_18_decimals_not_6(self):
        venue = FakeVenue()
        bs.buy_token(client=client_for(venue), **bsc_kwargs(usdt_amount=10))
        self.assertEqual(bs.USDT_DECIMALS["bsc"], 18)
        self.assertEqual(bs.USDT_DECIMALS["solana"], 6)
        self.assertEqual(bs._to_base_units(10, 18), 10 * 10 ** 18)

    def test_amount_truncates_and_never_rounds_a_spend_up(self):
        self.assertEqual(bs._to_base_units("1.9999999", 6), 1_999_999)


class TestGaslessMinimum(SwapXTestCase):
    def test_below_the_minimum_is_refused_before_any_call(self):
        venue = FakeVenue()
        with self.assertRaises(bs.BitgetSwapError) as ctx:
            bs.buy_token(gasless=True, client=client_for(venue, gasless=True),
                         **bsc_kwargs(usdt_amount=1))
        self.assertEqual(ctx.exception.code, "GASLESS_MINIMUM")
        self.assertEqual(venue.calls, [])

    def test_at_the_minimum_is_allowed(self):
        venue = FakeVenue()
        result = bs.buy_token(gasless=True, client=client_for(venue, gasless=True),
                              **bsc_kwargs(usdt_amount=bs.GASLESS_MIN_USDT))
        self.assertIs(result["gasless"], True)

    def test_a_gasless_client_cannot_be_silently_downgraded_by_the_keyword(self):
        # gasless=False + a gasless client must still enforce the $5 gate.
        venue = FakeVenue()
        with self.assertRaises(bs.BitgetSwapError) as ctx:
            bs.buy_token(gasless=False, client=client_for(venue, gasless=True),
                         **bsc_kwargs(usdt_amount=1))
        self.assertEqual(ctx.exception.code, "GASLESS_MINIMUM")

    def test_the_keyword_cannot_disagree_with_the_client_that_builds_the_order(self):
        # REGRESSION: gasless=True + a non-gasless client made makeSwapOrder
        # build a NORMAL order which was then signed as EIP-712 typed data - a
        # signature of the wrong kind, submitted as if it were right.
        venue = FakeVenue()
        with self.assertRaises(bs.BitgetSwapError) as ctx:
            bs.buy_token(dry_run=False, gasless=True, client=client_for(venue, gasless=False),
                         **bsc_kwargs(usdt_amount=25))
        self.assertEqual(ctx.exception.code, "GASLESS_MISMATCH")
        self.assertIs(ctx.exception.signed, False)
        self.assertEqual(venue.calls, [])
        self.assertEqual(FakeAccount.calls, [])

    def test_the_minimum_does_not_apply_when_gas_is_paid_normally(self):
        venue = FakeVenue()
        result = bs.buy_token(client=client_for(venue), **bsc_kwargs(usdt_amount=1))
        self.assertIs(result["gasless"], False)


# ---------- error mapping ----------

class TestErrorMapping(SwapXTestCase):
    def test_a_venue_code_in_a_200_body_becomes_a_BitgetSwapError(self):
        venue = FakeVenue(quote={W["envelope_code"][0]: "40037",
                                 W["envelope_msg"][0]: "Apikey does not exist",
                                 W["envelope_data"][0]: None})
        with self.assertRaises(bs.BitgetSwapError) as ctx:
            bs.buy_token(client=client_for(venue), **bsc_kwargs())
        exc = ctx.exception
        self.assertEqual(exc.venue_code, "40037")
        self.assertIn("Apikey does not exist", exc.message)
        self.assertIsInstance(exc, ChainError)      # catchable with the other back-ends
        self.assertIs(exc.signed, False)            # definitely not sent

    def test_http_error_status_becomes_a_typed_error(self):
        venue = FakeVenue(quote=FakeResponse({W["envelope_code"][0]: "50001",
                                              W["envelope_msg"][0]: "server error"}, 500))
        with self.assertRaises(bs.BitgetSwapError) as ctx:
            bs.buy_token(client=client_for(venue), **bsc_kwargs())
        self.assertEqual(ctx.exception.http_status, 500)

    def test_rate_limit_is_its_own_code(self):
        venue = FakeVenue(quote=FakeResponse({W["envelope_code"][0]: "0"}, 429))
        with self.assertRaises(bs.BitgetSwapError) as ctx:
            bs.buy_token(client=client_for(venue), **bsc_kwargs())
        self.assertEqual(ctx.exception.code, "RATE_LIMITED")

    def test_route_and_slippage_codes_stay_catchable_as_the_shared_types(self):
        self.assertIsInstance(bs.classify_error("51001", "no route found"), QuoteStale)
        self.assertIsInstance(bs.classify_error("51002", "slippage exceeded"), SwapReverted)
        self.assertIsInstance(bs.classify_error("51003", "insufficient gas"), InsufficientGas)
        # ...and as BitgetSwapError, carrying the venue's own code.
        for code, message in (("51001", "no route found"), ("51002", "slippage exceeded"),
                              ("51003", "insufficient gas")):
            exc = bs.classify_error(code, message)
            self.assertIsInstance(exc, bs.BitgetSwapError)
            self.assertEqual(exc.venue_code, code)

    def test_network_failure_fails_closed(self):
        venue = FakeVenue(quote=OSError("connection reset"))
        with self.assertRaises(bs.BitgetSwapError) as ctx:
            bs.buy_token(client=client_for(venue), **bsc_kwargs())
        self.assertEqual(ctx.exception.code, "NETWORK")

    def test_non_json_body_fails_closed(self):
        class Garbage:
            status_code = 200
            text = "<html>maintenance</html>"

            def json(self):
                raise ValueError("not json")

        venue = FakeVenue(quote=Garbage())
        with self.assertRaises(bs.BitgetSwapError) as ctx:
            bs.buy_token(client=client_for(venue), **bsc_kwargs())
        self.assertEqual(ctx.exception.code, "BAD_RESPONSE")

    def test_a_missing_data_member_is_never_read_as_success(self):
        venue = FakeVenue(quote={W["envelope_code"][0]: W["success_codes"][0],
                                 W["envelope_msg"][0]: "success"})
        with self.assertRaises(bs.BitgetSwapError) as ctx:
            bs.buy_token(client=client_for(venue), **bsc_kwargs())
        self.assertEqual(ctx.exception.code, "NO_DATA")

    def test_a_quote_with_no_output_field_is_refused(self):
        venue = FakeVenue(quote=_envelope({W["route"][0]: []}))
        with self.assertRaises(bs.BitgetSwapError) as ctx:
            bs.buy_token(client=client_for(venue), **bsc_kwargs())
        self.assertEqual(ctx.exception.code, "MISSING_FIELD")

    def test_a_zero_quote_is_stale_not_a_trade(self):
        venue = FakeVenue(quote=_envelope(quote_data(expected_out=0)))
        with self.assertRaises(QuoteStale):
            bs.buy_token(client=client_for(venue), **bsc_kwargs())


# ---------- the signed / not-signed split ----------

class TestSignedVersusUnsigned(SwapXTestCase):
    def test_a_pre_signing_failure_says_definitely_not_sent(self):
        venue = FakeVenue(make_order=OSError("connection reset"))
        with self.assertRaises(bs.BitgetSwapError) as ctx:
            bs.buy_token(dry_run=False, client=client_for(venue), **bsc_kwargs())
        exc = ctx.exception
        self.assertIs(exc.signed, False)
        self.assertNotIsInstance(exc, bs.BitgetSwapUnconfirmed)
        self.assertEqual(FakeAccount.calls, [])

    def test_a_submit_failure_after_signing_is_reported_as_unconfirmed(self):
        venue = FakeVenue(submit_order=OSError("read timeout"))
        with self.assertRaises(bs.BitgetSwapUnconfirmed) as ctx:
            bs.buy_token(dry_run=False, client=client_for(venue), **bsc_kwargs())
        exc = ctx.exception
        self.assertIs(exc.signed, True)
        self.assertEqual(exc.code, "SUBMIT_UNCONFIRMED")
        self.assertEqual(exc.order_id, "ORD-1")
        self.assertIn("reconcile", exc.message.lower())
        self.assertEqual([c[0] for c in FakeAccount.calls], ["sign_transaction"])

    def test_the_two_failures_are_distinguishable_by_type_and_flag(self):
        # This is the whole point: a caller must be able to tell "retry is safe"
        # from "you may have already bought".
        self.assertTrue(issubclass(bs.BitgetSwapUnconfirmed, bs.BitgetSwapError))
        self.assertIs(bs.BitgetSwapError.signed, False)
        self.assertIs(bs.BitgetSwapUnconfirmed.signed, True)

    def test_a_submit_with_no_hash_and_no_fill_is_unconfirmed_not_success(self):
        venue = FakeVenue(
            submit_order=_envelope({W["order_id"][0]: "ORD-1"}),
            get_order=_envelope({W["order_id"][0]: "ORD-1",
                                 W["order_status"][0]: "pending"}))
        with self.assertRaises(bs.BitgetSwapUnconfirmed) as ctx:
            bs.buy_token(dry_run=False, client=client_for(venue),
                         poll_attempts=2, sleep=lambda _s: None, **bsc_kwargs())
        self.assertEqual(ctx.exception.code, "FILL_UNCONFIRMED")

    def test_a_failed_order_status_is_a_revert_not_a_success(self):
        venue = FakeVenue(
            submit_order=_envelope({W["order_id"][0]: "ORD-1"}),
            get_order=_envelope({W["order_id"][0]: "ORD-1",
                                 W["order_status"][0]: W["failed_order_status"][0]}))
        with self.assertRaises(SwapReverted):
            bs.buy_token(dry_run=False, client=client_for(venue),
                         sleep=lambda _s: None, **bsc_kwargs())

    def test_polling_recovers_the_hash_when_submit_omitted_it(self):
        venue = FakeVenue(submit_order=_envelope({W["order_id"][0]: "ORD-1"}))
        result = bs.buy_token(dry_run=False, client=client_for(venue),
                              sleep=lambda _s: None, **bsc_kwargs())
        self.assertEqual(result["tx_hash"], "0xfeedface")
        self.assertIn("get_order", venue.names())

    def test_an_order_protecting_less_than_the_quote_is_refused_before_signing(self):
        venue = FakeVenue(make_order=_envelope(order_data(**{W["min_out"][0]: "1"})))
        with self.assertRaises(bs.BitgetSwapError) as ctx:
            bs.buy_token(dry_run=False, client=client_for(venue), **bsc_kwargs())
        self.assertEqual(ctx.exception.code, "MIN_RECEIVED")
        self.assertEqual(FakeAccount.calls, [])

    def test_an_order_without_signable_data_is_refused_before_submit(self):
        venue = FakeVenue(make_order=_envelope({W["order_id"][0]: "ORD-1"}))
        with self.assertRaises(bs.BitgetSwapError) as ctx:
            bs.buy_token(dry_run=False, client=client_for(venue), **bsc_kwargs())
        self.assertEqual(ctx.exception.code, "MISSING_FIELD")
        self.assertNotIn("submit_order", venue.names())

    def test_every_post_signing_failure_says_a_signature_existed(self):
        # REGRESSION: `signed is False` is the caller's licence to retry. Any
        # error raised after sign_order() that still carries it invites a second
        # real buy. Drive every post-signing failure and check the flag.
        scripts = {
            "submit lost": dict(submit_order=OSError("read timeout")),
            "no hash, no order id": dict(make_order=_envelope(order_data(order_id=None)),
                                         submit_order=_envelope({})),
            "never fills": dict(submit_order=_envelope({W["order_id"][0]: "ORD-1"}),
                                get_order=_envelope({W["order_status"][0]: "pending"})),
            "order reverted": dict(
                submit_order=_envelope({W["order_id"][0]: "ORD-1"}),
                get_order=_envelope({W["order_status"][0]: W["failed_order_status"][0]})),
        }
        for name, script in scripts.items():
            with self.subTest(name):
                FakeAccount.reset()
                venue = FakeVenue(**script)
                with self.assertRaises(bs.BitgetSwapError) as ctx:
                    bs.buy_token(dry_run=False, client=client_for(venue), poll_attempts=2,
                                 sleep=lambda _s: None, **bsc_kwargs())
                self.assertEqual([c[0] for c in FakeAccount.calls], ["sign_transaction"])
                self.assertIs(ctx.exception.signed, True, name)
        # The class default stays False: only the raised instance is marked.
        self.assertIs(bs.BitgetSwapError.signed, False)
        self.assertIs(bs.BitgetSwapReverted.signed, False)

    def test_a_garbled_fill_amount_never_fails_a_trade_that_already_happened(self):
        # REGRESSION: the swap is submitted and its hash is in hand; raising
        # BAD_RESPONSE over a cosmetic field handed the caller signed=False.
        venue = FakeVenue(submit_order=_envelope({W["order_id"][0]: "ORD-1",
                                                  W["tx_hash"][0]: "0xfeedface",
                                                  W["expected_out"][0]: "1.5e21"}))
        result = bs.buy_token(dry_run=False, client=client_for(venue),
                              sleep=lambda _s: None, **bsc_kwargs())
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["tx_hash"], "0xfeedface")
        self.assertEqual(result["expected_out"], 1000)      # the quoted figure, kept
        self.assertEqual(result["out_amount_reported"], "1.5e21")

    def test_a_non_int_poll_attempts_is_normalised_before_anything_is_signed(self):
        # REGRESSION: `attempt < attempts` raised a bare TypeError AFTER submit,
        # i.e. an error with no `signed` flag at all.
        venue = FakeVenue(submit_order=_envelope({W["order_id"][0]: "ORD-1"}),
                          get_order=_envelope({W["order_status"][0]: "pending"}))
        with self.assertRaises(bs.BitgetSwapUnconfirmed) as ctx:
            bs.buy_token(dry_run=False, client=client_for(venue), poll_attempts="2",
                         sleep=lambda _s: None, **bsc_kwargs())
        self.assertIs(ctx.exception.signed, True)
        venue = FakeVenue()
        with self.assertRaises(bs.BitgetSwapError) as ctx:
            bs.buy_token(client=client_for(venue), poll_attempts="soon", **bsc_kwargs())
        self.assertEqual(venue.calls, [])                   # refused pre-flight

    def test_a_signer_failure_never_echoes_key_material(self):
        def explode(tx, private_key=None):
            raise ValueError(f"bad key {private_key}")   # a signer that echoes its input

        venue = FakeVenue()
        with mock.patch.object(FakeAccount, "sign_transaction", explode):
            with self.assertRaises(bs.BitgetSwapError) as ctx:
                bs.buy_token(dry_run=False, client=client_for(venue), **bsc_kwargs())
        exc = ctx.exception
        self.assertEqual(exc.code, "SIGN_FAILED")
        self.assertNotIn("ab" * 32, str(exc))
        self.assertIsNone(exc.__cause__)     # no chained traceback carrying the key


# ---------- response normalisation ----------

class TestResponseNormalisation(SwapXTestCase):
    def test_result_carries_the_backend_tag_and_both_identifier_names(self):
        venue = FakeVenue()
        result = bs.buy_token(dry_run=False, client=client_for(venue), **bsc_kwargs())
        self.assertEqual(result["backend"], "bitget_swapx")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["tx_hash"], "0xfeedface")      # bsc_swap's key
        self.assertEqual(result["signature"], "0xfeedface")    # solana_swap's key
        self.assertEqual(result["order_id"], "ORD-1")

    def test_amount_fields_use_both_back_ends_names_for_the_same_number(self):
        venue = FakeVenue()
        result = bs.buy_token(dry_run=False, client=client_for(venue), **bsc_kwargs())
        self.assertEqual(result["amount_in"], result["in_amount"])
        self.assertEqual(result["expected_out"], result["out_amount"])
        self.assertEqual(result["amount_in"], 10 * 10 ** 18)   # 18-decimal BSC USDT

    def test_amount_out_min_applies_the_slippage_we_asked_for(self):
        venue = FakeVenue(quote=_envelope(quote_data(expected_out=10_000)))
        result = bs.buy_token(client=client_for(venue), **bsc_kwargs(slippage_bps=250))
        self.assertEqual(result["amount_out_min"], 10_000 * 9_750 // 10_000)

    def test_solana_result_normalises_to_the_same_shape(self):
        venue = FakeVenue(make_order=_envelope(order_data("solana")),
                          submit_order=_envelope({W["order_id"][0]: "ORD-9",
                                                  W["tx_hash"][0]: "5xSig"}))
        result = bs.buy_token(dry_run=False, client=client_for(venue), **sol_kwargs())
        self.assertEqual(result["backend"], "bitget_swapx")
        self.assertEqual(result["signature"], "5xSig")
        self.assertEqual(result["tx_hash"], "5xSig")
        self.assertEqual(result["amount_in"], 10 * 10 ** 6)    # 6-decimal Solana USDT

    def test_the_result_admits_the_integration_is_unverified(self):
        venue = FakeVenue()
        result = bs.buy_token(client=client_for(venue), **bsc_kwargs())
        self.assertIs(result["unverified_integration"], True)
        # No RPC handle -> no native-balance proof, and the result says so.
        self.assertIs(result["native_balance_checked"], False)

    def test_min_native_balance_is_accepted_but_reported_as_unenforced(self):
        venue = FakeVenue()
        with self.assertLogs("chains.bitget_swap", level="WARNING") as logs:
            result = bs.buy_token(client=client_for(venue),
                                  **bsc_kwargs(min_native_balance=0.05))
        self.assertIs(result["native_balance_checked"], False)
        self.assertTrue(any("min_native_balance" in line for line in logs.output))


# ---------- client / auth ----------

class TestClient(SwapXTestCase):
    def test_default_host_is_the_documented_production_one_and_overridable(self):
        self.assertEqual(bs.DEFAULT_BASE_URL, "https://bopenapi.bgwapi.io")
        self.assertEqual(bs.BitgetSwapClient().base_url, bs.DEFAULT_BASE_URL)
        self.assertEqual(bs.BitgetSwapClient(base_url="https://x/").base_url, "https://x")
        # The keyless client-facing host is available but is NOT the default.
        self.assertEqual(bs.CLIENT_BASE_URL, "https://copenapi.bgwapi.io")
        self.assertNotEqual(bs.CLIENT_BASE_URL, bs.DEFAULT_BASE_URL)

    def test_credentials_must_be_SecretKey_not_raw_strings(self):
        with self.assertRaises(bs.BitgetSwapError) as ctx:
            bs.BitgetSwapClient(api_key="plain-string")
        self.assertEqual(ctx.exception.code, "KEY_TYPE")
        with self.assertRaises(bs.BitgetSwapError):
            bs.BitgetSwapClient(api_secret="plain-string")

    def test_a_raw_wallet_key_is_refused(self):
        venue = FakeVenue()
        with self.assertRaises(bs.BitgetSwapError) as ctx:
            bs.buy_token(dry_run=False, client=client_for(venue),
                         **bsc_kwargs(key="0x" + "ab" * 32))
        self.assertEqual(ctx.exception.code, "KEY_TYPE")

    def test_auth_headers_live_in_one_method_and_carry_key_sign_and_timestamp(self):
        client = bs.BitgetSwapClient(api_key=SecretKey("KEY123"),
                                     api_secret=SecretKey("SECRET456"))
        headers = client._auth_headers("POST", "/p", '{"a":1}')
        self.assertEqual(headers[client.HEADER_API_KEY], "KEY123")
        self.assertIn(client.HEADER_SIGN, headers)
        self.assertIn(client.HEADER_TIMESTAMP, headers)
        # The signature is a digest, never the secret itself.
        self.assertNotIn("SECRET456", json.dumps(headers))

    def test_the_signing_recipe_is_deterministic_for_a_fixed_clock(self):
        client = bs.BitgetSwapClient(api_key=SecretKey("K"), api_secret=SecretKey("S"))
        with mock.patch.object(bs.time, "time", lambda: 1_700_000_000.0):
            first = client._auth_headers("GET", "/p", "")
            second = client._auth_headers("GET", "/p", "")
        self.assertEqual(first, second)

    def test_no_secret_means_only_the_key_header_which_is_the_keyless_host_shape(self):
        client = bs.BitgetSwapClient(api_key=SecretKey("K"))
        headers = client._auth_headers("GET", "/p", "")
        self.assertIn(client.HEADER_API_KEY, headers)
        self.assertNotIn(client.HEADER_SIGN, headers)
        self.assertNotIn(bs.BitgetSwapClient().HEADER_API_KEY,
                         bs.BitgetSwapClient()._auth_headers("GET", "/p", ""))


# ---------- portability + orchestrator compatibility ----------

class _BlockImport:
    """Meta-path hook that makes the named packages un-importable."""

    def __init__(self, *names):
        self.names = names

    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".")[0]
        if root in self.names:
            raise ImportError(f"{fullname} is not installed (blocked by the test)")
        return None


class TestPortability(unittest.TestCase):
    def test_module_imports_without_the_web3_or_solana_stacks(self):
        # Asserting "not in sys.modules" is not enough: a sibling test module
        # injects fake solders/eth_account for the whole process. Re-import the
        # module with those packages made genuinely un-importable instead.
        import importlib

        blocked = ("web3", "eth_account", "solders")
        saved = {name: sys.modules.pop(name, None) for name in
                 (*blocked, "solders.keypair", "solders.transaction",
                  "chains.bitget_swap")}
        blocker = _BlockImport(*blocked)
        sys.meta_path.insert(0, blocker)
        try:
            module = importlib.import_module("chains.bitget_swap")
            self.assertTrue(callable(module.buy_token))
            for name in blocked:
                self.assertNotIn(name, sys.modules, f"{name} must be imported lazily")
        finally:
            sys.meta_path.remove(blocker)
            for name, saved_module in saved.items():
                if saved_module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = saved_module

    def test_a_missing_signing_stack_is_a_typed_error_not_an_ImportError(self):
        blocker = _BlockImport("eth_account", "solders")
        saved = {name: sys.modules.pop(name, None) for name in
                 ("eth_account", "solders", "solders.keypair", "solders.transaction")}
        sys.meta_path.insert(0, blocker)
        try:
            venue = FakeVenue()
            with self.assertRaises(bs.BitgetSwapError) as ctx:
                bs.buy_token(dry_run=False, client=client_for(venue), **bsc_kwargs())
            self.assertEqual(ctx.exception.code, "DEP_MISSING")
        finally:
            sys.meta_path.remove(blocker)
            for name, saved_module in saved.items():
                if saved_module is not None:
                    sys.modules[name] = saved_module

    def test_signature_is_compatible_with_the_orchestrator_binder(self):
        # memebuy.orchestrator._bind_swap_kwargs refuses a callable that does not
        # name these as real parameters, and passes the rest by keyword.
        params = inspect.signature(bs.buy_token).parameters
        named = {n for n, p in params.items()
                 if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                               inspect.Parameter.KEYWORD_ONLY)}
        for required in ("dry_run", "usdt_amount", "slippage_bps"):
            self.assertIn(required, named)
        for optional in ("token_address", "recipient", "token_mint", "mint", "taker",
                         "rpc_url", "min_native_balance", "chain", "client", "key"):
            self.assertIn(optional, named)
        # No **kwargs tail: an unknown keyword must be a TypeError, not a silent drop.
        self.assertFalse(any(p.kind is inspect.Parameter.VAR_KEYWORD
                             for p in params.values()))

    def test_wire_names_are_all_alias_tuples_so_one_edit_fixes_the_shape(self):
        for key, aliases in bs.WIRE.items():
            self.assertIsInstance(aliases, tuple, key)
            self.assertTrue(aliases, key)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
