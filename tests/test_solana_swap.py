"""Unit tests for chains/solana_client.py + chains/solana_swap.py.

ZERO network, ZERO credentials.  Everything below the module under test is a
stand-in injected into sys.modules, so this file runs standalone even when
`requests`, `solders`, `chains.errors` and `chains.wallet_keys` are absent.
HTTP is mocked at the `requests.Session.request` boundary (see tests/test_autobuy.py).
"""

import base64
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


# ---------- stand-ins (installed before the modules under test are imported) ----------

def _install_chains_package():
    pkg = types.ModuleType("chains")
    pkg.__path__ = [os.path.join(ROOT, "chains")]  # load real submodules, skip __init__
    sys.modules["chains"] = pkg

    errors = types.ModuleType("chains.errors")

    class ExchangeAPIError(Exception):
        exchange = "chain"

        def __init__(self, code, message, http_status=None):
            super().__init__(f"{self.exchange} error {code}: {message}")
            self.code, self.message, self.http_status = code, message, http_status

    class ChainError(ExchangeAPIError):
        exchange = "chain"

    class SwapReverted(ChainError):
        pass

    class InsufficientGas(ChainError):
        pass

    class QuoteStale(ChainError):
        pass

    def chain_error_family(chain):
        family = {b.__name__: type(f"{chain.capitalize()}{b.__name__}", (b,), {"exchange": chain})
                  for b in (ChainError, SwapReverted, InsufficientGas, QuoteStale)}
        return types.SimpleNamespace(chain=chain, **family)

    errors.ChainError, errors.SwapReverted = ChainError, SwapReverted
    errors.InsufficientGas, errors.QuoteStale = InsufficientGas, QuoteStale
    errors.chain_error_family = chain_error_family
    sys.modules["chains.errors"] = errors

    keys = types.ModuleType("chains.wallet_keys")

    class SecretKey:
        """Mirrors chains.wallet_keys.SecretKey: str-only, reveal(), redacted."""

        def __init__(self, value, label="key"):
            if not isinstance(value, str) or not value:
                raise ChainError("KEY_EMPTY", "key material is empty")
            self._value, self.label = value, label

        def reveal(self):
            return self._value

        def __repr__(self):
            return "<redacted>"

        __str__ = __repr__
        __format__ = lambda self, spec: "<redacted>"  # noqa: E731

    def load_secret_key(env_var, fmt="evm", label=None):
        """Mini version of the real loader: the env var holds a PATH."""
        path = os.environ.get(env_var, "").strip()
        if not path:
            raise ChainError("KEY_ENV_MISSING", f"{env_var} is not set; it must hold "
                                                "the PATH to the key file")
        with open(path, "r", encoding="utf-8") as handle:
            return SecretKey(handle.read().strip(), label=label or env_var)

    keys.SecretKey, keys.load_secret_key = SecretKey, load_secret_key
    sys.modules["chains.wallet_keys"] = keys
    return ChainError, SwapReverted, QuoteStale, SecretKey


def _install_requests():
    try:
        import requests  # noqa: F401  (real package present -> patch it as usual)
        return
    except ImportError:
        pass
    requests = types.ModuleType("requests")

    class Session:
        def request(self, method, url, **kwargs):  # always patched in tests
            raise AssertionError("real HTTP attempted")

    requests.Session = Session
    requests.RequestException = type("RequestException", (Exception,), {})
    sys.modules["requests"] = requests


class FakeKeypair:
    made = 0

    def __init__(self):
        FakeKeypair.made += 1

    @classmethod
    def from_bytes(cls, raw):
        return cls()

    @classmethod
    def from_base58_string(cls, text):
        return cls()

    def pubkey(self):
        return "TakerWa11etPubkey111111111111111111111111111"


class FakeVersionedTransaction:
    signed = 0

    def __init__(self, message, signers=None):
        self.message, self.signers = message, signers
        if signers is not None:
            FakeVersionedTransaction.signed += 1

    @classmethod
    def from_bytes(cls, raw):
        return cls(("message-of", raw), None)

    def __bytes__(self):
        return b"signed-transaction-bytes"


