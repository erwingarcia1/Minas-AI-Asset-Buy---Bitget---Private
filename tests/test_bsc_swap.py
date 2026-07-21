"""Unit tests for the BSC / PancakeSwap V2 buy path.

ZERO network, ZERO real credentials: web3 is faked at the RPC boundary the
same way tests/test_autobuy.py fakes ``requests.Session.request``. The fake
chain below answers ``eth_call``-shaped reads and records every state-changing
call, so "did we send anything?" is directly assertable.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chains import bsc_client, bsc_swap  # noqa: E402
# Bound at import time on purpose: a sibling test module installs stand-ins
# over sys.modules["chains.errors"], so a deferred (in-method) import of these
# names would pick up that stub when the whole suite runs in one process.
from chains.errors import (  # noqa: E402
    ChainError, InsufficientGas, QuoteStale, SwapReverted, chain_error_family,
)
from chains.wallet_keys import SecretKey, load_secret_key  # noqa: E402

USDT = bsc_swap.USDT
WBNB = bsc_swap.WBNB
ROUTER = bsc_swap.PANCAKE_V2_ROUTER
TOKEN = "0x1111111111111111111111111111111111111111"
SENDER = "0x2222222222222222222222222222222222222222"
RECIPIENT = SENDER
PAIR = "0x3333333333333333333333333333333333333333"
ZERO = "0x0000000000000000000000000000000000000000"

ONE = 10 ** 18
TEST_KEY_HEX = "0x" + "ab" * 32  # not a funded key; never leaves this file


class FakeRevert(Exception):
    """Stands in for web3's ContractLogicError."""


# ---------- fake chain ----------

class FakeChain:
    """Mutable state + call log the fake contracts read from."""

    def __init__(self):
        self.bnb = 10 ** 17                 # 0.1 BNB, comfortably above the floor
        self.usdt_balance = 10_000 * ONE
        self.usdt_decimals = 18             # BSC USDT: 18, NOT 6
        self.allowance = 0
        self.pair = ZERO                    # no direct USDT/TOKEN pair by default
        self.quotes = {(USDT, WBNB, TOKEN): 1000 * ONE}
        self.swap_revert = None             # set to a string to make estimate_gas fail
        self.receipt_status = 1
        # observations
        self.sent = []
        self.signed_with = []
        self.approve_args = None
        self.swap_args = None
        self.quoted_paths = []
        self.reads = []


class FakeFunction:
    def __init__(self, chain, address, name, args):
        self.chain, self.address, self.name, self.args = chain, address, name, args

    def call(self, tx=None):
        return _read(self.chain, self.address, self.name, self.args)

    def estimate_gas(self, tx=None):
        if self.name == "approve":
            return 46_000
        if self.name.startswith("swapExactTokensForTokens"):
            if self.chain.swap_revert:
                raise FakeRevert(self.chain.swap_revert)
            return 210_000
        raise AssertionError(f"estimate_gas on unexpected fn {self.name}")

    def build_transaction(self, params):
        tx = dict(params)
        tx.update({"to": self.address, "data": "0x" + self.name[:8].encode().hex()})
        return tx


class FakeFunctions:
    def __init__(self, chain, address):
        self.chain, self.address = chain, address

    def __getattr__(self, name):
        def bind(*args):
            if name == "approve":
                self.chain.approve_args = args
            elif name.startswith("swapExactTokensForTokens"):
                assert name == "swapExactTokensForTokensSupportingFeeOnTransferTokens", \
                    f"must never call the non-fee-on-transfer variant, got {name}"
                self.chain.swap_args = args
            return FakeFunction(self.chain, self.address, name, args)
        return bind


class FakeContract:
    def __init__(self, chain, address, abi):
        self.address, self.abi = address, abi
        self.functions = FakeFunctions(chain, address)


