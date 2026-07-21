"""Typed errors for the Bitget payout path, plus actionable guidance per code.

Error strings are quoted from Bitget's own table at
https://www.bitget.com/api-doc/common/error-code/restapi — the guidance is what
an operator actually has to DO about each one, because most payout failures are
account-state problems (IP allowlist, 24h freeze, address book, KYC) that no
amount of retrying will fix.

All three classes subclass exchange_errors.ExchangeAPIError, so the bot's
existing `except ExchangeAPIError` handlers keep working, and str() stays
"Bitget error {code}: {message}".
"""

from exchange_errors import ExchangeAPIError

# Bitget: "clientOrderId duplicate". Means ALREADY SUBMITTED, never "failed" —
# the only correct response is to look the record up, never to resend.
DUPLICATE_CLIENT_OID = "43118"

# Synthetic codes raised locally, before/without an exchange round trip.
LOCAL_CODES = ("ALLOWLIST", "SUBKEY", "CHAIN", "METADATA", "PRECHECK", "CAP", "OID", "ARGS", "SHAPE")

ERROR_GUIDANCE = {
    # --- key / host / account state ---
    "40038": ("this host's public IP is not on the API key's allowlist. The withdrawal key must be "
              "IP-bound: add the payout host's egress IP in Bitget API Management, then retry. "
              "Never widen the allowlist to 0.0.0.0/0 for a withdrawal-capable key."),
    "40052": ("Bitget freezes ALL withdrawals for 24h after any security-setting change (password, "
              "2FA, email/phone, address book, API key). Nothing is retryable until the freeze "
              "expires — do not resubmit, and do not change more settings (that restarts the 24h)."),
    "40016": "the account has no phone/Google Authenticator 2FA bound; withdrawals stay blocked until one is bound in the web UI.",
    "40021": "withdrawals are disabled for this user or this API key. Re-check the key's permissions (Withdraw must be ON) in API Management.",
    "40027": "the account's region requires KYC before withdrawing. Complete KYC in the web UI; not retryable from the API.",
    "40101": "KYC is incomplete. Complete it in the web UI; not retryable from the API.",
    # --- address book ---
    "70217": ("the withdrawal whitelist is ON and this address is not in the account's address book. "
              "Add it manually on the Bitget website (2FA required) — there is NO API to add an "
              "address, which is exactly why a leaked API key cannot invent a destination."),
    "11000": "the withdrawal address is not in the account's address book. Add it manually on the Bitget website; there is no API to add addresses.",
    "47003": "the withdrawal address is not in the account's address book. Add it manually on the Bitget website; there is no API to add addresses.",
    # --- amount shape ---
    "43114": "amount is below this chain's minWithdrawAmount. Read the live minimum from GET /api/v2/spot/public/coins; do not hardcode it.",
    "43112": "amount is smaller than the on-chain withdrawFee for this chain — the payout would net nothing. Batch up to a larger size.",
    "43124": "amount violates the chain's withdrawStep: when withdrawStep is non-zero the size must be a whole multiple of it.",
    "43125": "more than 8 decimal places. Round the size down to this chain's withdrawMinScale.",
    "43113": "the per-transaction/daily withdrawal limit was exceeded. Split the payout across days or raise the limit in the web UI.",
    "70213": "the account's 24h withdrawal limit was exceeded. Wait for the rolling window to clear or raise the limit in the web UI.",
    "70226": ("the maximum withdrawable amount is reduced by open spot orders holding the balance. "
              "Cancel or let the open orders finish, or withdraw the smaller amount Bitget reports."),
    # --- idempotency ---
    DUPLICATE_CLIENT_OID: ("this clientOid was ALREADY submitted — the money may already be moving. "
                           "Do NOT retry, and do NOT mint a new clientOid: look the record up with "
                           "get_withdrawal_status(clientOid, startMs, endMs)."),
    # --- transfer path ---
    "43127": ("the classic sub-account transfer (POST /api/v2/spot/wallet/subaccount-transfer) accepts "
              "the PARENT account's key only. Either sign it with the parent key from its allowlisted "
              "IP, or use the UTA path POST /api/v3/account/sub-master-transfer with the SUB key."),
    "40037": "the API key does not exist for this environment — check that the key/secret/passphrase triple belongs to the account you think it does.",
}


def guidance(code):
    """Operator-actionable text for a Bitget error code, or "" if unmapped."""
    return ERROR_GUIDANCE.get(str(code), "")


def annotate(code, message):
    """Bitget's own message plus what to do about it."""
    hint = guidance(code)
    return f"{message} -- {hint}" if hint else message


class PayoutError(ExchangeAPIError):
    """Base for every failure on the Bitget payout path."""

    exchange = "Bitget"


class TransferError(PayoutError):
    """Sub-account -> main-account transfer failed or was refused locally."""


class WithdrawalError(PayoutError):
    """The withdrawal request reached Bitget and Bitget rejected it."""


class WithdrawalBlockedError(WithdrawalError):
    """Refused locally: nothing was sent, no money moved.

    Raised by the safety gates (address not on the allowlist, sub-account
    credentials, unknown chain string, unreadable chain metadata, amount below
    the chain minimum, per-call cap). Catching this is always safe — by
    construction the request never left the process.
    """