def _install_solders():
    solders = types.ModuleType("solders")
    solders.__path__ = []
    kp_mod = types.ModuleType("solders.keypair")
    kp_mod.Keypair = FakeKeypair
    tx_mod = types.ModuleType("solders.transaction")
    tx_mod.VersionedTransaction = FakeVersionedTransaction
    sys.modules.update({"solders": solders, "solders.keypair": kp_mod,
                        "solders.transaction": tx_mod})


ChainError, SwapReverted, QuoteStale, SecretKey = _install_chains_package()
_install_requests()
_install_solders()

import requests  # noqa: E402

from chains import solana_swap  # noqa: E402
from chains.solana_client import SolanaClient  # noqa: E402
from chains.solana_swap import (USDT_MINT, buy_token, taker_address,  # noqa: E402
                                to_atomic, validate_mint)

TOKEN_MINT = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"       # JUP
TAKER = "TakerWa11etPubkey111111111111111111111111111"
HOMOGLYPH_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYВ"  # Cyrillic B
# Not a real key: 64 base58 chars of filler, never loaded by anything.
FAKE_KEY_B58 = "4" * 88


# ---------- HTTP fakes ----------

class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload, self.status_code = payload, status_code
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        if isinstance(self.payload, str):
            raise ValueError("not json")
        return self.payload


def order_payload(**overrides):
    # Field shape taken from a live GET https://api.jup.ag/swap/v2/order:
    # inAmount echoes the request exactly, otherAmountThreshold is the enforced
    # minimum received, priceImpactPct is a FRACTION (0.0012 == 0.12%).
    payload = {
        "inputMint": USDT_MINT,
        "outputMint": TOKEN_MINT,
        "inAmount": "12340000",
        "outAmount": "9876543",
        "otherAmountThreshold": "9777777",   # 9876543 * 9900 // 10000
        "swapMode": "ExactIn",
        "priceImpactPct": "0.0012",
        "slippageBps": 100,
        "simulationError": None,
        "transaction": base64.b64encode(b"unsigned-tx").decode(),
        "requestId": "req-1",
        "routePlan": [{"percent": 100, "swapInfo": {"label": "Meteora DLMM",
                                                    "inputMint": USDT_MINT,
                                                    "outputMint": TOKEN_MINT}}],
    }
    payload.update(overrides)
    return payload


EXECUTE_OK = {"status": "Success", "signature": "5SigNatureBase58xxxxxxxxxxxxxxxxxxxxxxxxxxx",
              "slot": 987654, "inputAmountResult": "12340000", "outputAmountResult": "9876000"}
EXECUTE_STALE = {"status": "Failed", "code": "BlockhashNotFound",
                 "error": "Blockhash not found"}


class Router:
    """Stands in for requests.Session.request and records every call."""

    def __init__(self, orders=None, executes=None, balance_lamports=60_000_000,
                 mint_decimals=6):
        self.calls = []
        self.orders = list(orders or [FakeResponse(order_payload())])
        self.executes = list(executes or [FakeResponse(EXECUTE_OK)])
        self.balance_lamports = balance_lamports
        self.mint_decimals = mint_decimals

    # counters -------------------------------------------------------------
    def _of(self, kind):
        return [c for c in self.calls if c["kind"] == kind]

    @property
    def order_calls(self):
        return self._of("order")

    @property
    def execute_calls(self):
        return self._of("execute")

    def _pop(self, queue):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def __call__(self, method, url, params=None, json=None, headers=None, timeout=None):
        if "/swap/v2/order" in url:
            self.calls.append({"kind": "order", "params": params, "headers": headers})
            return self._pop(self.orders)
        if "/swap/v2/execute" in url:
            self.calls.append({"kind": "execute", "body": json})
            return self._pop(self.executes)
        rpc = (json or {}).get("method")
        self.calls.append({"kind": "rpc", "rpc": rpc, "params": (json or {}).get("params")})
        if rpc == "getBalance":
            return FakeResponse({"jsonrpc": "2.0", "id": 1,
                                 "result": {"value": self.balance_lamports}})
        if rpc == "getAccountInfo":
            return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": {"value": {
                "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                "data": {"parsed": {"type": "mint", "info": {
                    "decimals": self.mint_decimals, "mintAuthority": None,
                    "freezeAuthority": None, "supply": "1000000", "isInitialized": True}}}}}})
        if rpc == "getTokenAccountsByOwner":
            return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": {"value": [
                {"account": {"data": {"parsed": {"info": {"tokenAmount": {
                    "amount": "4200", "decimals": 6}}}}}}]}})
        raise AssertionError(f"unexpected rpc {rpc}")