def _read(chain, address, name, args):
    chain.reads.append((address, name, args))
    if name == "decimals":
        return chain.usdt_decimals if address == USDT else 9
    if name == "balanceOf":
        return chain.usdt_balance
    if name == "allowance":
        return chain.allowance
    if name == "symbol":
        return "USDT" if address == USDT else "TKN"
    if name == "getPair":
        return chain.pair
    if name == "getAmountsOut":
        amount_in, path = args
        chain.quoted_paths.append(tuple(path))
        out = chain.quotes.get(tuple(path))
        if out is None:
            raise FakeRevert("execution reverted: PancakeLibrary: INSUFFICIENT_LIQUIDITY")
        return [amount_in] + [0] * (len(path) - 2) + [out]
    raise AssertionError(f"unexpected read {name}")


class FakeSigned:
    def __init__(self, raw):
        self.raw_transaction = raw  # web3 v7 name; v6 .rawTransaction must NOT be used


class FakeAccount:
    def __init__(self, chain):
        self.chain = chain

    def sign_transaction(self, tx, private_key):
        self.chain.signed_with.append(private_key)
        return FakeSigned(b"\xde\xad\xbe\xef")


class FakeEth:
    def __init__(self, chain):
        self.chain = chain
        self.account = FakeAccount(chain)
        self.gas_price = 1_000_000_000  # 1 gwei

    def contract(self, address=None, abi=None):
        return FakeContract(self.chain, address, abi)

    def get_balance(self, address):
        return self.chain.bnb

    def get_transaction_count(self, address, block=None):
        return 7

    def send_raw_transaction(self, raw):
        self.chain.sent.append(raw)
        return b"\x01" * 32

    def wait_for_transaction_receipt(self, tx_hash, timeout=None):
        return {"status": self.chain.receipt_status, "gasUsed": 198_765}


class FakeWeb3:
    def __init__(self, chain):
        self.chain = chain
        self.eth = FakeEth(chain)

    @staticmethod
    def to_checksum_address(address):
        return address  # constants in this file are already checksummed

    @staticmethod
    def to_hex(value):
        return "0x" + bytes(value).hex()


# ---------- swap tests ----------

