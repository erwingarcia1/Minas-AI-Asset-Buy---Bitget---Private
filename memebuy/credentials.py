"""Presence check for the TWO Bitget API key sets, without reading any secret.

Bitget's two-key model is not a preference, it is the exchange's own shape: a
sub-account key is only ever granted ``uta_trade``/``uta_mgt`` and can never
withdraw, so the pipeline needs a SUB key for stage 1 (sub -> main transfer) and
a separate, IP-allowlisted MAIN key for stage 2 (the irreversible withdrawal).
See ``payout/bitget_payout.py``, which refuses the wrong role locally, before any
network call, and the endpoint docs it cites:
  https://www.bitget.com/api-doc/uta/account/transfer/Sub-Master-Transfer
  https://www.bitget.com/api-doc/spot/account/Wallet-Withdrawal

Each role's credentials live in three files whose PATHS are held in env vars
(``{PREFIX}_API_KEY_FILE`` and friends) — the *_FILE convention, so the secret
itself never lands in ``docker inspect``, /proc, or a crash report.

This module answers exactly one question: "are those three files there and
readable?"  It NEVER opens them, so nothing here can leak key material, and it
makes zero network calls.  It exists so a run that will withdraw fails at stage
0 with an actionable message instead of between the transfer and the withdrawal,
where a human has to reconcile two accounts by hand.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

ROLE_SUB = "sub"
ROLE_MAIN = "main"
ROLES = (ROLE_SUB, ROLE_MAIN)

DEFAULT_SUB_PREFIX = "BITGET_SUB"
DEFAULT_MAIN_PREFIX = "BITGET_MAIN"

# Order matters only for readable output.
ENV_SUFFIXES = ("API_KEY_FILE", "API_SECRET_FILE", "API_PASSPHRASE_FILE")

# Plain-language capability statement per role.  Kept here (not in the CLI) so
# the orchestrator's refusal messages and --check-credentials cannot drift apart.
ROLE_MAY = {
    ROLE_SUB: ("transfer USDT from the sub-account up to the main account (stage 1)",),
    ROLE_MAIN: (
        "withdraw USDT on-chain to the configured allowlisted address "
        "(stage 2 — IRREVERSIBLE)",
        "read withdrawal records (read-only status lookups)",
    ),
}
ROLE_MAY_NOT = {
    ROLE_SUB: (
        "withdraw anything: Bitget never grants a sub-account key withdraw "
        "permission, and payout/ refuses it locally before any network call",
    ),
    ROLE_MAIN: (
        "be the source of the sub -> main transfer: that call must be signed "
        "with the sub-account's own key",
    ),
}


@dataclass(frozen=True)
class CredentialFile:
    """One credential file.  ``path`` is a path, never a secret."""

    env_var: str
    path: str
    env_set: bool
    exists: bool
    readable: bool

    @property
    def ok(self) -> bool:
        return self.env_set and self.exists and self.readable

    def describe(self) -> str:
        if not self.env_set:
            return "UNSET (must hold the PATH of the secret file)"
        if not self.exists:
            # The classic misconfiguration is pasting the SECRET into the
            # variable instead of a path - in which case this "path" IS the key,
            # and echoing it publishes the credential to stdout, the CI log and
            # the run journal.  Only a value that resolves to a real file has
            # been shown to be a path, so only that one is ever printed.
            return ("set, but names no existing file: %d character(s), NOT SHOWN "
                    "(if the secret itself was pasted into the variable, this "
                    "value IS the credential)" % len(self.path))
        if not self.readable:
            return "set -> %s (NOT READABLE by this process)" % self.path
        return "set -> %s (readable)" % self.path


@dataclass(frozen=True)
class RoleCredentials:
    role: str
    prefix: str
    files: Tuple[CredentialFile, ...]

    @property
    def complete(self) -> bool:
        return bool(self.files) and all(f.ok for f in self.files)

    @property
    def missing(self) -> Tuple[str, ...]:
        return tuple(f.env_var for f in self.files if not f.ok)

    def may(self) -> Tuple[str, ...]:
        return ROLE_MAY.get(self.role, ())

    def may_not(self) -> Tuple[str, ...]:
        return ROLE_MAY_NOT.get(self.role, ())


def _inspect_file(env_var: str, source: Mapping[str, str]) -> CredentialFile:
    path = str(source.get(env_var, "") or "").strip()
    if not path:
        return CredentialFile(env_var, "", False, False, False)
    # os.path/os.access only stat the file; the contents are never opened here.
    exists = os.path.isfile(path)
    readable = exists and os.access(path, os.R_OK)
    return CredentialFile(env_var, path, True, exists, readable)


def inspect_role(
    role: str, prefix: str, env: Optional[Mapping[str, str]] = None
) -> RoleCredentials:
    """Readability of one role's three credential files.  No file is opened."""
    role_norm = str(role).strip().lower()
    if role_norm not in ROLES:
        raise ValueError("role must be one of %s, got %r" % (", ".join(ROLES), role))
    source = os.environ if env is None else env
    clean = str(prefix or "").strip()
    files = tuple(_inspect_file("%s_%s" % (clean, suffix), source) for suffix in ENV_SUFFIXES)
    return RoleCredentials(role=role_norm, prefix=clean, files=files)


def missing_report(creds: RoleCredentials) -> str:
    """One actionable line naming exactly what the operator must fix."""
    return (
        "%s-role Bitget credentials are not usable (prefix %s): %s. Each variable "
        "must hold the PATH of a readable file containing that one secret."
        % (creds.role.upper(), creds.prefix, ", ".join(
            "%s %s" % (f.env_var, f.describe()) for f in creds.files if not f.ok
        ))
    )