class SolanaTestCase(unittest.TestCase):
    def setUp(self):
        FakeVersionedTransaction.signed = 0
        FakeKeypair.made = 0
        self.key = SecretKey(FAKE_KEY_B58)

    def run_buy(self, router=None, **kwargs):
        router = router or Router()
        patcher = mock.patch.object(requests.Session, "request", side_effect=router)
        patcher.start()
        self.addCleanup(patcher.stop)
        client = SolanaClient(base_url="https://api.jup.ag", rpc_url="https://rpc.test")
        params = {"mint": TOKEN_MINT, "usdt_amount": "12.34", "taker": TAKER}
        params.update(kwargs)
        return router, client, params


# ---------- mint identity ----------

class TestMintValidation(SolanaTestCase):
    def test_rejects_garbage(self):
        for bad in ("not-a-mint!!!", "0OIl" * 10, "", None, 12345, "So11111"):
            with self.assertRaises(ChainError):
                validate_mint(bad)

    def test_rejects_symbol_like_input(self):
        # Identity is the mint address only - symbols are attacker-controlled.
        for symbol in ("JUP", "USDT", "BONK", "Tether USD"):
            with self.assertRaises(ChainError) as ctx:
                validate_mint(symbol)
            self.assertEqual(ctx.exception.code, "BAD_MINT")

    def test_rejects_homoglyph_lookalike(self):
        with self.assertRaises(ChainError):
            validate_mint(HOMOGLYPH_MINT)

    def test_rejects_usdt_as_output(self):
        with self.assertRaises(ChainError):
            validate_mint(USDT_MINT)

    def test_accepts_real_mint(self):
        self.assertEqual(validate_mint(TOKEN_MINT), TOKEN_MINT)

    def test_buy_rejects_bad_mint_before_any_http(self):
        router, client, params = self.run_buy()
        params["mint"] = "JUP"
        with self.assertRaises(ChainError):
            buy_token(client, self.key, **params)
        self.assertEqual(router.calls, [])


# ---------- atomic units ----------

class TestAtomicConversion(SolanaTestCase):
    def test_six_decimals(self):
        self.assertEqual(to_atomic("12.34", 6), 12_340_000)
        self.assertEqual(to_atomic(1, 6), 1_000_000)
        self.assertEqual(to_atomic("0.000001", 6), 1)

    def test_truncates_never_rounds_up(self):
        self.assertEqual(to_atomic("12.3456789", 6), 12_345_678)

    def test_rejects_non_positive_and_dust(self):
        for bad in ("0", "-5", "0.0000001", "abc"):
            with self.assertRaises(ChainError):
                to_atomic(bad, 6)

    def test_buy_sends_atomic_amount_from_mint_decimals(self):
        router = Router(orders=[FakeResponse(order_payload(inAmount="12345678"))])
        _, client, params = self.run_buy(router)
        params["usdt_amount"] = "12.3456789"
        buy_token(client, self.key, dry_run=True, **params)
        self.assertEqual(router.order_calls[0]["params"]["amount"], "12345678")


# ---------- dry run ----------