class BuyTokenTest(unittest.TestCase):

    def setUp(self):
        bsc_client._DECIMALS_CACHE.clear()  # module-level cache would bleed
        self.chain = FakeChain()
        self.w3 = FakeWeb3(self.chain)
        self.key = SecretKey(TEST_KEY_HEX)
        patcher = mock.patch.object(bsc_swap, "address_from_key", return_value=SENDER)
        self.addCleanup(patcher.stop)
        patcher.start()

    def buy(self, **kw):
        params = dict(token_address=TOKEN, usdt_amount="25", slippage_bps=300,
                      recipient=RECIPIENT, dry_run=True)
        params.update(kw)
        return bsc_swap.buy_token(self.w3, self.key, **params)

    # -- decimals --

    def test_usdt_amount_uses_18_decimals_not_6(self):
        result = self.buy(usdt_amount="25")
        self.assertEqual(result["amount_in"], 25 * ONE)
        self.assertNotEqual(result["amount_in"], 25 * 10 ** 6)

    def test_decimals_are_read_at_runtime_not_assumed(self):
        # A hypothetical 6-decimal stablecoin must scale by 1e6, proving we
        # never hardcode either constant.
        self.chain.usdt_decimals = 6
        self.assertEqual(self.buy(usdt_amount="25")["amount_in"], 25 * 10 ** 6)

    def test_fractional_amount_truncates(self):
        self.assertEqual(self.buy(usdt_amount="0.1")["amount_in"], 10 ** 17)

    # -- slippage math --

    def test_amount_out_min_from_slippage_bps(self):
        result = self.buy(slippage_bps=300)
        self.assertEqual(result["expected_out"], 1000 * ONE)
        self.assertEqual(result["amount_out_min"], 970 * ONE)

    def test_buy_tax_bps_is_added_to_slippage(self):
        # getAmountsOut models the LP fee only, so the token's own buy tax has
        # to widen amountOutMin or the swap reverts INSUFFICIENT_OUTPUT_AMOUNT.
        result = self.buy(slippage_bps=300, buy_tax_bps=500)
        self.assertEqual(result["amount_out_min"], 920 * ONE)

    def test_absurd_slippage_rejected(self):
        with self.assertRaises(ChainError) as ctx:
            self.buy(slippage_bps=9999)
        self.assertEqual(ctx.exception.code, "SLIPPAGE_RANGE")

    def test_combined_slippage_and_tax_cannot_exceed_the_cap(self):
        # Each leg is "in range" on its own, but they are ADDED: 4000+4000 would
        # accept 20% of the quote, i.e. an 80% loss, past the 50% fat-finger cap.
        with self.assertRaises(ChainError) as ctx:
            self.buy(slippage_bps=4000, buy_tax_bps=4000)
        self.assertEqual(ctx.exception.code, "SLIPPAGE_RANGE")
        self.assertEqual(self.chain.quoted_paths, [])
        # the boundary itself still works
        self.assertEqual(self.buy(slippage_bps=4000, buy_tax_bps=1000)["amount_out_min"],
                         500 * ONE)

    def test_fractional_bps_rejected(self):
        # A float bps makes amountOutMin a float: inexact past 2**53 and not a
        # uint256 the ABI encoder accepts - and it would only blow up after the
        # approve is already on chain.
        for kw in ({"slippage_bps": 300.0}, {"buy_tax_bps": 12.5}):
            with self.assertRaises(ChainError) as ctx:
                self.buy(**kw)
            self.assertEqual(ctx.exception.code, "SLIPPAGE_TYPE")

    def test_amount_out_min_is_always_an_exact_int(self):
        self.chain.quotes = {(USDT, WBNB, TOKEN): 1234567890123456789012}
        amount_out_min = self.buy(slippage_bps=300)["amount_out_min"]
        self.assertIsInstance(amount_out_min, int)
        self.assertEqual(amount_out_min, 1234567890123456789012 * 9700 // 10_000)

    # -- path discovery --

    def test_uses_wbnb_path_when_no_direct_pair(self):
        self.chain.pair = ZERO
        result = self.buy()
        self.assertEqual(result["path"], [USDT, WBNB, TOKEN])
        self.assertNotIn((USDT, TOKEN), self.chain.quoted_paths)

    def test_prefers_direct_pair_when_it_quotes_better(self):
        self.chain.pair = PAIR
        self.chain.quotes = {(USDT, TOKEN): 1100 * ONE,
                             (USDT, WBNB, TOKEN): 1000 * ONE}
        result = self.buy()
        self.assertEqual(result["path"], [USDT, TOKEN])
        self.assertEqual(result["expected_out"], 1100 * ONE)
        self.assertEqual(len(self.chain.quoted_paths), 2)  # both were compared

    def test_prefers_wbnb_path_when_direct_pair_quotes_worse(self):
        self.chain.pair = PAIR
        self.chain.quotes = {(USDT, TOKEN): 900 * ONE,
                             (USDT, WBNB, TOKEN): 1000 * ONE}
        self.assertEqual(self.buy()["path"], [USDT, WBNB, TOKEN])

    def test_buying_wbnb_is_a_single_hop(self):
        self.chain.quotes = {(USDT, WBNB): 500 * ONE}
        result = self.buy(token_address=WBNB)
        self.assertEqual(result["path"], [USDT, WBNB])

    def test_rejects_zero_and_same_token(self):
        for kw, code in (({"token_address": ZERO}, "ADDRESS_ZERO"),
                         ({"recipient": ZERO}, "ADDRESS_ZERO"),
                         ({"token_address": USDT}, "SAME_TOKEN")):
            with self.assertRaises(ChainError) as ctx:
                self.buy(**kw)
            self.assertEqual(ctx.exception.code, code)

    def test_no_route_raises_quote_stale(self):
        self.chain.quotes = {}
        with self.assertRaises(QuoteStale):
            self.buy()

    # -- gas floor --

    def test_insufficient_bnb_aborts_before_any_quoting(self):
        self.chain.bnb = 10 ** 12  # 0.000001 BNB
        with self.assertRaises(InsufficientGas) as ctx:
            self.buy(dry_run=False)
        self.assertEqual(ctx.exception.code, "BNB_FLOOR")
        self.assertEqual(self.chain.quoted_paths, [])
        self.assertEqual(self.chain.sent, [])

    def test_bnb_floor_is_configurable(self):
        self.chain.bnb = 10 ** 15
        with self.assertRaises(InsufficientGas):
            self.buy(min_bnb_wei=2 * 10 ** 15)
        self.assertIsNotNone(self.buy(min_bnb_wei=10 ** 14))

    def test_insufficient_usdt_aborts(self):
        self.chain.usdt_balance = ONE
        with self.assertRaises(ChainError) as ctx:
            self.buy(usdt_amount="25")
        self.assertEqual(ctx.exception.code, "USDT_BALANCE")

    # -- pre-flight --

    def test_estimate_gas_revert_aborts_without_sending(self):
        self.chain.allowance = 10 ** 30  # already approved: we reach the swap
        self.chain.swap_revert = (
            "execution reverted: PancakeRouter: INSUFFICIENT_OUTPUT_AMOUNT")
        with self.assertRaises(SwapReverted) as ctx:
            self.buy(dry_run=False)
        self.assertEqual(ctx.exception.code, "PREFLIGHT_REVERT")
        self.assertIn("INSUFFICIENT_OUTPUT_AMOUNT", str(ctx.exception))
        self.assertEqual(self.chain.sent, [], "must not broadcast a known-revert tx")
        self.assertEqual(self.chain.signed_with, [])

    def test_receipt_status_zero_raises(self):
        self.chain.allowance = 10 ** 30
        self.chain.receipt_status = 0
        with self.assertRaises(SwapReverted) as ctx:
            self.buy(dry_run=False)
        self.assertEqual(ctx.exception.code, "SWAP_FAILED")

    # -- the tx is already on chain: never report "failed" for "unknown" --

    def test_unformatted_receipt_is_not_mistaken_for_a_revert(self):
        # A provider without result formatters returns hex strings. Reading a
        # SUCCESSFUL swap as a revert makes the caller retry and buy twice.
        self.chain.allowance = 10 ** 30
        self.w3.eth.wait_for_transaction_receipt = (
            lambda h, timeout=None: {"status": "0x1", "gasUsed": "0x30d40"})
        result = self.buy(dry_run=False)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["gas_used"], 0x30D40)

    def test_missing_status_field_still_fails_closed(self):
        self.chain.allowance = 10 ** 30
        self.w3.eth.wait_for_transaction_receipt = (
            lambda h, timeout=None: {"gasUsed": 21_000})
        with self.assertRaises(SwapReverted) as ctx:
            self.buy(dry_run=False)
        self.assertEqual(ctx.exception.code, "SWAP_FAILED")

    def test_receipt_timeout_keeps_the_tx_hash_for_reconciliation(self):
        # The swap IS broadcast; a bare TimeExhausted would lose the hash and
        # the caller would have no way to tell "not mined yet" from "not sent".
        self.chain.allowance = 10 ** 30

        def timeout(tx_hash, timeout=None):
            raise TimeoutError("not mined")

        self.w3.eth.wait_for_transaction_receipt = timeout
        with self.assertRaises(ChainError) as ctx:
            self.buy(dry_run=False)
        self.assertEqual(ctx.exception.code, "RECEIPT_UNCONFIRMED")
        self.assertEqual(len(self.chain.sent), 1)
        self.assertTrue(ctx.exception.tx_hash.startswith("0x"))
        self.assertIn("before ANY retry", str(ctx.exception))

    def test_broadcast_failure_is_reported_as_unconfirmed_with_the_nonce(self):
        # The node may already hold the tx and only the response was lost.
        self.chain.allowance = 10 ** 30

        def reset(raw):
            self.chain.sent.append(raw)
            raise ConnectionResetError("connection reset by peer")

        self.w3.eth.send_raw_transaction = reset
        with self.assertRaises(ChainError) as ctx:
            self.buy(dry_run=False)
        self.assertEqual(ctx.exception.code, "BROADCAST_UNCONFIRMED")
        self.assertEqual(ctx.exception.nonce, 7)
        self.assertIn("before ANY retry", str(ctx.exception))

    def test_approve_receipt_timeout_does_not_reach_the_swap(self):
        self.chain.allowance = 0

        def timeout(tx_hash, timeout=None):
            raise TimeoutError("not mined")

        self.w3.eth.wait_for_transaction_receipt = timeout
        with self.assertRaises(ChainError) as ctx:
            self.buy(dry_run=False)
        self.assertEqual(ctx.exception.code, "RECEIPT_UNCONFIRMED")
        self.assertEqual(len(self.chain.sent), 1)   # approve only, no swap
        self.assertIsNone(self.chain.swap_args)

    # -- dry run --

    def test_dry_run_is_the_default(self):
        result = bsc_swap.buy_token(self.w3, self.key, TOKEN, "25", 300, RECIPIENT)
        self.assertEqual(result["status"], "dry_run")
        self.assertIsNone(result["tx_hash"])
        self.assertEqual(self.chain.sent, [])

    def test_dry_run_sends_nothing_but_still_quotes(self):
        result = self.buy(dry_run=True)
        self.assertEqual(self.chain.sent, [])
        self.assertEqual(self.chain.signed_with, [])
        self.assertIsNone(self.chain.approve_args)
        # ...while the plan is fully populated for the log
        self.assertEqual(result["amount_in"], 25 * ONE)
        self.assertEqual(result["amount_out_min"], 970 * ONE)
        self.assertEqual(result["path"], [USDT, WBNB, TOKEN])

    def test_dry_run_with_allowance_does_the_readonly_preflight(self):
        self.chain.allowance = 10 ** 30
        result = self.buy(dry_run=True)
        self.assertEqual(result["gas_used"], 210_000)  # estimate, nothing sent
        self.assertEqual(self.chain.sent, [])

    # -- live path --

    def test_live_buy_approves_exact_amount_then_swaps(self):
        self.chain.allowance = 0
        result = self.buy(dry_run=False, usdt_amount="25")

        self.assertEqual(self.chain.approve_args, (ROUTER, 25 * ONE))
        self.assertNotEqual(self.chain.approve_args[1], 2 ** 256 - 1)  # never infinite
        self.assertEqual(len(self.chain.sent), 2)  # approve + swap
        self.assertEqual(self.chain.signed_with, [TEST_KEY_HEX, TEST_KEY_HEX])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["gas_used"], 198_765)
        self.assertTrue(result["tx_hash"].startswith("0x"))

    def test_live_buy_skips_approve_when_allowance_sufficient(self):
        self.chain.allowance = 25 * ONE
        self.buy(dry_run=False, usdt_amount="25")
        self.assertIsNone(self.chain.approve_args)
        self.assertEqual(len(self.chain.sent), 1)

    def test_swap_call_args_are_the_fee_on_transfer_variant(self):
        self.chain.allowance = 10 ** 30
        self.buy(dry_run=False)
        amount_in, amount_out_min, path, to, deadline = self.chain.swap_args
        self.assertEqual(amount_in, 25 * ONE)
        self.assertEqual(amount_out_min, 970 * ONE)
        self.assertEqual(path, [USDT, WBNB, TOKEN])
        self.assertEqual(to, RECIPIENT)
        self.assertGreater(deadline, 0)

    def test_rejects_raw_key_material(self):
        with self.assertRaises(ChainError):
            bsc_swap.buy_token(self.w3, TEST_KEY_HEX, TOKEN, "25", 300, RECIPIENT)


