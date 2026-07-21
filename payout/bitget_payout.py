"""Bitget payout client: sub-account -> main account transfer, then on-chain withdrawal.

Signing is the repo's existing ACCESS-SIGN scheme -- this module composes
BitgetClient (bitget_client.py) rather than re-implementing HMAC, so there is
exactly one signing implementation in the codebase.

Endpoints (all verified against Bitget's docs):
  POST /api/v3/account/sub-master-transfer     sub -> main, SUB key, UTA mgt r/w, 5/s
    https://www.bitget.com/api-doc/uta/account/transfer/Sub-Master-Transfer
  POST /api/v2/spot/wallet/withdrawal          MAIN key, IP-allowlisted host, 5/s
    https://www.bitget.com/api-doc/spot/account/Wallet-Withdrawal
  GET  /api/v2/spot/wallet/withdrawal-records  startTime AND endTime both required
    https://www.bitget.com/api-doc/spot/account/Get-Withdraw-Record
  GET  /api/v3/account/withdraw-address        read-only address book, Wallet(read), 1/s
    https://www.bitget.com/api-doc/uta/account/withdrawal/Get-Withdraw-Address

TWO KEYS, ON PURPOSE. A sub-account key cannot withdraw: Bitget only grants sub
keys uta_trade/uta_mgt. Bitget Support: "To make a withdrawal, you must first
transfer the funds to the Main Account, then proceed with the withdrawal from
there." `account_role` encodes which key this instance holds; the wrong role is
refused locally, before any network call.

Every money-moving method defaults to dry_run=True and carries a caller-supplied
clientOid that MUST be persisted before the call, so a crash can never
double-spend: Bitget answers 43118 on a repeat and we turn that into a lookup.
"""

import logging
import os
import time
from decimal import Decimal, InvalidOperation

from bitget_client import BASE_URL, BitgetAPIError, BitgetClient

from .coin_chains import CoinChains
from .errors import (
    DUPLICATE_CLIENT_OID,
    TransferError,
    WithdrawalBlockedError,
    WithdrawalError,
    annotate,
)

log = logging.getLogger("payout.bitget")

SUB_MASTER_TRANSFER_PATH = "/api/v3/account/sub-master-transfer"
WITHDRAWAL_PATH = "/api/v2/spot/wallet/withdrawal"
WITHDRAWAL_RECORDS_PATH = "/api/v2/spot/wallet/withdrawal-records"
WITHDRAW_ADDRESS_PATH = "/api/v3/account/withdraw-address"

MAX_CLIENT_OID = 64  # Bitget: "clientOid, Cannot exceed 64 characters."
DUPLICATE_LOOKBACK_MS = 24 * 60 * 60 * 1000


class Secret:
    """Opaque holder for key material: repr/str are always "<redacted>".

    Key material is read from a FILE whose path lives in an env var (the
    *_FILE convention) -- never from the env var itself, which leaks into
    `docker inspect`, /proc, and crash reports.
    """

    __slots__ = ("_value",)

    def __init__(self, value):
        self._value = str(value)

    @classmethod
    def from_file(cls, path):
        with open(path, "r", encoding="utf-8") as fh:
            return cls(fh.read().strip())

    @classmethod
    def from_env_file(cls, env_var):
        """`env_var` holds a PATH; the secret is the file's contents."""
        path = os.environ.get(env_var, "").strip()
        if not path:
            raise ValueError(f"{env_var} is unset (it must hold the PATH of the secret file)")
        return cls.from_file(path)

    def reveal(self):
        return self._value

    def __bool__(self):
        return bool(self._value)

    def __repr__(self):
        return "<redacted>"

    __str__ = __repr__


def _as_secret(value):
    """Wrap key material so the plaintext never becomes a plain attribute."""
    return value if isinstance(value, Secret) else Secret("" if value is None else value)


def _credential(name):
    """Property holding one credential as a Secret, outside the visible name."""
    slot = "_" + name

    def get(self):
        held = getattr(self, slot, None)
        return held.reveal() if held is not None else ""

    def set_(self, value):
        object.__setattr__(self, slot, _as_secret(value))

    return property(get, set_, doc=f"{name}; stored as a Secret so vars()/%r cannot print it")