class TestDryRun(SolanaTestCase):
    def test_dry_run_never_executes_and_never_signs(self):
        router, client, params = self.run_buy()
        result = buy_token(client, self.key, dry_run=True, **params)
        self.assertTrue(result["dry_run"])
        self.assertIsNone(result["signature"])
        self.assertEqual(router.execute_calls, [])
        self.assertEqual(FakeVersionedTransaction.signed, 0)
        self.assertEqual(result["out_amount"], 9876543)
        self.assertEqual(result["route"][0]["amm"], "Meteora DLMM")

    def test_dry_run_is_the_default(self):
        router, client, params = self.run_buy()
        result = buy_token(client, self.key, **params)
        self.assertTrue(result["dry_run"])
        self.assertEqual(router.execute_calls, [])

    def test_slippage_and_route_restriction_flow_into_the_request(self):
        router, client, params = self.run_buy()
        buy_token(client, self.key, slippage_bps=250, dry_run=True, **params)
        sent = router.order_calls[0]["params"]
        self.assertEqual(sent["slippageBps"], "250")
        self.assertEqual(sent["restrictIntermediateTokens"], "true")
        self.assertEqual(sent["dynamicComputeUnitLimit"], "true")
        self.assertEqual(sent["inputMint"], USDT_MINT)
        self.assertEqual(sent["outputMint"], TOKEN_MINT)
        self.assertEqual(sent["taker"], TAKER)


# ---------- simulation gate ----------

class TestSimulationGate(SolanaTestCase):
    def test_simulation_error_aborts_before_signing(self):
        router = Router(orders=[FakeResponse(order_payload(
            simulationError={"errorCode": "SlippageToleranceExceeded",
                             "error": "custom program error: 0x1771"}))])
        _, client, params = self.run_buy(router)
        with self.assertRaises(SwapReverted):
            buy_token(client, self.key, dry_run=False, **params)
        self.assertEqual(FakeVersionedTransaction.signed, 0)
        self.assertEqual(router.execute_calls, [])

    def test_missing_simulation_error_field_fails_closed(self):
        payload = order_payload()
        payload.pop("simulationError")
        router = Router(orders=[FakeResponse(payload)])
        _, client, params = self.run_buy(router)
        with self.assertRaises(ChainError) as ctx:
            buy_token(client, self.key, dry_run=True, **params)
        self.assertEqual(ctx.exception.code, "NO_SIMULATION")

    def test_missing_price_impact_fails_closed(self):
        payload = order_payload()
        payload.pop("priceImpactPct")
        router = Router(orders=[FakeResponse(payload)])
        _, client, params = self.run_buy(router)
        with self.assertRaises(ChainError) as ctx:
            buy_token(client, self.key, dry_run=True, **params)
        self.assertEqual(ctx.exception.code, "NO_PRICE_IMPACT")

    def test_mint_mismatch_in_response_aborts(self):
        router = Router(orders=[FakeResponse(order_payload(
            outputMint="So11111111111111111111111111111111111111112"))])
        _, client, params = self.run_buy(router)
        with self.assertRaises(ChainError) as ctx:
            buy_token(client, self.key, dry_run=True, **params)
        self.assertEqual(ctx.exception.code, "MINT_MISMATCH")


class TestOrderEchoGate(SolanaTestCase):
    """The order response must describe the swap we asked for, not just a swap."""

    def _reject(self, code, **overrides):
        router = Router(orders=[FakeResponse(order_payload(**overrides))])
        _, client, params = self.run_buy(router)
        with self.assertRaises(ChainError) as ctx:
            buy_token(client, self.key, dry_run=False, **params)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(FakeVersionedTransaction.signed, 0)
        self.assertEqual(router.execute_calls, [])
        return ctx.exception

    def test_inflated_in_amount_is_never_signed(self):
        # We asked to spend 12.34 USDT; the order would spend 1234.
        self._reject("AMOUNT_MISMATCH", inAmount="1234000000")

    def test_missing_in_amount_fails_closed(self):
        payload = order_payload()
        payload.pop("inAmount")
        router = Router(orders=[FakeResponse(payload)])
        _, client, params = self.run_buy(router)
        with self.assertRaises(ChainError) as ctx:
            buy_token(client, self.key, dry_run=False, **params)
        self.assertEqual(ctx.exception.code, "MISSING_FIELD")

    def test_widened_slippage_echo_is_rejected(self):
        # We asked for 100bps; the order enforces 3000bps -> 30% worse fill.
        self._reject("SLIPPAGE_MISMATCH", slippageBps=3000,
                     otherAmountThreshold="6913580")

    def test_tighter_slippage_echo_is_accepted(self):
        router = Router(orders=[FakeResponse(order_payload(
            slippageBps=50, otherAmountThreshold="9827160"))])  # 9876543*9950//10000
        _, client, params = self.run_buy(router)
        self.assertTrue(buy_token(client, self.key, dry_run=True, **params)["dry_run"])

    def test_min_received_below_the_quote_floor_is_rejected(self):
        # otherAmountThreshold is what the tx enforces; 1 unit protects nothing.
        self._reject("MIN_RECEIVED", otherAmountThreshold="1")

    def test_missing_min_received_fails_closed(self):
        payload = order_payload()
        payload.pop("otherAmountThreshold")
        router = Router(orders=[FakeResponse(payload)])
        _, client, params = self.run_buy(router)
        with self.assertRaises(ChainError) as ctx:
            buy_token(client, self.key, dry_run=False, **params)
        self.assertEqual(ctx.exception.code, "MISSING_FIELD")