# ---------- client helper tests ----------

class ClientHelperTest(unittest.TestCase):

    def setUp(self):
        bsc_client._DECIMALS_CACHE.clear()
        self.w3 = FakeWeb3(FakeChain())

    def test_gas_price_clamped_to_floor_and_ceiling(self):
        self.w3.eth.gas_price = 1
        self.assertEqual(bsc_client.gas_price(self.w3, 0.1, 5.0), 10 ** 8)
        self.w3.eth.gas_price = 500 * 10 ** 9
        self.assertEqual(bsc_client.gas_price(self.w3, 0.1, 5.0), 5 * 10 ** 9)
        self.w3.eth.gas_price = 2 * 10 ** 9
        self.assertEqual(bsc_client.gas_price(self.w3, 0.1, 5.0), 2 * 10 ** 9)

    def test_insane_decimals_rejected(self):
        self.w3.chain.usdt_decimals = 200
        with self.assertRaises(ChainError):
            bsc_client.token_decimals(self.w3, USDT)

    def test_to_base_units_rejects_non_positive(self):
        for bad in ("0", "-1", "abc", "nan", "Infinity", "-Infinity"):
            with self.assertRaises(ChainError):
                bsc_client.to_base_units(bad, 18)

    def test_to_base_units_truncates_and_never_rounds_up(self):
        # Decimal's default 28-digit context rounds HALF_EVEN here, which used
        # to hand back one base unit MORE than the caller asked to spend.
        from decimal import Decimal, localcontext
        for amount in ("12345678901.123456789012345678",
                       "99999999999.999999999999999999",
                       "0.1", "25"):
            with localcontext() as ctx:
                ctx.prec = 80
                exact = int(Decimal(amount) * (Decimal(10) ** 18))
            self.assertEqual(bsc_client.to_base_units(amount, 18), exact, amount)

    def test_is_zero_address(self):
        self.assertTrue(bsc_client.is_zero_address(ZERO))
        self.assertFalse(bsc_client.is_zero_address(PAIR))


