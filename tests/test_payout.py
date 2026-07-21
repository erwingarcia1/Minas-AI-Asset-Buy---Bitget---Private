"""Unit tests for the payout package (sub->main transfer + on-chain withdrawal).

Every Bitget HTTP call is mocked at requests.Session.request — no network, no
real credentials. The safety tests deliberately install a fake that EXPLODES on
any request, so "nothing was sent" is proven, not assumed.

Run from the repo root:  python -m unittest discover -s tests -v
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from payout import errors
from payout.bitget_payout import BitgetPayout, Secret
from payout.coin_chains import CoinChains
from payout.errors import TransferError, WithdrawalBlockedError, WithdrawalError

ALLOWED_BEP20 = "0x1111111111111111111111111111111111111111"
ALLOWED_SOL = "So11111111111111111111111111111111111111112"
ALLOWLIST = [ALLOWED_BEP20, ALLOWED_SOL]

COIN_USDT = {
    "coinId": "2",
    "coin": "USDT",
    "transfer": "true",
    "chains": [
        {"chain": "BEP20", "needTag": "false", "withdrawable": "true", "rechargeable": "true",
         "withdrawFee": "0.29", "extraWithdrawFee": "0", "minWithdrawAmount": "5",
         "withdrawMinScale": "6", "withdrawStep": "0", "congestion": "normal",
         "contractAddress": "0x55d398326f99059fF775485246999027B3197955"},
        {"chain": "SOL", "needTag": "false", "withdrawable": "true", "rechargeable": "true",
         "withdrawFee": "0.8", "extraWithdrawFee": "0", "minWithdrawAmount": "2",
         "withdrawMinScale": "4", "withdrawStep": "0", "congestion": "normal",
         "contractAddress": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"},
    ],
}

RECORD = {
    "orderId": "1278112570963742720", "clientOid": "payout-1", "coin": "USDT",
    "type": "withdraw", "dest": "on_chain", "size": "20", "fee": "0.29", "status": "success",
    "toAddress": ALLOWED_BEP20, "chain": "BEP20", "confirm": "15",
    "tradeId": "0xdeadbeefcafe", "cTime": "1740451703778", "uTime": "1740451703778",
}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def ok(data):
    return FakeResponse({"code": "00000", "msg": "success", "data": data})


def err(code, msg="rejected", status=200):
    return FakeResponse({"code": code, "msg": msg, "data": None}, status_code=status)


class FakeBitgetHTTP:
    """Routes payout requests to canned responses and records every call."""

    def __init__(self, coin=COIN_USDT, withdraw=None, transfer=None, records=None, addresses=None):
        self.coin = coin
        self.withdraw = withdraw or ok({"orderId": "900", "clientOid": "payout-1"})
        self.transfer = transfer or ok({"transferId": "172947298237423", "clientOid": "payout-1"})
        self.records = records if records is not None else [RECORD]
        self.addresses = addresses if addresses is not None else [
            {"coin": "USDT", "chain": "BEP20", "address": ALLOWED_BEP20, "type": "EVM"}]
        self.calls = []

    def __call__(self, method, url, data=None, headers=None, timeout=None):
        body = json.loads(data) if data else None
        self.calls.append({"method": method, "url": url, "body": body, "headers": headers})
        if "/spot/public/coins" in url:
            return ok([self.coin] if self.coin else [])
        if "/spot/wallet/withdrawal-records" in url:
            return ok(self.records)
        if "/spot/wallet/withdrawal" in url:
            return self.withdraw
        if "/account/sub-master-transfer" in url:
            return self.transfer
        if "/account/withdraw-address" in url:
            return ok({"addressList": self.addresses, "cursor": ""})
        raise AssertionError(f"unexpected request: {method} {url}")

    @property
    def posts(self):
        return [c for c in self.calls if c["method"] == "POST"]


def no_network(*args, **kwargs):
    raise AssertionError("a network call was attempted; it must not have been")


def patch_http(fake):
    return mock.patch("requests.Session.request", new=fake)


def setUpModule():
    # The live-path tests deliberately exercise the loud LIVE/DRY RUN logging.
    logging.getLogger("payout").setLevel(logging.CRITICAL)


def main_payout(**kwargs):
    kwargs.setdefault("max_withdrawal", 1000)
    return BitgetPayout("k", "s", "p", account_role="main", **kwargs)


def sub_payout(**kwargs):
    return BitgetPayout("k", "s", "p", account_role="sub", **kwargs)


class TestAllowlist(unittest.TestCase):
    def test_unlisted_address_blocks_before_any_network_call(self):
        with patch_http(no_network):
            payout = main_payout()
            with self.assertRaises(WithdrawalBlockedError) as ctx:
                payout.withdraw_onchain("USDT", "BEP20", "0xdeadbeef00000000000000000000000000000000",
                                        20, "payout-1", ALLOWLIST, dry_run=False)
        self.assertEqual(ctx.exception.code, "ALLOWLIST")
        self.assertIn("Nothing was sent", ctx.exception.message)

    def test_empty_allowlist_blocks(self):
        with patch_http(no_network):
            with self.assertRaises(WithdrawalBlockedError):
                main_payout().withdraw_onchain("USDT", "BEP20", ALLOWED_BEP20, 20, "payout-1", [], dry_run=False)

    def test_evm_address_matches_case_insensitively(self):
        fake = FakeBitgetHTTP()
        with patch_http(fake):
            result = main_payout().withdraw_onchain("USDT", "BEP20", ALLOWED_BEP20.upper().replace("0X", "0x"),
                                                    20, "payout-1", ALLOWLIST, dry_run=True)
        self.assertEqual(result["status"], "simulated")

    def test_solana_address_is_case_sensitive(self):
        with patch_http(no_network):
            with self.assertRaises(WithdrawalBlockedError):
                main_payout().withdraw_onchain("USDT", "SOL", ALLOWED_SOL.lower(), 20,
                                               "payout-1", ALLOWLIST, dry_run=False)


class TestSubKeyCannotWithdraw(unittest.TestCase):
    def test_sub_credentials_refuse_to_withdraw(self):
        with patch_http(no_network):
            with self.assertRaises(WithdrawalBlockedError) as ctx:
                sub_payout(max_withdrawal=1000).withdraw_onchain(
                    "USDT", "BEP20", ALLOWED_BEP20, 20, "payout-1", ALLOWLIST, dry_run=False)
        self.assertEqual(ctx.exception.code, "SUBKEY")
        self.assertIn("transfer_sub_to_main", ctx.exception.message)

    def test_main_key_refuses_to_be_transfer_source(self):
        with patch_http(no_network):
            with self.assertRaises(TransferError) as ctx:
                main_payout().transfer_sub_to_main("USDT", 20, "payout-1", dry_run=False)
        self.assertEqual(ctx.exception.code, "SUBKEY")


class TestDryRun(unittest.TestCase):
    def test_withdraw_dry_run_sends_nothing_but_shows_the_call(self):
        fake = FakeBitgetHTTP()
        with patch_http(fake):
            result = main_payout().withdraw_onchain("USDT", "BEP20", ALLOWED_BEP20, 20, "payout-1", ALLOWLIST)
        self.assertEqual(fake.posts, [])  # only the public GET /coins happened
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["sent"])
        self.assertEqual(result["endpoint"], "/api/v2/spot/wallet/withdrawal")
        self.assertEqual(result["body"], {
            "coin": "USDT", "transferType": "on_chain", "chain": "BEP20",
            "address": ALLOWED_BEP20, "size": "20", "clientOid": "payout-1"})

    def test_transfer_dry_run_sends_nothing(self):
        fake = FakeBitgetHTTP()
        with patch_http(fake):
            result = sub_payout().transfer_sub_to_main("USDT", "12.5", "payout-1")
        self.assertEqual(fake.calls, [])
        self.assertFalse(result["sent"])
        self.assertEqual(result["endpoint"], "/api/v3/account/sub-master-transfer")
        self.assertEqual(result["body"], {"fromType": "spot", "toType": "spot", "amount": "12.5",
                                          "coin": "USDT", "clientOid": "payout-1"})

    def test_dry_run_is_the_default(self):
        fake = FakeBitgetHTTP()
        with patch_http(fake):
            self.assertTrue(sub_payout().transfer_sub_to_main("USDT", 20, "payout-1")["dry_run"])
            self.assertTrue(main_payout().withdraw_onchain(
                "USDT", "BEP20", ALLOWED_BEP20, 20, "payout-1", ALLOWLIST)["dry_run"])


class TestLiveCalls(unittest.TestCase):
    def test_live_withdrawal_posts_the_expected_body_and_signs_it(self):
        fake = FakeBitgetHTTP()
        with patch_http(fake):
            result = main_payout().withdraw_onchain("USDT", "bep20", ALLOWED_BEP20, "20.5",
                                                    "payout-1", ALLOWLIST, dry_run=False)
        post = fake.posts[0]
        self.assertIn("/api/v2/spot/wallet/withdrawal", post["url"])
        self.assertEqual(post["body"]["chain"], "BEP20")  # canonical exchange spelling
        self.assertEqual(post["body"]["transferType"], "on_chain")
        self.assertEqual(post["body"]["size"], "20.5")
        self.assertEqual(result["status"], "submitted")
        # same ACCESS-SIGN scheme as bitget_client.py
        ts = post["headers"]["ACCESS-TIMESTAMP"]
        expected = base64.b64encode(hmac.new(
            b"s", (ts + "POST" + "/api/v2/spot/wallet/withdrawal" + json.dumps(post["body"], separators=(",", ":"))
                   ).encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(post["headers"]["ACCESS-SIGN"], expected)

    def test_live_transfer_posts_to_the_uta_endpoint(self):
        fake = FakeBitgetHTTP()
        with patch_http(fake):
            result = sub_payout().transfer_sub_to_main("USDT", 20, "payout-1", dry_run=False)
        self.assertIn("/api/v3/account/sub-master-transfer", fake.posts[0]["url"])
        self.assertEqual(result["data"]["transferId"], "172947298237423")

    def test_address_book_is_read_only_lookup(self):
        fake = FakeBitgetHTTP()
        with patch_http(fake):
            addresses = main_payout().list_whitelisted_addresses(coin="USDT")
        self.assertEqual(addresses[0]["address"], ALLOWED_BEP20)
        self.assertEqual(fake.posts, [])


class TestChainValidation(unittest.TestCase):
    def test_unknown_chain_string_is_rejected(self):
        fake = FakeBitgetHTTP()
        with patch_http(fake):
            with self.assertRaises(WithdrawalBlockedError) as ctx:
                main_payout().withdraw_onchain("USDT", "BSC", ALLOWED_BEP20, 20,
                                               "payout-1", ALLOWLIST, dry_run=False)
        self.assertEqual(ctx.exception.code, "CHAIN")
        self.assertIn("BEP20", ctx.exception.message)
        self.assertEqual(fake.posts, [])

    def test_amount_below_chain_minimum_is_rejected(self):
        fake = FakeBitgetHTTP()
        with patch_http(fake):
            with self.assertRaises(WithdrawalBlockedError) as ctx:
                main_payout().withdraw_onchain("USDT", "BEP20", ALLOWED_BEP20, "1",
                                               "payout-1", ALLOWLIST, dry_run=False)
        self.assertIn("minWithdrawAmount 5", ctx.exception.message)
        self.assertIn("43114", ctx.exception.message)
        self.assertEqual(fake.posts, [])

    def test_amount_over_per_call_cap_is_rejected(self):
        with patch_http(no_network):
            with self.assertRaises(WithdrawalBlockedError) as ctx:
                main_payout(max_withdrawal=50).withdraw_onchain(
                    "USDT", "BEP20", ALLOWED_BEP20, 500, "payout-1", ALLOWLIST, dry_run=False)
        self.assertEqual(ctx.exception.code, "CAP")

    def test_missing_cap_is_refused(self):
        with patch_http(no_network):
            payout = BitgetPayout("k", "s", "p", account_role="main")
            with self.assertRaises(WithdrawalBlockedError) as ctx:
                payout.withdraw_onchain("USDT", "BEP20", ALLOWED_BEP20, 20, "payout-1", ALLOWLIST)
        self.assertEqual(ctx.exception.code, "CAP")

    def test_blank_client_oid_is_refused(self):
        with patch_http(no_network):
            with self.assertRaises(WithdrawalError):
                main_payout().withdraw_onchain("USDT", "BEP20", ALLOWED_BEP20, 20, "", ALLOWLIST)


class TestCoinChains(unittest.TestCase):
    def test_metadata_is_cached(self):
        fake = FakeBitgetHTTP()
        with patch_http(fake):
            chains = CoinChains()
            chains.chain_info("USDT", "BEP20")
            chains.chain_info("USDT", "SOL")
        self.assertEqual(len(fake.calls), 1)

    def test_missing_field_fails_closed(self):
        coin = json.loads(json.dumps(COIN_USDT))
        del coin["chains"][0]["minWithdrawAmount"]
        fake = FakeBitgetHTTP(coin=coin)
        with patch_http(fake):
            reason = CoinChains().amount_rejection_reason("USDT", "BEP20", 20)
        self.assertIn("missing minWithdrawAmount", reason)
        self.assertIn("fail closed", reason)

    def test_withdrawals_disabled_on_chain(self):
        coin = json.loads(json.dumps(COIN_USDT))
        coin["chains"][0]["withdrawable"] = "false"
        with patch_http(FakeBitgetHTTP(coin=coin)):
            self.assertIn("withdrawable", CoinChains().amount_rejection_reason("USDT", "BEP20", 20))

    def test_congested_chain_rejected(self):
        coin = json.loads(json.dumps(COIN_USDT))
        coin["chains"][0]["congestion"] = "congested"
        with patch_http(FakeBitgetHTTP(coin=coin)):
            self.assertIn("congestion", CoinChains().amount_rejection_reason("USDT", "BEP20", 20))

    def test_too_many_decimals_rejected(self):
        with patch_http(FakeBitgetHTTP()):
            reason = CoinChains().amount_rejection_reason("USDT", "SOL", "10.123456789")
        self.assertIn("43125", reason)

    def test_withdraw_step_violation_rejected(self):
        coin = json.loads(json.dumps(COIN_USDT))
        coin["chains"][0]["withdrawStep"] = "10"
        with patch_http(FakeBitgetHTTP(coin=coin)):
            self.assertIn("43124", CoinChains().amount_rejection_reason("USDT", "BEP20", "25"))

    def test_amount_not_above_fee_rejected(self):
        coin = json.loads(json.dumps(COIN_USDT))
        coin["chains"][0]["minWithdrawAmount"] = "0.1"
        coin["chains"][0]["withdrawFee"] = "5"
        with patch_http(FakeBitgetHTTP(coin=coin)):
            self.assertIn("43112", CoinChains().amount_rejection_reason("USDT", "BEP20", "3"))

    def test_good_amount_passes(self):
        with patch_http(FakeBitgetHTTP()):
            self.assertIsNone(CoinChains().amount_rejection_reason("USDT", "BEP20", "20"))

    def test_unreadable_metadata_blocks(self):
        with mock.patch("requests.Session.request", return_value=err("40001", "boom", 400)):
            with self.assertRaises(WithdrawalBlockedError) as ctx:
                CoinChains().chain_info("USDT", "BEP20")
        self.assertEqual(ctx.exception.code, "METADATA")


class TestErrorCodeMapping(unittest.TestCase):
    CASES = [
        ("40038", "IP"),
        ("40052", "24h"),
        ("70217", "address book"),
        ("11000", "address book"),
        ("47003", "address book"),
        ("43114", "minWithdrawAmount"),
        ("43112", "withdrawFee"),
        ("43124", "withdrawStep"),
        ("43125", "decimal places"),
        ("43113", "daily withdrawal limit"),
        ("70213", "24h withdrawal limit"),
        ("70226", "open spot orders"),
        ("40027", "KYC"),
        ("40101", "KYC"),
        ("40016", "2FA"),
        ("40021", "disabled"),
    ]

    def test_each_code_yields_actionable_guidance(self):
        for code, expected in self.CASES:
            with self.subTest(code=code):
                fake = FakeBitgetHTTP(withdraw=err(code))
                with patch_http(fake):
                    with self.assertRaises(WithdrawalError) as ctx:
                        main_payout().withdraw_onchain("USDT", "BEP20", ALLOWED_BEP20, 20,
                                                       "payout-1", ALLOWLIST, dry_run=False)
                self.assertEqual(ctx.exception.code, code)
                self.assertIn(expected, ctx.exception.message)
                self.assertNotIsInstance(ctx.exception, WithdrawalBlockedError)

    def test_transfer_43127_explains_the_parent_key_requirement(self):
        fake = FakeBitgetHTTP(transfer=err("43127", "Sub transfer not by main account"))
        with patch_http(fake):
            with self.assertRaises(TransferError) as ctx:
                sub_payout().transfer_sub_to_main("USDT", 20, "payout-1", dry_run=False)
        self.assertIn("PARENT", ctx.exception.message)

    def test_guidance_helper_covers_every_documented_code(self):
        for code, _ in self.CASES:
            self.assertTrue(errors.guidance(code))
        self.assertEqual(errors.guidance("99999"), "")


class TestIdempotency(unittest.TestCase):
    def test_duplicate_client_oid_becomes_a_lookup_not_a_failure(self):
        fake = FakeBitgetHTTP(withdraw=err(errors.DUPLICATE_CLIENT_OID, "clientOrderId duplicate"))
        with patch_http(fake):
            result = main_payout().withdraw_onchain("USDT", "BEP20", ALLOWED_BEP20, 20,
                                                    "payout-1", ALLOWLIST, dry_run=False)
        self.assertEqual(result["status"], "already_submitted")
        self.assertFalse(result["sent"])
        self.assertEqual(result["record"]["tradeId"], "0xdeadbeefcafe")  # on-chain tx hash
        self.assertEqual(result["record"]["status"], "success")

    def test_duplicate_transfer_is_not_resent(self):
        fake = FakeBitgetHTTP(transfer=err(errors.DUPLICATE_CLIENT_OID, "clientOrderId duplicate"))
        with patch_http(fake):
            result = sub_payout().transfer_sub_to_main("USDT", 20, "payout-1", dry_run=False)
        self.assertEqual(result["status"], "already_submitted")
        self.assertEqual(len(fake.posts), 1)


class TestWithdrawalStatus(unittest.TestCase):
    def test_both_timestamps_are_required(self):
        with patch_http(no_network):
            payout = main_payout()
            with self.assertRaises(WithdrawalError):
                payout.get_withdrawal_status("payout-1", None, 2)
            with self.assertRaises(WithdrawalError):
                payout.get_withdrawal_status("payout-1", 2000, 1000)

    def test_lookup_filters_by_client_oid(self):
        other = dict(RECORD, clientOid="someone-else")
        fake = FakeBitgetHTTP(records=[other, RECORD])
        with patch_http(fake):
            record = main_payout().get_withdrawal_status("payout-1", 1, 2)
        self.assertEqual(record["clientOid"], "payout-1")
        self.assertIn("startTime=1", fake.calls[0]["url"])
        self.assertIn("endTime=2", fake.calls[0]["url"])

    def test_missing_record_returns_none(self):
        with patch_http(FakeBitgetHTTP(records=[])):
            self.assertIsNone(main_payout().get_withdrawal_status("payout-1", 1, 2))


class TestSecret(unittest.TestCase):
    def test_secret_never_renders_its_value(self):
        secret = Secret("super-secret-key")
        self.assertEqual(repr(secret), "<redacted>")
        self.assertEqual(str(secret), "<redacted>")
        self.assertNotIn("super-secret-key", f"{secret!r} {secret}")
        self.assertEqual(secret.reveal(), "super-secret-key")

    def test_secret_reads_from_the_path_in_an_env_var(self):
        fh = tempfile.NamedTemporaryFile("w", delete=False)
        fh.write("file-secret\n")
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        with mock.patch.dict(os.environ, {"TEST_PAYOUT_SECRET_FILE": fh.name}):
            self.assertEqual(Secret.from_env_file("TEST_PAYOUT_SECRET_FILE").reveal(), "file-secret")

    def test_unset_env_var_rejected(self):
        os.environ.pop("TEST_PAYOUT_DEFINITELY_UNSET", None)
        with self.assertRaisesRegex(ValueError, "TEST_PAYOUT_DEFINITELY_UNSET"):
            Secret.from_env_file("TEST_PAYOUT_DEFINITELY_UNSET")

    def test_repr_of_payout_hides_credentials(self):
        payout = BitgetPayout(Secret("api-key"), Secret("api-secret"), Secret("api-pass"), account_role="main")
        self.assertEqual(repr(payout), "<BitgetPayout role=main key=<redacted>>")

    def test_secret_credentials_still_sign_correctly(self):
        fake = FakeBitgetHTTP()
        payout = BitgetPayout(Secret("k"), Secret("s"), Secret("p"), account_role="main", max_withdrawal=1000)
        with patch_http(fake):
            payout.withdraw_onchain("USDT", "BEP20", ALLOWED_BEP20, 20, "payout-1", ALLOWLIST, dry_run=False)
        headers = fake.posts[0]["headers"]
        self.assertEqual(headers["ACCESS-KEY"], "k")
        self.assertEqual(headers["ACCESS-PASSPHRASE"], "p")


class TestTagChains(unittest.TestCase):
    """A chain that needs a memo/tag must never be paid out: the body has no tag."""

    def _coin_needing_tag(self):
        coin = json.loads(json.dumps(COIN_USDT))
        coin["chains"][0]["needTag"] = "true"
        return coin

    def test_tag_requiring_chain_is_refused(self):
        fake = FakeBitgetHTTP(coin=self._coin_needing_tag())
        with patch_http(fake):
            with self.assertRaises(WithdrawalBlockedError) as ctx:
                main_payout().withdraw_onchain("USDT", "BEP20", ALLOWED_BEP20, 20,
                                               "payout-1", ALLOWLIST, dry_run=False)
        self.assertEqual(ctx.exception.code, "PRECHECK")
        self.assertIn("needTag", ctx.exception.message)
        self.assertIn("IRRECOVERABLE", ctx.exception.message)
        self.assertEqual(fake.posts, [])

    def test_missing_need_tag_field_fails_closed(self):
        coin = json.loads(json.dumps(COIN_USDT))
        del coin["chains"][0]["needTag"]
        with patch_http(FakeBitgetHTTP(coin=coin)):
            reason = CoinChains().amount_rejection_reason("USDT", "BEP20", 20)
        self.assertIn("missing needTag", reason)

    def test_unrecognised_need_tag_value_fails_closed(self):
        coin = json.loads(json.dumps(COIN_USDT))
        coin["chains"][0]["needTag"] = "maybe"
        with patch_http(FakeBitgetHTTP(coin=coin)):
            self.assertIn("needTag", CoinChains().amount_rejection_reason("USDT", "BEP20", 20))


class TestAllowlistChainBinding(unittest.TestCase):
    def test_chain_pinned_entry_is_not_honoured_on_another_chain(self):
        allowlist = [{"chain": "BEP20", "address": ALLOWED_BEP20}]
        with patch_http(no_network):
            with self.assertRaises(WithdrawalBlockedError) as ctx:
                main_payout().withdraw_onchain("USDT", "SOL", ALLOWED_BEP20, 20,
                                               "payout-1", allowlist, dry_run=False)
        self.assertEqual(ctx.exception.code, "ALLOWLIST")
        self.assertIn("'SOL'", ctx.exception.message)

    def test_chain_pinned_entry_is_honoured_on_its_own_chain(self):
        fake = FakeBitgetHTTP()
        with patch_http(fake):
            result = main_payout().withdraw_onchain(
                "USDT", "bep20", ALLOWED_BEP20, 20, "payout-1",
                [{"chain": "BEP20", "address": ALLOWED_BEP20}], dry_run=False)
        self.assertEqual(result["status"], "submitted")

    def test_evm_address_is_refused_on_a_base58_chain(self):
        """The flat legacy allowlist holds both addresses; the chain still has to match."""
        fake = FakeBitgetHTTP()
        with patch_http(fake):
            with self.assertRaises(WithdrawalBlockedError) as ctx:
                main_payout().withdraw_onchain("USDT", "SOL", ALLOWED_BEP20, 20,
                                               "payout-1", ALLOWLIST, dry_run=False)
        self.assertEqual(ctx.exception.code, "PRECHECK")
        self.assertIn("IRRECOVERABLE", ctx.exception.message)
        self.assertEqual(fake.posts, [])

    def test_base58_address_is_refused_on_an_evm_chain(self):
        fake = FakeBitgetHTTP()
        with patch_http(fake):
            with self.assertRaises(WithdrawalBlockedError) as ctx:
                main_payout().withdraw_onchain("USDT", "BEP20", ALLOWED_SOL, 20,
                                               "payout-1", ALLOWLIST, dry_run=False)
        self.assertEqual(ctx.exception.code, "PRECHECK")
        self.assertEqual(fake.posts, [])

    def test_matching_families_still_pass(self):
        fake = FakeBitgetHTTP()
        with patch_http(fake):
            self.assertEqual(
                main_payout().withdraw_onchain("USDT", "SOL", ALLOWED_SOL, 20,
                                               "payout-1", ALLOWLIST, dry_run=False)["status"],
                "submitted")

    def test_native_coin_without_a_contract_is_not_second_guessed(self):
        coin = json.loads(json.dumps(COIN_USDT))
        coin["chains"][0]["contractAddress"] = ""
        with patch_http(FakeBitgetHTTP(coin=coin)):
            self.assertIsNone(CoinChains().address_rejection_reason(
                {"contractAddress": ""}, "BEP20", ALLOWED_SOL))

    def test_bare_string_allowlist_is_refused_not_iterated_by_character(self):
        with patch_http(no_network):
            with self.assertRaises(WithdrawalBlockedError) as ctx:
                main_payout().withdraw_onchain("USDT", "BEP20", ALLOWED_BEP20, 20,
                                               "payout-1", ALLOWED_BEP20, dry_run=False)
        self.assertIn("must be a list", ctx.exception.message)
        self.assertIn("(0 entries)", ctx.exception.message)


class TestFailClosedShapes(unittest.TestCase):
    def test_coins_endpoint_returning_an_object_blocks(self):
        class F(FakeBitgetHTTP):
            def __call__(self, method, url, **kw):
                if "/spot/public/coins" in url:
                    return ok({"coin": "USDT"})
                return super().__call__(method, url, **kw)

        with patch_http(F()):
            with self.assertRaises(WithdrawalBlockedError) as ctx:
                CoinChains().chain_info("USDT", "BEP20")
        self.assertEqual(ctx.exception.code, "METADATA")

    def test_withdrawal_records_object_payload_is_handled(self):
        fake = FakeBitgetHTTP(records={"resultList": [RECORD]})
        with patch_http(fake):
            record = main_payout().get_withdrawal_status("payout-1", 1, 2)
        self.assertEqual(record["tradeId"], "0xdeadbeefcafe")

    def test_unparseable_withdraw_step_is_refused(self):
        coin = json.loads(json.dumps(COIN_USDT))
        coin["chains"][0]["withdrawStep"] = "n/a"
        with patch_http(FakeBitgetHTTP(coin=coin)):
            self.assertIn("unparseable withdrawStep", CoinChains().amount_rejection_reason("USDT", "BEP20", "25"))

    def test_non_numeric_size_raises_a_typed_error_not_an_arithmetic_one(self):
        with patch_http(no_network):
            with self.assertRaises(WithdrawalBlockedError) as ctx:
                main_payout().withdraw_onchain("USDT", "BEP20", ALLOWED_BEP20, "twenty",
                                               "payout-1", ALLOWLIST, dry_run=True)
        self.assertEqual(ctx.exception.code, "ARGS")

    def test_non_numeric_cap_raises_a_typed_error(self):
        with patch_http(no_network):
            with self.assertRaises(WithdrawalBlockedError) as ctx:
                main_payout(max_withdrawal=True).withdraw_onchain(
                    "USDT", "BEP20", ALLOWED_BEP20, 20, "payout-1", ALLOWLIST, dry_run=False)
        self.assertEqual(ctx.exception.code, "CAP")

    def test_non_numeric_transfer_amount_raises_a_typed_error(self):
        with patch_http(no_network):
            with self.assertRaises(TransferError) as ctx:
                sub_payout().transfer_sub_to_main("USDT", "lots", "payout-1", dry_run=False)
        self.assertEqual(ctx.exception.code, "ARGS")


class TestDuplicateLookupCannotMaskSubmission(unittest.TestCase):
    def test_failed_record_lookup_still_reports_already_submitted(self):
        class F(FakeBitgetHTTP):
            def __call__(self, method, url, **kw):
                if "/withdrawal-records" in url:
                    return err("40001", "records unavailable")
                return super().__call__(method, url, **kw)

        fake = F(withdraw=err(errors.DUPLICATE_CLIENT_OID, "clientOrderId duplicate"))
        with patch_http(fake):
            result = main_payout().withdraw_onchain("USDT", "BEP20", ALLOWED_BEP20, 20,
                                                    "payout-1", ALLOWLIST, dry_run=False)
        self.assertEqual(result["status"], "already_submitted")
        self.assertFalse(result["sent"])
        self.assertIsNone(result["record"])
        self.assertIn("records unavailable", result["lookup_error"])


class TestCredentialsAreNotInInstanceState(unittest.TestCase):
    NEEDLES = ("AKEY", "ASECRET", "APASS")

    def _blobs(self, payout):
        return [repr(payout), str(payout), repr(vars(payout)), f"{payout}", f"{payout!r}",
                repr(payout._http), str(payout._http), repr(vars(payout._http))]

    def test_no_credential_appears_in_any_rendering_of_the_object(self):
        payout = BitgetPayout(Secret("AKEY"), Secret("ASECRET"), Secret("APASS"),
                              account_role="main", max_withdrawal=10)
        for blob in self._blobs(payout):
            for needle in self.NEEDLES:
                self.assertNotIn(needle, blob)

    def test_plain_string_credentials_are_wrapped_too(self):
        payout = BitgetPayout("AKEY", "ASECRET", "APASS", account_role="main", max_withdrawal=10)
        for blob in self._blobs(payout):
            for needle in self.NEEDLES:
                self.assertNotIn(needle, blob)

    def test_no_credential_reaches_a_formatted_log_record(self):
        payout = BitgetPayout(Secret("AKEY"), Secret("ASECRET"), Secret("APASS"),
                              account_role="main", max_withdrawal=10)
        record = logging.LogRecord("payout", logging.ERROR, __file__, 1,
                                   "client=%r state=%r secret=%s",
                                   (payout._http, vars(payout._http), Secret("ASECRET")), None)
        formatted = logging.Formatter("%(message)s").format(record)
        for needle in self.NEEDLES:
            self.assertNotIn(needle, formatted)

    def test_credentials_still_sign_after_redaction(self):
        fake = FakeBitgetHTTP()
        payout = BitgetPayout("k", "s", "p", account_role="main", max_withdrawal=1000)
        with patch_http(fake):
            payout.withdraw_onchain("USDT", "BEP20", ALLOWED_BEP20, 20, "payout-1", ALLOWLIST, dry_run=False)
        headers = fake.posts[0]["headers"]
        self.assertEqual(headers["ACCESS-KEY"], "k")
        self.assertEqual(headers["ACCESS-PASSPHRASE"], "p")
        ts = headers["ACCESS-TIMESTAMP"]
        expected = base64.b64encode(hmac.new(
            b"s", (ts + "POST" + "/api/v2/spot/wallet/withdrawal"
                   + json.dumps(fake.posts[0]["body"], separators=(",", ":"))).encode(),
            hashlib.sha256).digest()).decode()
        self.assertEqual(headers["ACCESS-SIGN"], expected)


if __name__ == "__main__":
    unittest.main()