# ---------- SOL floor ----------

class TestSolFloor(SolanaTestCase):
    def test_aborts_below_floor_before_quoting(self):
        router = Router(balance_lamports=10_000_000)  # 0.01 SOL - not enough
        _, client, params = self.run_buy(router)
        with self.assertRaises(ChainError) as ctx:
            buy_token(client, self.key, dry_run=True, **params)
        self.assertEqual(ctx.exception.code, "SOL_FLOOR")
        self.assertEqual(router.order_calls, [])

    def test_floor_is_configurable(self):
        router = Router(balance_lamports=35_000_000)
        _, client, params = self.run_buy(router)
        with self.assertRaises(ChainError):
            buy_token(client, self.key, dry_run=True, **params)
        result = buy_token(client, self.key, dry_run=True,
                           sol_floor_lamports=30_000_000, **params)
        self.assertTrue(result["dry_run"])

    def test_missing_balance_field_fails_closed(self):
        router = Router()
        router.balance_lamports = None
        _, client, params = self.run_buy(router)
        with self.assertRaises(ChainError) as ctx:
            buy_token(client, self.key, dry_run=True, **params)
        self.assertEqual(ctx.exception.code, "NO_BALANCE")


# ---------- live path + stale blockhash ----------

class TestLiveExecution(SolanaTestCase):
    def test_live_signs_once_and_returns_settled_amounts(self):
        router, client, params = self.run_buy()
        result = buy_token(client, self.key, dry_run=False, **params)
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["signature"], EXECUTE_OK["signature"])
        self.assertEqual(result["out_amount"], 9876000)
        self.assertEqual(result["in_amount"], 12340000)
        self.assertEqual(FakeVersionedTransaction.signed, 1)
        body = router.execute_calls[0]["body"]
        self.assertEqual(body["requestId"], "req-1")
        self.assertEqual(body["signedTransaction"],
                         base64.b64encode(b"signed-transaction-bytes").decode())

    def test_stale_blockhash_requotes_then_succeeds(self):
        router = Router(orders=[FakeResponse(order_payload()), FakeResponse(order_payload())],
                        executes=[FakeResponse(EXECUTE_STALE), FakeResponse(EXECUTE_OK)])
        _, client, params = self.run_buy(router)
        result = buy_token(client, self.key, dry_run=False, **params)
        self.assertEqual(result["signature"], EXECUTE_OK["signature"])
        self.assertEqual(len(router.order_calls), 2)   # re-quoted, not resubmitted
        self.assertEqual(FakeVersionedTransaction.signed, 2)

    def test_stale_blockhash_retries_are_bounded(self):
        router = Router(executes=[FakeResponse(EXECUTE_STALE)])
        _, client, params = self.run_buy(router)
        with self.assertRaises(ChainError) as ctx:
            buy_token(client, self.key, dry_run=False, max_requotes=2, **params)
        self.assertEqual(ctx.exception.code, "STALE_GIVEUP")
        self.assertEqual(len(router.order_calls), 3)   # initial + 2 re-quotes, no loop
        self.assertEqual(len(router.execute_calls), 3)

    def test_slippage_revert_is_not_retried(self):
        router = Router(executes=[FakeResponse(
            {"status": "Failed", "code": 6001, "error": "custom program error: 0x1771"})])
        _, client, params = self.run_buy(router)
        with self.assertRaises(SwapReverted) as ctx:
            buy_token(client, self.key, dry_run=False, **params)
        self.assertEqual(ctx.exception.code, 6001)
        self.assertIn("SlippageToleranceExceeded", ctx.exception.message)
        self.assertEqual(len(router.order_calls), 1)

    def test_unknown_outcome_is_never_requoted(self):
        """The one failure that must NOT retry: solana's confirmation timeout
        says the transaction may still land, so a second signed spend is a
        double spend.  Its text also matches the stale markers, hence this."""
        unknown = {"status": "Failed", "code": "TransactionExpiredTimeoutError",
                   "error": "Transaction was not confirmed in 30.00 seconds. It is "
                            "unknown if it succeeded or failed."}
        router = Router(executes=[FakeResponse(unknown)])
        _, client, params = self.run_buy(router)
        with self.assertRaises(ChainError) as ctx:
            buy_token(client, self.key, dry_run=False, **params)
        self.assertEqual(ctx.exception.code, "OUTCOME_UNKNOWN")
        self.assertNotIsInstance(ctx.exception, QuoteStale)
        self.assertEqual(len(router.execute_calls), 1)   # exactly ONE spend
        self.assertEqual(len(router.order_calls), 1)

    def test_taker_derived_from_key_when_not_supplied(self):
        router, client, params = self.run_buy()
        params.pop("taker")
        result = buy_token(client, self.key, dry_run=True, **params)
        self.assertEqual(result["taker"], FakeKeypair().pubkey())