# ---------- secret handling tests ----------

class SecretKeyTest(unittest.TestCase):

    def test_never_leaks_through_any_rendering(self):
        secret = TEST_KEY_HEX
        key = SecretKey(secret)
        renderings = [
            repr(key), str(key), f"{key}", f"{key!r}", f"{key!s}",
            "{}".format(key), "{!r}".format(key), "{:>40}".format(key),
            "%s" % key, "%r" % key, format(key), format(key, ">40"),
            str([key]), str({"k": key}),
        ]
        for rendered in renderings:
            self.assertIn("<redacted>", rendered)
            self.assertNotIn(secret, rendered)
            self.assertNotIn(secret[2:], rendered)
            self.assertNotIn("ab" * 8, rendered)
        self.assertEqual(key.reveal(), secret)

    def test_not_serialisable(self):
        import copy
        import pickle
        key = SecretKey(TEST_KEY_HEX)
        with self.assertRaises(TypeError):
            pickle.dumps(key)
        with self.assertRaises(TypeError):
            copy.deepcopy(key)

    def test_signer_error_never_echoes_the_key(self):
        # sign_transaction is the one call handed raw key material. A signer
        # that quotes its own input must not put the key in the traceback.
        import traceback
        chain = FakeChain()
        chain.allowance = 10 ** 30
        w3 = FakeWeb3(chain)
        w3.eth.account.sign_transaction = (
            lambda tx, private_key: (_ for _ in ()).throw(
                ValueError("invalid private key " + private_key)))
        with mock.patch.object(bsc_swap, "address_from_key", return_value=SENDER):
            try:
                bsc_swap.buy_token(w3, SecretKey(TEST_KEY_HEX), TOKEN, "25", 300,
                                   RECIPIENT, dry_run=False)
            except ChainError as exc:
                self.assertEqual(exc.code, "SIGN_FAILED")
                text = traceback.format_exc()
        self.assertNotIn(TEST_KEY_HEX, text)
        self.assertNotIn("ab" * 8, text)
        self.assertEqual(chain.sent, [])

    def test_traceback_of_failed_buy_has_no_key(self):
        import traceback
        chain = FakeChain()
        chain.bnb = 0
        w3 = FakeWeb3(chain)
        key = SecretKey(TEST_KEY_HEX)
        with mock.patch.object(bsc_swap, "address_from_key", return_value=SENDER):
            try:
                bsc_swap.buy_token(w3, key, TOKEN, "25", 300, RECIPIENT, dry_run=False)
            except InsufficientGas:
                text = traceback.format_exc()
        self.assertNotIn(TEST_KEY_HEX, text)
        self.assertNotIn("ab" * 8, text)


