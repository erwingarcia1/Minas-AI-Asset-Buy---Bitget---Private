"""Bitget API-key permission tables and their interpreters: a leaf module.

Moved verbatim from api/services/exchange_client.py in v0.9.2 (PR3) so the
Guardian onboarding gate (guardian/onboarding.py) can share the platform's
confirmed table without importing a trading transport: exchange_client.py
carries the order-placing client, and nothing under guardian/ may import it
(tests/test_guardian_isolation.py). exchange_client.py re-exports every name
defined here, so its existing importers are unchanged.

This module imports nothing but the standard library, and the isolation test
keeps it that way.
"""


# Bitget reports a key's permissions in /api/v2/spot/account/info "authorities".
# Bitget reports a key's permissions as short codes in `authorities`
# (GET /api/v2/spot/account/info). Confirmed from Bitget's "Edit API key"
# screen on 2026-09-11:
#   stow = Spot -> Trade            (REQUIRED for purchases; a Read-write key)
#   stor = Spot -> Read             (a READ-ONLY key with Spot ticked: it authenticates and
#                                    reads balances, but every order fails with 40014.
#                                    Seen live on 2026-10-06; rejected since v0.7.4.1)
#   smow = Margin -> Trade          (spot margin; not needed)
#   taxw = Taxation -> Search       (read trading history; not needed)
#   p2pw = P2P -> Search            (read merchant info; not needed)
#   wtow = Wallet -> Transfer       (internal transfers between the user's own
#                                    Bitget accounts; NOT an external withdrawal)
# plus the documented words readonly / read / trade / withdraw / transfer.
# The code Bitget uses for external Withdraw is still unconfirmed, so ANY other
# code is treated as "withdrawal possible" until it has been confirmed.
BITGET_TRADE_TOKENS = {"trade", "stow"}
BITGET_READ_ONLY_TOKENS = {"stor"}      # Spot -> Read: the key is Read-only, orders are refused (40014)
BITGET_WITHDRAW_TOKENS = {"withdraw"}
# recognised, harmless for withdrawals, but more than AutoBuy needs
BITGET_EXTRA_TOKENS = {"smow", "taxw", "p2pw", "wtow", "transfer"}
BITGET_EXTRA_LABELS = {"smow": "Margin trade", "taxw": "Taxation search", "p2pw": "P2P search",
                       "wtow": "Wallet transfer (internal)", "transfer": "Transfer (internal)"}
BITGET_KNOWN_TOKENS = BITGET_TRADE_TOKENS | BITGET_READ_ONLY_TOKENS | BITGET_WITHDRAW_TOKENS | BITGET_EXTRA_TOKENS | {"readonly", "read"}


# Unified Trading Account (v3) key permissions, from /api/v3/account/info (docs read 2026-10-08):
#   permType   read-only | read-and-write
#   permissions uta_trade (trade), uta_mgt (account management/read), withdraw, copy_futures_*
UTA_TRADE_TOKENS = {"uta_trade"}
UTA_WITHDRAW_TOKENS = {"withdraw"}
UTA_KNOWN_TOKENS = UTA_TRADE_TOKENS | UTA_WITHDRAW_TOKENS | {"uta_mgt", "copy_futures_position", "copy_futures_order"}


def interpret_unified_permissions(perms: list, perm_type) -> dict:
    """{can_trade, can_withdraw, permissions_known, unconfirmed, extra, read_only} for a Unified key."""
    tokens = {p.strip().lower() for p in perms if p}
    read_only = str(perm_type or "").lower() in ("read-only", "readonly", "read_only")
    trade = bool(tokens & UTA_TRADE_TOKENS) and not read_only
    withdraw = bool(tokens & UTA_WITHDRAW_TOKENS)
    unconfirmed = sorted(tokens - UTA_KNOWN_TOKENS)
    return {"can_trade": trade, "can_withdraw": withdraw, "permissions_known": bool(tokens) or bool(perm_type),
            "unconfirmed": unconfirmed, "extra": sorted(tokens & {"copy_futures_position", "copy_futures_order"}),
            "read_only": read_only,
            # Management -> Read (uta_mgt) is REQUIRED on a Unified key: the balance reads (/api/v3/account/assets) need it
            "management_read": "uta_mgt" in tokens}


def interpret_bitget_authorities(auths: list) -> dict:
    """{can_trade, can_withdraw, permissions_known, unconfirmed, extra} from
    Bitget's authorities list.

    can_trade is True with stow/trade, else False (spot trading is required).
    can_withdraw is True with the documented "withdraw", False when every code
    is recognised and none grants withdrawal, and None ("possible" — treat as
    enabled) when the list is empty or carries codes not yet confirmed.
    ``unconfirmed`` lists those codes; ``extra`` lists recognised codes the
    platform does not need (margin, transfer, taxation/P2P search).
    """
    tokens = {a.strip().lower() for a in auths if a}
    unconfirmed = sorted(tokens - BITGET_KNOWN_TOKENS)
    extra = sorted(tokens & BITGET_EXTRA_TOKENS)
    if not tokens:
        return {"can_trade": None, "can_withdraw": None, "permissions_known": False, "unconfirmed": [], "extra": [], "read_only": False}
    trade = bool(tokens & BITGET_TRADE_TOKENS)
    withdraw = bool(tokens & BITGET_WITHDRAW_TOKENS)
    read_only = bool(tokens & BITGET_READ_ONLY_TOKENS) and not trade   # Spot -> Read without Spot -> Trade
    fully_understood = not unconfirmed
    return {
        "read_only": read_only,
        "can_trade": False if read_only else (True if trade else (False if fully_understood else None)),
        "can_withdraw": True if withdraw else (False if fully_understood else None),
        "permissions_known": trade or withdraw or fully_understood,
        "unconfirmed": unconfirmed,
        "extra": extra,
    }