# ---------- client-level behaviour ----------

class TestClient(SolanaTestCase):
    def _client(self, router, **kwargs):
        patcher = mock.patch.object(requests.Session, "request", side_effect=router)
        patcher.start()
        self.addCleanup(patcher.stop)
        return SolanaClient(rpc_url="https://rpc.test", **kwargs)

    def test_rate_limit_is_an_error_not_an_empty_quote(self):
        router = Router(orders=[FakeResponse({"error": "rate limit"}, status_code=429)])
        client = self._client(router)
        with self.assertRaises(ChainError) as ctx:
            client.get_order(USDT_MINT, TOKEN_MINT, 1_000_000, 100, TAKER)
        self.assertEqual(ctx.exception.code, "RATE_LIMITED")

    def test_no_routes_found_is_typed(self):
        router = Router(orders=[FakeResponse(
            {"errorCode": "NO_ROUTES_FOUND", "error": "no routes"}, status_code=400)])
        client = self._client(router)
        with self.assertRaises(ChainError) as ctx:
            client.get_order(USDT_MINT, TOKEN_MINT, 1_000_000, 100, TAKER)
        self.assertIn("no route", ctx.exception.message)

    def test_execute_without_status_fails_closed(self):
        router = Router(executes=[FakeResponse({"signature": "abc"})])
        client = self._client(router)
        with self.assertRaises(ChainError) as ctx:
            client.execute_order("c2lnbmVk", "req-1")
        self.assertEqual(ctx.exception.code, "NO_STATUS")

    def test_api_key_header_only_when_configured(self):
        router = Router()
        client = self._client(router, api_key="jup-key")
        client.get_order(USDT_MINT, TOKEN_MINT, 1_000_000, 100, TAKER)
        self.assertEqual(router.order_calls[0]["headers"]["x-api-key"], "jup-key")

        router2 = Router()
        client2 = self._client(router2)
        client2.get_order(USDT_MINT, TOKEN_MINT, 1_000_000, 100, TAKER)
        self.assertNotIn("x-api-key", router2.order_calls[0]["headers"])

    def test_get_mint_info_parses_authorities_and_program(self):
        router = Router()
        info = self._client(router).get_mint_info(USDT_MINT)
        self.assertEqual(info["decimals"], 6)
        self.assertIsNone(info["mint_authority"])
        self.assertIsNone(info["freeze_authority"])
        self.assertTrue(info["program"].startswith("Tokenkeg"))

    def test_get_mint_info_rejects_missing_account(self):
        client = self._client(Router())
        with mock.patch.object(SolanaClient, "_rpc", return_value={"value": None}):
            with self.assertRaises(ChainError) as ctx:
                client.get_mint_info(TOKEN_MINT)
        self.assertEqual(ctx.exception.code, "MINT_NOT_FOUND")

    def test_get_token_balance_sums_accounts(self):
        router = Router()
        bal = self._client(router).get_token_balance(TAKER, TOKEN_MINT)
        self.assertEqual(bal["amount"], 4200)
        self.assertEqual(bal["decimals"], 6)

    def test_secret_key_never_appears_in_a_repr(self):
        self.assertEqual(repr(self.key), "<redacted>")
        self.assertNotIn(FAKE_KEY_B58, f"{self.key} {self.key!r} {self.key!s}")