class LoadSecretKeyTest(unittest.TestCase):

    def _write(self, contents):
        handle = tempfile.NamedTemporaryFile("w", suffix=".key", delete=False)
        handle.write(contents)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_loads_from_file_path_in_env_and_strips_whitespace(self):
        path = self._write("  " + TEST_KEY_HEX + "\n")
        with mock.patch.dict(os.environ, {"BSC_PRIVATE_KEY_FILE": path}):
            key = load_secret_key("BSC_PRIVATE_KEY_FILE")
        self.assertIsInstance(key, SecretKey)
        self.assertEqual(key.reveal(), TEST_KEY_HEX)
        self.assertEqual(str(key), "<redacted>")

    def test_accepts_key_without_0x_and_normalises(self):
        path = self._write("AB" * 32)
        with mock.patch.dict(os.environ, {"BSC_PRIVATE_KEY_FILE": path}):
            self.assertEqual(load_secret_key("BSC_PRIVATE_KEY_FILE").reveal(),
                             TEST_KEY_HEX)

    def test_missing_env_var_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ChainError) as ctx:
                load_secret_key("BSC_PRIVATE_KEY_FILE")
        self.assertEqual(ctx.exception.code, "KEY_ENV_MISSING")

    def test_env_var_holding_the_key_itself_is_rejected(self):
        # The classic misconfiguration: someone puts the key in the var.
        with mock.patch.dict(os.environ, {"BSC_PRIVATE_KEY_FILE": TEST_KEY_HEX}):
            with self.assertRaises(ChainError) as ctx:
                load_secret_key("BSC_PRIVATE_KEY_FILE")
        self.assertEqual(ctx.exception.code, "KEY_FILE_UNREADABLE")
        self.assertNotIn(TEST_KEY_HEX[10:], str(ctx.exception))

    def test_malformed_key_rejected(self):
        for bad in ("not-a-key", "0x1234", "ab" * 33):
            path = self._write(bad)
            with mock.patch.dict(os.environ, {"BSC_PRIVATE_KEY_FILE": path}):
                with self.assertRaises(ChainError) as ctx:
                    load_secret_key("BSC_PRIVATE_KEY_FILE")
            self.assertEqual(ctx.exception.code, "KEY_FORMAT")

    def test_empty_file_rejected(self):
        path = self._write("\n   \n")
        with mock.patch.dict(os.environ, {"BSC_PRIVATE_KEY_FILE": path}):
            with self.assertRaises(ChainError) as ctx:
                load_secret_key("BSC_PRIVATE_KEY_FILE")
        self.assertEqual(ctx.exception.code, "KEY_FILE_EMPTY")


class ErrorFormatTest(unittest.TestCase):

    def test_str_form_matches_repo_convention(self):
        exc = bsc_client.BSC.InsufficientGas("BNB_FLOOR", "no gas")
        self.assertEqual(str(exc), "bsc error BNB_FLOOR: no gas")
        self.assertIsInstance(exc, InsufficientGas)
        self.assertIsInstance(exc, ChainError)

    def test_chain_label_is_not_global(self):
        # Building another chain's family must not relabel the BSC one.
        other = chain_error_family("solana")
        self.assertEqual(str(other.ChainError("X", "y")), "solana error X: y")
        self.assertEqual(str(bsc_client.BSC.ChainError("X", "y")), "bsc error X: y")
        self.assertEqual(ChainError.exchange, "chain")


if __name__ == "__main__":
    unittest.main()
