"""Exchange gateway: the venue-agnostic buy / fill / withdraw calls the executor makes.

Moved verbatim from api/tasks/purchase_scheduler.py on 2026-09-22 (audit
Batch B3); that path re-exports everything here.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from calendar import monthrange
from datetime import datetime, timedelta
from typing import Optional, Protocol

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import NullPool

from api.config import settings
from api.database import DATABASE_URL
from api.models import APIKey, Purchase, PurchaseTransaction, User, utcnow
from api.services import bitget_market
from api.services.history import BUY_TYPES

logger = logging.getLogger("autobuy.tasks.purchases")


class ExecutionError(Exception):
    """A failed attempt; the message is stored on the transaction.

    ``stage`` says which step failed ("buy" or "withdraw"); when the buy had
    already filled, ``fill`` carries its details so they are recorded and a
    ``withdrawal_failed`` event (not ``buy_failed``) is emitted."""

    def __init__(self, message: str, stage: str = "buy", fill: Optional[dict] = None, code: Optional[str] = None):
        super().__init__(message)
        self.stage = stage
        self.fill = fill
        self.code = code

    @property
    def needs_info(self) -> bool:
        """Bitget rejected a withdrawal for missing travel-rule / identity data."""
        m = str(self).lower()
        return any(k in m for k in ("travel", "identity", "kyt", "beneficiary", "first name", "last name",
                                    "areacode", "area code", "membercode", "compliance"))


class ExchangeGateway(Protocol):
    """What the executor needs from an exchange; production uses ExchangeClientGateway,
    tests substitute a fake. Every method raises ExecutionError on venue failures."""

    def place_buy(self, account: APIKey, purchase: Purchase, client_oid: str, dry_run: bool) -> dict:
        """Send the market buy tagged with our client id. Returns {"order_id"}."""
        ...

    def fetch_fill(self, account: APIKey, purchase: Purchase, order_id: str, client_oid: str, dry_run: bool) -> dict:
        """{order_id, filled, raw_status, amount_crypto, exchange_rate, cost_usdt, fee, fee_currency}."""
        ...

    def lookup_order(self, account: APIKey, purchase: Purchase, client_oid: str) -> dict:
        """{"state": filled | open | cancelled | not_found, "fill": {...} | None} for a client id."""
        ...

    def withdraw(self, account: APIKey, purchase: Purchase, amount: float, client_oid: str, dry_run: bool) -> dict:
        """{withdrawal_id, chain, withdrawal_fee, withdrawal_fee_currency, tx_hash, raw_status}."""

    def withdrawal_status(self, account: APIKey, purchase: Purchase, wd: PurchaseTransaction) -> dict:
        """The exchange's record of a sent withdrawal: {found, raw_status, tx_hash, fee, done, ok}."""
        ...


def _client_for(account: APIKey):
    from api.routes.exchange import decrypt_value
    from api.services.exchange_client import ExchangeClient
    return ExchangeClient(decrypt_value(account.encrypted_api_key),
                          decrypt_value(account.encrypted_api_secret),
                          decrypt_value(account.encrypted_passphrase) if account.encrypted_passphrase else "",
                          exchange=account.exchange, account_mode=getattr(account, "account_mode", "unknown") or "unknown")


def tag_requirement(purchase: Purchase, chain: str) -> Optional[str]:
    """Why a withdrawal on a tag chain must NOT be sent, or None when it may.
    Refuses when the destination type was never recorded, and when the address
    is an exchange account with no memo. Self-custody sends without a memo."""
    from api.utils.chains import TAG_CHAINS, family_for_chain
    word = TAG_CHAINS.get(family_for_chain(chain), "memo")
    dtype = (purchase.wallet_destination_type or "").lower() or None
    if dtype not in ("self_custody", "exchange"):
        return (f"{chain} withdrawals need a {word}: this purchase never recorded whether the "
                f"address is self-custody or an exchange account. Edit the purchase and answer that question.")
    if dtype == "exchange" and not (purchase.wallet_memo or "").strip():
        return (f"the {purchase.crypto_symbol} address is an exchange account but no {word} is stored; "
                f"sending without it would strand the coins. Edit the purchase and add the {word}.")
    return None