class TestKeyHandling(SolanaTestCase):
    def test_raw_material_is_rejected(self):
        with self.assertRaises(ChainError) as ctx:
            taker_address(FAKE_KEY_B58)  # a bare string, not a SecretKey
        self.assertEqual(ctx.exception.code, "KEY_TYPE")

    def test_json_byte_array_format_is_accepted(self):
        self.assertEqual(taker_address(SecretKey(json.dumps(list(range(64))))),
                         FakeKeypair().pubkey())

    def test_unparsable_key_error_never_echoes_the_material(self):
        with mock.patch.object(FakeKeypair, "from_base58_string",
                               side_effect=ValueError(f"bad key {FAKE_KEY_B58}")):
            with self.assertRaises(ChainError) as ctx:
                taker_address(self.key)
        self.assertEqual(ctx.exception.code, "BAD_KEY")
        self.assertNotIn(FAKE_KEY_B58, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)  # original swallowed on purpose

    def test_key_is_absent_from_traceback_frame_locals(self):
        """A swallowed cause is not enough: the raising frame still lands in the
        traceback, and Sentry/rich/cgitb render frame locals."""
        import traceback

        with mock.patch.object(FakeKeypair, "from_base58_string",
                               side_effect=ValueError("nope")):
            try:
                taker_address(self.key)
            except ChainError as exc:
                frames = traceback.TracebackException.from_exception(
                    exc, capture_locals=True)
                rendered = "".join(frames.format())
        self.assertIn("BAD_KEY", rendered)
        self.assertNotIn(FAKE_KEY_B58, rendered)

    def test_signing_failure_leaks_neither_keypair_nor_cause(self):
        with mock.patch.object(FakeVersionedTransaction, "from_bytes",
                               side_effect=ValueError(f"boom {FAKE_KEY_B58}")):
            with self.assertRaises(ChainError) as ctx:
                solana_swap._sign(base64.b64encode(b"tx").decode(), FakeKeypair())
        self.assertEqual(ctx.exception.code, "SIGN_FAILED")
        self.assertNotIn(FAKE_KEY_B58, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)