class _RedactedCredsClient(BitgetClient):
    """BitgetClient that keeps key material out of its instance __dict__.

    The base class assigns the triple to plain attributes, so `vars(client)`,
    `"%r" % client.__dict__`, a debugger frame dump or a crash reporter that
    serializes locals prints the secret verbatim -- which defeats Secret one
    layer below the API it protects. Here each credential lives inside a Secret
    (repr "<redacted>") and is revealed only where _sign()/_request() read it.
    """

    api_key = _credential("api_key")
    api_secret = _credential("api_secret")
    passphrase = _credential("passphrase")

    def __repr__(self):
        return f"<{type(self).__name__} base_url={self.base_url!r} credentials=<redacted>>"


def _amount(value, label, error_cls, code="ARGS"):
    """Decimal(value), or a typed error -- never a bare decimal.InvalidOperation.

    Callers catch ExchangeAPIError; an ArithmeticError escaping from a size
    check would sail straight past those handlers and out of the bot.
    """
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise error_cls(code, f"{label} {value!r} is not a number") from exc


def _size_str(amount):
    """Plain-decimal string (never 1E+2) for the wire."""
    return format(Decimal(str(amount)), "f")


def _entry_parts(entry):
    """(chain_or_None, address) for one allowlist entry.

    A plain string is an address usable on any chain (the original shape); a
    mapping may pin the entry to one chain, which is the safer way to write it:
        {"chain": "BEP20", "address": "0x..."}
    """
    if isinstance(entry, dict):
        return (str(entry.get("chain") or "").strip().upper() or None,
                str(entry.get("address") or "").strip())
    return None, str(entry or "").strip()


def _in_allowlist(address, allowlist, chain=None):
    """Exact match; 0x-prefixed EVM addresses compare case-insensitively.

    An entry that names a `chain` matches only a withdrawal on THAT chain:
    paying a BEP20 address out over SOL burns the funds, and the exchange's own
    address parsing is not a line of defence we get to rely on.
    """
    addr = str(address or "").strip()
    if not addr or not allowlist or isinstance(allowlist, (str, bytes)):
        # A bare string would be iterated character by character and "compare"
        # nothing; refuse rather than pretend it is a one-entry allowlist.
        return False
    want_chain = str(chain or "").strip().upper()
    for entry in allowlist:
        entry_chain, candidate = _entry_parts(entry)
        if not candidate or (entry_chain and entry_chain != want_chain):
            continue
        if addr == candidate:
            return True
        # EIP-55 is a checksum, not part of the address, so 0x... compares
        # case-insensitively. base58 (Solana) and bech32 do not.
        if addr.lower().startswith("0x") and addr.lower() == candidate.lower():
            return True
    return False