# ---------------------------------------------------------------------------
# Guardian's read-only gate (v0.9.2). A monitoring key must not be able to
# place an order, withdraw, or move funds between the customer's wallets.
# Derived from the tables above so the two cannot disagree: every code the
# table calls trade, withdraw or transfer is refused here, and the confirmed
# read scopes (Spot -> Read, Taxation -> Search, P2P -> Search) are accepted.
# Guardian's own gate refused `taxw` and `p2pw` for their 'w' suffix, which
# turned away honest read-only keys.
#
# A code the table does not list falls back to Bitget's documented convention
# (an 'r' suffix is a read scope, a 'w' suffix a write scope; Phase 0 audit,
# docs/phase0-capability-audit.md), and a code that matches neither is refused
# by name. Guardian never uses a write scope, so refusing one it cannot place
# costs the customer a new key and nothing else; accepting one would make
# "we only hold read-only keys" a hope rather than a check.
GUARDIAN_BITGET_WRITE_TOKENS = frozenset(
    BITGET_TRADE_TOKENS | BITGET_WITHDRAW_TOKENS | {"smow", "wtow", "transfer"}
)
GUARDIAN_BITGET_READ_TOKENS = frozenset(BITGET_READ_ONLY_TOKENS | {"readonly", "read", "taxw", "p2pw"})
# What each refused code grants, for the log line.
_WRITE_SCOPE_KIND = {
    "trade": "trade", "stow": "spot trade", "smow": "margin trade",
    "withdraw": "withdraw", "wtow": "wallet transfer", "transfer": "transfer",
}
# Words that mark a code the table does not list as writable (Guardian's
# original gate, kept as the fallback for codes nobody has confirmed yet).
_WRITE_WORDS = ("trade", "withdraw", "transfer", "write")
_UTA_READ_ONLY_PERM_TYPES = ("read-only", "readonly", "read_only")


def _permission_tokens(value) -> list | None:
    """Permission codes as lower-cased strings. Bitget sends `authorities`
    and `permissions` as lists; Guardian's fixtures and older callers carry a
    comma- or semicolon-joined string. None for a shape nothing here reads."""
    if isinstance(value, str):
        return [t.strip().lower() for t in value.replace(";", ",").split(",") if t.strip()]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(t).strip().lower() for t in value if str(t).strip()]
    return None


def guardian_bitget_read_only(authorities, perm_type=None) -> tuple[bool, str]:
    """Is this Bitget key read-only, as Guardian defines it? Returns
    (verdict, reason); the reason is a short line for the log, never for the
    customer.

    Classic key (`perm_type` None): `authorities` must carry at least one
    code and none may be a trade, withdraw or transfer scope; an empty list,
    an unreadable field and an unrecognised code all fail CLOSED, the last
    naming the code. Unified key (`perm_type` given, from
    /api/v3/account/info): `permType` must be read-only AND the permission
    tokens must carry neither `uta_trade` nor `withdraw`.
    """
    if perm_type is not None:
        return _unified_read_only(authorities, perm_type)
    if authorities is None:
        return False, "account-info response had no 'authorities' field - cannot verify scope"
    tokens = _permission_tokens(authorities)
    if tokens is None:
        return False, f"unrecognized 'authorities' field type: {type(authorities).__name__}"
    if not tokens:
        return False, "'authorities' field was empty - cannot verify scope"
    for token in tokens:
        if token in GUARDIAN_BITGET_WRITE_TOKENS:
            return False, f"authority code '{token}' is a {_WRITE_SCOPE_KIND[token]} scope - not a read-only key"
        if token in GUARDIAN_BITGET_READ_TOKENS:
            continue
        if any(word in token for word in _WRITE_WORDS):
            return False, f"authority token '{token}' indicates non-read-only scope"
        if token.endswith("r"):
            continue
        if token.endswith("w"):
            return False, f"authority token '{token}' has Bitget's documented write-scope suffix"
        return False, f"authority token '{token}' is not a recognized read-only code - failing closed"
    return True, "every authority code is a confirmed or documented read scope"


def _unified_read_only(permissions, perm_type) -> tuple[bool, str]:
    kind = str(perm_type).strip().lower()
    if not kind:
        return False, "Unified account-info response had an empty 'permType' - cannot verify scope"
    if kind not in _UTA_READ_ONLY_PERM_TYPES:
        return False, f"Unified key permType is '{kind}' - not a read-only key"
    tokens = _permission_tokens(permissions)
    if tokens is None and permissions is not None:
        return False, f"unrecognized Unified 'permissions' field type: {type(permissions).__name__}"
    for token in tokens or []:
        if token in UTA_TRADE_TOKENS:
            return False, f"Unified key carries '{token}' (trade) - not a read-only key"
        if token in UTA_WITHDRAW_TOKENS:
            return False, f"Unified key carries '{token}' (withdraw) - not a read-only key"
    return True, "Unified key permType is read-only and carries no trade or withdraw permission"