class TestOrchestratorCallShape(SolanaTestCase):
    """memebuy.orchestrator holds no client and no key material; it calls with
    token_mint=/rpc_url=/min_native_balance= and expects a 'signature' back."""

    def _key_file(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "solana.key")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(FAKE_KEY_B58 + "\n")
        return path

    def test_orchestrator_kwargs_are_accepted(self):
        router, _, _ = self.run_buy()
        with mock.patch.dict(os.environ, {"SOLANA_PRIVATE_KEY_FILE": self._key_file()}):
            result = buy_token(usdt_amount=12.34, slippage_bps=150,
                               rpc_url="https://rpc.test", min_native_balance=0.05,
                               token_mint=TOKEN_MINT, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["taker"], FakeKeypair().pubkey())
        self.assertEqual(router.order_calls[0]["params"]["slippageBps"], "150")
        self.assertEqual(router.order_calls[0]["params"]["amount"], "12340000")

    def test_configured_floor_is_clamped_up_never_down(self):
        # 0.001 SOL cannot even pay the 0.00203928 ATA rent - clamp, do not obey.
        router = Router(balance_lamports=1_500_000)
        _, client, params = self.run_buy(router)
        with self.assertRaises(ChainError) as ctx:
            buy_token(client, self.key, min_native_balance=0.001, dry_run=True, **params)
        self.assertEqual(ctx.exception.code, "SOL_FLOOR")
        self.assertEqual(router.order_calls, [])

    def test_zero_min_native_balance_does_not_lower_the_floor(self):
        """memebuy.config defaults min_native_balance to 0.0.  Honouring that
        would drop the floor from 0.04 SOL to the ~0.002 sanity clamp."""
        router = Router(balance_lamports=10_000_000)  # 0.01 SOL
        _, client, params = self.run_buy(router)
        for falsy in (0, 0.0, "0"):
            with self.assertRaises(ChainError) as ctx:
                buy_token(client, self.key, min_native_balance=falsy,
                          dry_run=True, **params)
            self.assertEqual(ctx.exception.code, "SOL_FLOOR")
        self.assertEqual(router.order_calls, [])

    def test_non_bool_dry_run_is_refused_not_treated_as_live(self):
        router, client, params = self.run_buy()
        for sneaky in (None, 0, "", "false"):
            with self.assertRaises(ChainError) as ctx:
                buy_token(client, self.key, dry_run=sneaky, **params)
            self.assertEqual(ctx.exception.code, "BAD_DRY_RUN")
        self.assertEqual(router.execute_calls, [])
        self.assertEqual(FakeVersionedTransaction.signed, 0)

    def test_dry_run_with_explicit_taker_never_opens_the_key_file(self):
        _, client, params = self.run_buy()
        with mock.patch.dict(os.environ, {"SOLANA_PRIVATE_KEY_FILE": ""}):
            result = buy_token(client, None, dry_run=True, **params)  # no key at all
        self.assertTrue(result["dry_run"])

    def test_missing_key_env_fails_closed(self):
        _, client, params = self.run_buy()
        params.pop("taker")
        with mock.patch.dict(os.environ, {"SOLANA_PRIVATE_KEY_FILE": ""}):
            with self.assertRaises(ChainError) as ctx:
                buy_token(client, None, dry_run=True, **params)
        self.assertEqual(ctx.exception.code, "KEY_ENV_MISSING")


class TestModuleConstants(unittest.TestCase):
    def test_usdt_mint_and_floor(self):
        self.assertEqual(USDT_MINT, "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB")
        self.assertEqual(solana_swap.ATA_RENT_LAMPORTS, 2_039_280)
        self.assertGreaterEqual(solana_swap.DEFAULT_SOL_FLOOR_LAMPORTS, 30_000_000)

    def test_error_family_is_labelled_solana(self):
        from chains.solana_client import SOL
        self.assertEqual(str(SOL.ChainError("X", "boom")), "solana error X: boom")
        self.assertTrue(issubclass(SOL.QuoteStale, QuoteStale))

    def test_real_shared_modules_match_the_assumed_contract(self):
        """Drift guard: load the sibling-owned modules off disk (bypassing the
        stand-ins) and assert the API this package is written against."""
        import importlib.util

        for name, expected in (("errors", ("ChainError", "SwapReverted", "QuoteStale",
                                           "InsufficientGas", "chain_error_family")),
                               ("wallet_keys", ("SecretKey", "load_secret_key"))):
            path = os.path.join(ROOT, "chains", f"{name}.py")
            if not os.path.exists(path):  # sibling not finished yet
                continue
            spec = importlib.util.spec_from_file_location(f"chains.{name}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for attr in expected:
                self.assertTrue(hasattr(module, attr), f"chains.{name}.{attr} is missing")
            if name == "errors":
                err = module.ChainError("CODE", "msg")
                self.assertEqual((err.code, err.message, err.http_status), ("CODE", "msg", None))
            else:
                secret = module.SecretKey("abc")
                self.assertEqual(repr(secret), "<redacted>")
                self.assertIsInstance(secret.reveal(), str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