def _record_list(payload):
    """The records array out of whatever shape the endpoint hands back.

    v2 answers with a bare list. A wrapper object or a single record would
    otherwise be iterated element by element and die on .get with an
    AttributeError -- an untyped crash in the middle of the 43118 handler,
    exactly where the caller most needs a clean answer.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("resultList", "list", "data"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        return [payload] if "clientOid" in payload else []
    raise WithdrawalError("SHAPE", f"{WITHDRAWAL_RECORDS_PATH} returned {type(payload).__name__}, "
                                   "expected a list of records")


class BitgetPayout:
    """Payout half of the Bitget integration. One instance == one API key.

    account_role="sub"  -> may transfer to main, may NEVER withdraw
    account_role="main" -> may withdraw, may NEVER be the transfer source

    max_withdrawal is a hard per-call ceiling in coin units and is REQUIRED
    before any withdrawal (the rolling 24h cap is the orchestrator's job and
    must be persisted in SQLite so a restart cannot reset it).
    """

    def __init__(self, api_key="", api_secret="", passphrase="", account_role="sub",
                 base_url=BASE_URL, timeout=15, max_withdrawal=None, coin_chains=None):
        role = str(account_role).strip().lower()
        if role not in ("sub", "main"):
            raise ValueError("account_role must be 'sub' or 'main'")
        self.account_role = role
        self.max_withdrawal = max_withdrawal
        # Credentials go straight into the shared signed client as Secrets and
        # are never stored on self, logged, or repeated as arguments.
        self._http = _RedactedCredsClient(
            api_key=_as_secret(api_key),
            api_secret=_as_secret(api_secret),
            passphrase=_as_secret(passphrase),
            base_url=base_url,
            timeout=timeout,
        )
        self.chains = coin_chains or CoinChains(base_url=base_url, timeout=timeout, session=self._http.session)

    @classmethod
    def from_env(cls, account_role, prefix="BITGET_MAIN", **kwargs):
        """Build from {PREFIX}_API_KEY_FILE / _API_SECRET_FILE / _API_PASSPHRASE_FILE."""
        return cls(
            api_key=Secret.from_env_file(f"{prefix}_API_KEY_FILE"),
            api_secret=Secret.from_env_file(f"{prefix}_API_SECRET_FILE"),
            passphrase=Secret.from_env_file(f"{prefix}_API_PASSPHRASE_FILE"),
            account_role=account_role,
            **kwargs,
        )

    def __repr__(self):
        return f"<BitgetPayout role={self.account_role} key=<redacted>>"

    # ---------- internals ----------

    def _request(self, method, path, params=None, body=None, error_cls=WithdrawalError):
        """Signed request; business-code failures become typed, annotated errors."""
        try:
            return self._http._request(method, path, params=params, body=body)
        except BitgetAPIError as exc:
            raise error_cls(exc.code, annotate(exc.code, exc.message), exc.http_status) from exc

    @staticmethod
    def _check_client_oid(client_oid, error_cls):
        oid = str(client_oid or "").strip()
        if not oid or len(oid) > MAX_CLIENT_OID or any(c.isspace() for c in oid):
            raise error_cls("OID", f"client_oid must be a non-empty token of <= {MAX_CLIENT_OID} chars "
                                   f"(persisted BEFORE the call so a crash cannot double-spend); got {client_oid!r}")
        return oid

    @staticmethod
    def _plan(method, path, body=None, params=None, **extra):
        plan = {"method": method, "endpoint": path, "body": body, "params": params}
        plan.update(extra)
        return plan

    # ---------- sub -> main ----------

    def transfer_sub_to_main(self, coin, amount, client_oid, dry_run=True,
                             from_type="spot", to_type="spot"):
        """Move `amount` of `coin` from this SUB account up to the main account.

        POST /api/v3/account/sub-master-transfer, signed with the SUB account's
        OWN key (UTA mgt. read-write). Direction is fixed sub->master: the
        endpoint takes no fromUserId/toUserId, which is why it cannot be aimed
        anywhere else. Rate limit 5/s/UID.

        The classic alternative, POST /api/v2/spot/wallet/subaccount-transfer,
        needs the PARENT key from its allowlisted IP and answers 43127 if a sub
        key tries it -- that is why this module prefers the UTA path.

        dry_run=True (default) logs the exact call and sends nothing.
        """
        if self.account_role != "sub":
            raise TransferError("SUBKEY", "sub->main transfer must be signed with the SUB account's own key; "
                                          f"this instance holds a {self.account_role!r} key")
        oid = self._check_client_oid(client_oid, TransferError)
        if _amount(amount, "transfer amount", TransferError) <= 0:
            raise TransferError("ARGS", f"transfer amount must be positive, got {amount!r}")

        body = {
            "fromType": from_type,
            "toType": to_type,
            "amount": _size_str(amount),
            "coin": str(coin).upper(),
            "clientOid": oid,
        }
        plan = self._plan("POST", SUB_MASTER_TRANSFER_PATH, body=body)

        if dry_run:
            log.info("DRY RUN transfer sub->main: POST %s %s", SUB_MASTER_TRANSFER_PATH, body)
            return dict(plan, dry_run=True, sent=False, status="simulated", clientOid=oid)

        log.warning("LIVE transfer sub->main: %s %s (clientOid=%s)", body["amount"], body["coin"], oid)
        try:
            data = self._request("POST", SUB_MASTER_TRANSFER_PATH, body=body, error_cls=TransferError)
        except TransferError as exc:
            if exc.code != DUPLICATE_CLIENT_OID:
                raise
            # Already submitted. Reporting failure here would invite a resend.
            log.warning("transfer clientOid=%s already submitted (43118); treating as submitted", oid)
            return dict(plan, dry_run=False, sent=False, status="already_submitted", clientOid=oid, data=None)
        return dict(plan, dry_run=False, sent=True, status="submitted", clientOid=oid, data=data)

    # ---------- withdrawal ----------

    def withdraw_onchain(self, coin, chain, address, amount, client_oid, allowlist, dry_run=True):
        """Withdraw `amount` of `coin` on `chain` to `address`. IRREVERSIBLE.

        *** A submitted withdrawal CANNOT BE CANCELLED. *** Small automated
        withdrawals skip Bitget's manual review queue and go straight to the
        chain; Cancel Withdrawal only ever applies to requests still sitting in
        that queue. There is no undo, no support reversal, no chargeback. Treat
        a live call here as spending the money.

        POST /api/v2/spot/wallet/withdrawal with the MAIN account key, from an
        IP-allowlisted host (else 40038). Rate limit 5/s/UID.

        Gates, in order -- the first two run before ANY network call:
          1. `address` must be in `allowlist` (config-supplied; never a runtime
             or user-supplied address) FOR THIS CHAIN, else
             WithdrawalBlockedError. An allowlist entry written as
             {"chain": "BEP20", "address": "0x..."} is honoured only on BEP20;
             a bare-string entry is honoured on any chain.
          2. account_role must be "main"; a sub key is refused outright.
          3. per-call max_withdrawal ceiling must be configured and respected.
          4. chain string must exist in Bitget's live chain table, the address
             encoding must match that chain, the chain must need no memo/tag
             (this body carries none), and the size must satisfy
             minWithdrawAmount / withdrawFee / withdrawMinScale / withdrawStep,
             with the chain withdrawable and not congested.
        Then, and only then, dry_run=True logs the exact call and sends nothing.

        A 43118 (duplicate clientOid) is NOT a failure: it means this payout was
        already submitted, so the record is looked up and returned.
        """
        # 1. destination -- hardest gate, first, before anything can leave the process.
        if not _in_allowlist(address, allowlist, chain):
            entries = [] if isinstance(allowlist, (str, bytes)) else list(allowlist or [])
            shape = " (the allowlist must be a list, not a bare string)" if isinstance(allowlist, (str, bytes)) else ""
            raise WithdrawalBlockedError(
                "ALLOWLIST",
                f"destination {address!r} is not in the configured withdrawal allowlist for chain "
                f"{str(chain).upper()!r} ({len(entries)} entr{'y' if len(entries) == 1 else 'ies'}){shape}; "
                "withdrawal addresses may only come from static config, never from runtime data. Nothing was sent.",
            )
        # 2. wrong key entirely.
        if self.account_role != "main":
            raise WithdrawalBlockedError(
                "SUBKEY",
                "a Bitget SUB-ACCOUNT API key can never withdraw (sub keys only get uta_trade/uta_mgt). "
                "Two-key architecture: transfer_sub_to_main() with the SUB key first, then "
                "withdraw_onchain() with the MAIN key from the IP-allowlisted host. Nothing was sent.",
            )
        oid = self._check_client_oid(client_oid, WithdrawalError)
        # 3. per-call ceiling: unset means unbounded, which fails open, so refuse.
        if self.max_withdrawal is None:
            raise WithdrawalBlockedError("CAP", "max_withdrawal is not configured; refusing to withdraw without a "
                                                "per-call ceiling (the rolling 24h cap belongs in the orchestrator's "
                                                "persisted ledger)")
        ceiling = _amount(self.max_withdrawal, "max_withdrawal", WithdrawalBlockedError, "CAP")
        if ceiling <= 0:
            raise WithdrawalBlockedError("CAP", f"max_withdrawal {self.max_withdrawal!r} is not a positive ceiling")
        if _amount(amount, "size", WithdrawalBlockedError) > ceiling:
            raise WithdrawalBlockedError("CAP", f"size {amount} exceeds the per-call ceiling {self.max_withdrawal}")

        # 4. live chain rules (read-only public endpoint; still refuses on anything unknown).
        info = self.chains.chain_info(coin, chain)
        reason = (self.chains.address_rejection_reason(info, chain, address)
                  or self.chains.amount_rejection_reason(coin, chain, amount))
        if reason:
            raise WithdrawalBlockedError("PRECHECK", f"{reason}. Nothing was sent.")

        body = {
            "coin": str(coin).upper(),
            "transferType": "on_chain",
            "chain": info.get("chain") or chain,  # Bitget's own spelling: "BEP20" / "SOL"
            "address": str(address).strip(),
            "size": _size_str(amount),
            "clientOid": oid,
        }
        plan = self._plan("POST", WITHDRAWAL_PATH, body=body,
                          fee=info.get("withdrawFee"), minWithdrawAmount=info.get("minWithdrawAmount"))

        if dry_run:
            log.info("DRY RUN withdrawal: POST %s %s (fee=%s)", WITHDRAWAL_PATH, body, info.get("withdrawFee"))
            return dict(plan, dry_run=True, sent=False, status="simulated", clientOid=oid)

        log.warning("LIVE IRREVERSIBLE withdrawal: %s %s -> %s on %s (clientOid=%s)",
                    body["size"], body["coin"], body["address"], body["chain"], oid)
        try:
            data = self._request("POST", WITHDRAWAL_PATH, body=body, error_cls=WithdrawalError)
        except WithdrawalError as exc:
            if exc.code != DUPLICATE_CLIENT_OID:
                raise
            # ALREADY SUBMITTED -- the money may already be on the chain. The
            # follow-up lookup is a convenience; if IT fails we must still
            # report "already_submitted", because surfacing a plain error here
            # is what makes an orchestrator mint a fresh clientOid and pay
            # twice. Broad except on purpose: nothing this lookup can do
            # changes the fact that the withdrawal was accepted.
            now = int(time.time() * 1000)
            record, lookup_error = None, None
            try:
                record = self.get_withdrawal_status(oid, now - DUPLICATE_LOOKBACK_MS, now + 60_000)
            except Exception as lookup_exc:  # noqa: BLE001 -- see comment above
                lookup_error = str(lookup_exc)
                log.error("withdrawal clientOid=%s was already submitted (43118) but the record lookup failed "
                          "(%s); DO NOT RESEND and DO NOT re-mint the clientOid -- reconcile by hand",
                          oid, lookup_error)
            else:
                log.warning("withdrawal clientOid=%s already submitted (43118); record=%s", oid, record)
            return dict(plan, dry_run=False, sent=False, status="already_submitted", clientOid=oid,
                        record=record, lookup_error=lookup_error)
        return dict(plan, dry_run=False, sent=True, status="submitted", clientOid=oid, data=data)

    # ---------- read-only ----------

    def get_withdrawal_status(self, client_oid, start_ms, end_ms):
        """Withdrawal record for `client_oid`, or None.

        GET /api/v2/spot/wallet/withdrawal-records. BOTH startTime and endTime
        are mandatory (Unix ms) even when looking up a single clientOid. On a
        successful on-chain withdrawal `tradeId` is the on-chain tx hash
        ("when dest is on_chain, it's the on chain hash value"); `status` is
        pending / fail / success.
        """
        oid = self._check_client_oid(client_oid, WithdrawalError)
        try:
            start, end = int(start_ms), int(end_ms)
        except (TypeError, ValueError):
            raise WithdrawalError("ARGS", "start_ms and end_ms must be Unix millisecond integers (both required)")
        if start <= 0 or end <= start:
            raise WithdrawalError("ARGS", f"invalid window start_ms={start} end_ms={end}; both are required and start < end")

        params = {"clientOid": oid, "startTime": str(start), "endTime": str(end)}
        payload = self._request("GET", WITHDRAWAL_RECORDS_PATH, params=params, error_cls=WithdrawalError)
        # Filter again locally: never trust the server to have honoured the filter.
        for record in _record_list(payload):
            if str(record.get("clientOid", "")) == oid:
                return record
        return None

    def list_whitelisted_addresses(self, coin=None, limit=None):
        """The account's withdrawal address book (GET /api/v3/account/withdraw-address).

        READ-ONLY BY DESIGN: Bitget exposes no API to ADD an address. New
        destinations can only be added by a human in the web UI with 2FA (and
        doing so triggers the 24h withdrawal freeze, error 40052). That asymmetry
        is a security feature -- a stolen API key can move funds only to
        addresses a human already approved, so it is worth cross-checking the
        local config allowlist against this list before going live.

        Permission: Wallet (read). Rate limit 1/s/UID.
        """
        params = {}
        if coin:
            params["coin"] = str(coin).upper()
        if limit:
            params["limit"] = str(limit)
        data = self._request("GET", WITHDRAW_ADDRESS_PATH, params=params or None, error_cls=WithdrawalError)
        if isinstance(data, dict):
            return data.get("addressList") or []
        return data or []
