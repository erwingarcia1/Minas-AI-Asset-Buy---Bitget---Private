"""Stable identities for exchange records, so each one is judged exactly once.

Every detector in the fast loop asks the same question first: *have I already
seen this record?* The answer has to come from the record itself, because the
exchange won't remember for us. Where the exchange supplies a reference ID that
is trivial. Where it doesn't, the honest options are to derive one or to drop
the record - and dropping it is how a withdrawal goes unreported (G-11) and a
fill goes unchecked (G-47).

These are pure functions with no database or exchange dependency, so both the
baseline writers and the detectors that build alert evidence can use the same
answer. They must, in fact: an identity used for storage but not for alert
dedup gets a record past the first gate only to have it silently collapsed at
the second.
"""

from __future__ import annotations

import hashlib
import json

# Fields that describe *what a withdrawal is*, as opposed to where it has got
# to. A withdrawal record mutates while it settles - `status` walks Bitget's
# pending -> pending_review -> success state machine, `txid` and `updated`
# appear partway through, and the raw `info` block carries all of that. Digest
# those and every transition reads as a new withdrawal, paging the customer
# three times for one movement.
_WITHDRAWAL_IDENTITY_FIELDS = ("currency", "amount", "address", "tag", "network", "type", "timestamp")


def _digest(parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def withdrawal_identity(record: dict) -> str:
    """A stable key for one withdrawal.

    Prefers the exchange's own reference (`id`, then `txid`). Falling back to a
    digest of the settling-invariant fields rather than giving up is the whole
    of G-11: a record with no reference used to be reported as "already seen",
    which meant it never reached the withdrawal-created or unseen-address
    detectors at all.

    Residual limitation, stated rather than hidden: two withdrawals identical
    in coin, amount, destination, network *and* millisecond timestamp collapse
    into one. With a timestamp present that is effectively impossible. With one
    missing too, it stops being impossible - and that case is the exchange
    sending us records with neither a reference nor a time, which is a format
    breakage an operator should be looking at directly.
    """
    reference = str(record.get("id") or record.get("txid") or "")
    if reference:
        return f"id:{reference}"
    return "sha256:" + _digest({f: record.get(f) for f in _WITHDRAWAL_IDENTITY_FIELDS})


# A transfer describes a movement between two internal accounts. None of these
# fields change after the fact the way a withdrawal's `status` does, so the
# whole set is safe to digest.
_TRANSFER_IDENTITY_FIELDS = ("currency", "amount", "fromAccount", "toAccount", "timestamp")


def transfer_identity(transfer: dict) -> str:
    """A stable key for one internal transfer.

    Same reasoning as `withdrawal_identity`, and the same bug: an id-less
    transfer used to be reported as "already seen", so it never reached
    `detect_new_transfer_route` - the detector whose whole job is noticing funds
    moving over a route the customer has never used. G-11 fixed this for
    withdrawals and left its twin standing."""
    reference = str(transfer.get("id") or "")
    if reference:
        return f"id:{reference}"
    return "sha256:" + _digest({f: transfer.get(f) for f in _TRANSFER_IDENTITY_FIELDS})


def fill_identity(fill: dict) -> str:
    """A stable key for one fill.

    Prefers the exchange's order id, and digests the whole record when there
    isn't one. A fill can be digested whole where a withdrawal can't: these are
    closed orders, already terminal, so there is no later state for the digest
    to move under.

    Two distinct fills collide only if every field matches, including fee and
    the raw `info` block. The cost of such a collision is bounded: identical
    fills produce identical evidence, so the alert the second would raise is
    the alert the first already raised.
    """
    order_id = str(fill.get("id") or "")
    if order_id:
        return f"id:{order_id}"
    return "sha256:" + _digest(fill)


def is_unidentifiable(record: dict) -> bool:
    """True when a withdrawal has neither an exchange reference nor a time,
    which is the one case where `withdrawal_identity` can genuinely collide
    (#67).

    Without a reference the identity is a digest of currency, amount, address,
    tag, network, type and timestamp. A timestamp alone rescues it - two
    withdrawals cannot share a millisecond. With the timestamp *also* missing,
    two separate withdrawals of the same amount to the same address are one
    record as far as this system is concerned, and the second is silently
    dropped as already seen.

    Falsy values count as absent, deliberately and to match
    `withdrawal_identity`: an exchange whose schema requires the field sends
    `""` or `0` when it has nothing, and 0 is not a plausible withdrawal time.
    Anything that disagreed with the identity function about what counts as
    present would mean this check never fires for the records that need it
    most."""
    has_reference = bool(record.get("id") or record.get("txid"))
    return not has_reference and not record.get("timestamp")


def evidence_reference(record: dict, identity: str) -> str:
    """What to put in an alert's `order_id` evidence field.

    The exchange's own reference when there is one, so operators keep seeing
    the value they can paste into a support ticket. The derived identity only
    when there isn't - not for cosmetics, but because `dispatch._dedup_key`
    falls back to the *coin* when this field is empty, which makes two
    unrelated id-less withdrawals of the same coin one alert and suppresses the
    second for a whole cooldown.
    """
    reference = record.get("id") or record.get("txid")
    return reference if reference else identity