class ExchangeClientGateway:
    """Buys, reads fills and withdraws through the platform's venue-agnostic ExchangeClient."""

    @staticmethod
    def _wrap(exc: Exception, stage: str) -> ExecutionError:
        from api.services.exchange_client import ExchangeClientError
        if isinstance(exc, ExecutionError):
            return exc
        if isinstance(exc, ExchangeClientError):
            return ExecutionError(f"{exc.code}: {exc}", stage=stage, code=str(exc.code))
        return ExecutionError(f"{type(exc).__name__}: {exc}", stage=stage, code=type(exc).__name__)

    def place_buy(self, account, purchase, client_oid, dry_run):
        try:
            if account.exchange == "bitget":
                # pair exists, is online, order above Bitget's minimum — from the hour-cached symbol data
                from api.services import bitget_market
                bitget_market.check_symbol(purchase.crypto_symbol, purchase.amount_usd,
                                           bitget_market.get_symbol_sync(purchase.crypto_symbol))
            client = _client_for(account)
            symbol = client.spot_symbol(purchase.crypto_symbol, "USDT")
            reference = None
            try:   # the price the fill will be judged against; best effort, never blocks the buy
                reference = float(client.get_price(symbol))
            except Exception as exc:
                logger.warning("no reference price for %s before the buy: %s", symbol, exc)
            placed = client.place_buy(symbol, purchase.amount_usd, client_oid, dry_run=dry_run)
            return {**placed, "reference_price": reference}
        except Exception as exc:
            raise self._wrap(exc, "buy") from exc

    def fetch_fill(self, account, purchase, order_id, client_oid, dry_run):
        try:
            client = _client_for(account)
            symbol = client.spot_symbol(purchase.crypto_symbol, "USDT")
            return client.fetch_fill(symbol, purchase.amount_usd, order_id, client_oid, dry_run=dry_run)
        except Exception as exc:
            raise self._wrap(exc, "fill") from exc

    def lookup_order(self, account, purchase, client_oid):
        try:
            client = _client_for(account)
            symbol = client.spot_symbol(purchase.crypto_symbol, "USDT")
            return client.lookup_order(symbol, client_oid)
        except Exception as exc:
            raise self._wrap(exc, "lookup") from exc

    def withdraw(self, account, purchase, amount, client_oid, dry_run):
        """Send the coins of a buy (`amount` = bought minus the trading fee) to
        the purchase's wallet on its chain.

        Bitget: the chain's live fee/minimum/precision (hour-cached) and
        BITGET_WITHDRAW_FEE_MODE decide the size actually sent (see
        bitget_market.withdrawal_plan): exclusive = net minus the fee, floored
        to the chain precision; inclusive = the net balance, Bitget deducts the
        fee. The minimum applies to the size sent. Before a LIVE withdrawal the
        account's available balance must cover size (+ fee when exclusive).
        Dry runs use the same real data and never call the withdrawal endpoint.
        """
        if not purchase.wallet_address:
            raise ExecutionError("no wallet address on an autobuy purchase", stage="withdraw")
        chain = purchase.blockchain_network
        if not chain:
            raise ExecutionError("no blockchain network on an autobuy purchase", stage="withdraw", code="CHAIN_MISSING")
        fee = fee_currency = None
        size, received = amount, amount
        try:
            client = _client_for(account)
            if account.exchange == "bitget":
                from api.services import bitget_market
                entry = bitget_market.find_chain(bitget_market.get_chains_sync(purchase.crypto_symbol), chain)
                if entry is None:
                    raise ExecutionError(f"Bitget lists no chain {chain!r} for {purchase.crypto_symbol}",
                                         stage="withdraw", code="CHAIN_NOT_AVAILABLE")
                if not entry["withdrawable"]:
                    raise ExecutionError(f"Bitget has {purchase.crypto_symbol} withdrawals on {chain} suspended",
                                         stage="withdraw", code="CHAIN_NOT_WITHDRAWABLE")
                if entry.get("need_tag"):
                    # Live Bitget says this chain uses a destination tag / memo. Never send to an
                    # exchange address without one, and never send when nobody said what the
                    # address is: the coins would land in a shared account with nothing linking
                    # them to the customer.
                    tag_check = tag_requirement(purchase, chain)
                    if tag_check:
                        raise ExecutionError(tag_check, stage="withdraw", code="TAG_REQUIRED")
                plan = bitget_market.withdrawal_plan(amount, entry)
                size, fee, received = plan["size"], plan["fee"], plan["received"]
                fee_currency = purchase.crypto_symbol
                minimum = entry["min_withdraw_amount"] or 0.0
                if not plan["sendable"] or size < minimum:
                    raise ExecutionError(
                        f"{size:.8f} {purchase.crypto_symbol} would be sent ({plan['mode']} fee {fee:.8f}), below "
                        f"Bitget's minimum withdrawal of {minimum:g} on {chain}", stage="withdraw", code="WITHDRAW_BELOW_MINIMUM")
                if not dry_run:
                    available = client.get_available(purchase.crypto_symbol)
                    if available + 1e-12 < plan["balance_needed"]:
                        raise ExecutionError(
                            f"available {purchase.crypto_symbol} balance {available:.8f} is below the {plan['balance_needed']:.8f} "
                            f"needed to send {size:.8f} with the {fee:.8f} fee ({plan['mode']} mode)",
                            stage="withdraw", code="INSUFFICIENT_BALANCE")
            result = client.withdraw(purchase.crypto_symbol, purchase.wallet_address, size,
                                     network=chain,
                                     client_oid=client_oid, dry_run=dry_run,
                                     travel_rule=purchase.travel_rule_dict() or None,
                                     # only tag chains carry a memo; passed only when present
                                     **({"tag": purchase.wallet_memo} if purchase.wallet_memo else {}))
        except Exception as exc:
            raise self._wrap(exc, "withdraw") from exc
        return {"withdrawal_id": result["withdrawal_id"], "chain": chain, "size": size, "expected_received": received,
                "memo": purchase.wallet_memo or None,
                "withdrawal_fee": result.get("fee", fee), "withdrawal_fee_currency": result.get("fee_currency", fee_currency),
                "tx_hash": result.get("tx_hash"), "raw_status": "simulated" if dry_run else "submitted"}


    def withdrawal_status(self, account, purchase, wd):
        """Bitget's withdrawal record for `wd` (by client id / withdrawal id),
        searched from an hour before it was sent. Raises for other venues."""
        if account.exchange != "bitget":
            raise ExecutionError(f"withdrawal tracking is not implemented for {account.exchange}",
                                 stage="withdrawal_status", code="UNSUPPORTED")
        sent = wd.executed_at or wd.created_at or utcnow()
        since_ms = int((sent - timedelta(hours=1)).timestamp() * 1000)
        until_ms = int(utcnow().timestamp() * 1000)
        try:
            return _client_for(account).withdrawal_status(purchase.crypto_symbol, wd.client_oid, wd.withdrawal_id,
                                                          since_ms, until_ms)
        except Exception as exc:
            raise self._wrap(exc, "withdrawal_status") from exc
