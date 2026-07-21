"""Exchange payout: move USDT off a Bitget sub-account and out on-chain.

Two hops, two different API keys — this is a Bitget constraint, not a design
choice.  A sub-account API key can only trade and manage its own assets; it
can never withdraw.  Bitget Support: "To make a withdrawal, you must first
transfer the funds to the Main Account, then proceed with the withdrawal from
there."

    1. transfer_sub_to_main()  signed with the SUB account's own key
       POST /api/v3/account/sub-master-transfer   (UTA mgt. read-write)
    2. withdraw_onchain()      signed with the MAIN account's key, from an
       IP-allowlisted host
       POST /api/v2/spot/wallet/withdrawal

Both steps default to dry_run=True and refuse anything they cannot positively
verify.  Withdrawal destinations come only from a caller-supplied allowlist;
small automated withdrawals are irreversible once submitted.
"""

from .bitget_payout import BitgetPayout, Secret
from .coin_chains import CoinChains, chain_info
from .errors import (
    DUPLICATE_CLIENT_OID,
    PayoutError,
    TransferError,
    WithdrawalBlockedError,
    WithdrawalError,
    guidance,
)

__all__ = [
    "BitgetPayout",
    "CoinChains",
    "DUPLICATE_CLIENT_OID",
    "PayoutError",
    "Secret",
    "TransferError",
    "WithdrawalBlockedError",
    "WithdrawalError",
    "chain_info",
    "guidance",
]
