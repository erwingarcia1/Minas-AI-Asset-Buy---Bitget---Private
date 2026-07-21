"""Solana mint-account inspector (JSON-RPC getAccountInfo, jsonParsed).

Docs: https://solana.com/docs/rpc/http/getaccountinfo
      https://spl.solana.com/token-2022/extensions

Checks the authoritative source rather than an aggregator: mintAuthority and
freezeAuthority must BOTH be null, the account must be owned by a known token
program, and Token-2022 extensions are screened against a small ALLOWLIST --
anything unknown is dangerous by default.

Endpoint secrecy: a paid RPC URL usually embeds an API key, so it is never read
from a plain env var.  ``SOLANA_RPC_URL_FILE`` holds a PATH; the file holds the
URL; the value lives in a RedactedUrl whose repr/str is "<redacted>" and is
scrubbed out of every error message raised here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import requests

from .errors import MissingFieldError, SolanaRPCError

DEFAULT_TIMEOUT = 6.0
PUBLIC_RPC_URL = "https://api.mainnet-beta.solana.com"
RPC_URL_ENV = "SOLANA_RPC_URL_FILE"

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
KNOWN_TOKEN_PROGRAMS = (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)

# Extensions that cannot be used to seize, freeze, tax or block a holder.
# Everything NOT listed here is treated as dangerous (fail closed).
BENIGN_EXTENSIONS = frozenset({
    "metadataPointer", "tokenMetadata", "groupPointer", "groupMemberPointer",
    "tokenGroup", "tokenGroupMember", "immutableOwner",
})

_SESSION = requests.Session()
_SESSION.headers.update({"Content-Type": "application/json"})


class RedactedUrl:
    """Holds an RPC endpoint that may embed an API key.  Never prints itself."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    @property
    def value(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__


@dataclass
class MintReport:
    mint: str
    program_id: str
    mint_authority: Optional[str]
    freeze_authority: Optional[str]
    decimals: Optional[int]
    is_initialized: bool
    extensions: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)


# ---------- endpoint ----------

def rpc_url() -> RedactedUrl:
    """Read the endpoint from the file named by SOLANA_RPC_URL_FILE."""
    path = os.environ.get(RPC_URL_ENV)
    if not path:
        return RedactedUrl(PUBLIC_RPC_URL)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError as exc:
        raise SolanaRPCError("config", f"cannot read {RPC_URL_ENV}: {type(exc).__name__}") from None
    if not value:
        raise SolanaRPCError("config", f"{RPC_URL_ENV} points at an empty file")
    return RedactedUrl(value)


def _secret_parts(secret: str) -> List[str]:
    """Every substring of the endpoint that could carry the API key.

    ``requests`` never puts the WHOLE url in its exception text.  A real
    ConnectionError reads::

        HTTPSConnectionPool(host='mainnet.helius-rpc.com', port=443): Max
        retries exceeded with url: /?api-key=SUPERSECRET (Caused by ...)

    so replacing only ``secret`` leaves the key in the message -- and the gate
    copies that message into the verdict, which the orchestrator logs and
    persists.  Scrub the components too.
    """
    parts = [secret]
    try:
        split = urlsplit(secret)
    except ValueError:
        return parts
    if split.username:
        parts.append(split.username)
    if split.password:
        parts.append(split.password)
    path, query = split.path or "", split.query or ""
    if query:
        parts.append(path + "?" + query)
        parts.append(query)
        for pair in query.split("&"):
            _, _, value = pair.partition("=")
            if len(value) >= 6:
                parts.append(value)
    if path and path != "/":
        parts.append(path)
        for segment in path.split("/"):
            if len(segment) >= 6:
                parts.append(segment)
    return parts


def _scrub(text: str, secret: str) -> str:
    """Redact the endpoint and every credential-bearing piece of it."""
    if not secret:
        return text
    # Longest first, so the full url is replaced before its own substrings.
    for part in sorted(set(_secret_parts(secret)), key=len, reverse=True):
        if part:
            text = text.replace(part, "<redacted>")
    return text


# ---------- transport ----------

def _request(method: str, params: List[Any], timeout: float,
             endpoint: Optional[RedactedUrl] = None) -> Any:
    """One JSON-RPC call.  No retries; every failure mode raises."""
    url = endpoint or rpc_url()
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        resp = _SESSION.request("POST", url.value, json=body, timeout=timeout)
    except requests.RequestException as exc:
        raise SolanaRPCError("network", _scrub(f"{type(exc).__name__}: {exc}", url.value)) from None

    if resp.status_code == 429:
        raise SolanaRPCError("rate_limited", "HTTP 429 from Solana RPC", 429)
    if resp.status_code != 200:
        raise SolanaRPCError("http_error", f"HTTP {resp.status_code}", resp.status_code)

    try:
        payload = resp.json()
    except ValueError:
        raise SolanaRPCError("bad_json", "response was not JSON", resp.status_code) from None
    if not isinstance(payload, dict):
        raise SolanaRPCError("bad_json", "response was not a JSON object", resp.status_code)
    if "error" in payload:
        err = payload["error"] or {}
        raise SolanaRPCError(err.get("code", "rpc_error"),
                             _scrub(str(err.get("message", "RPC error")), url.value))
    if "result" not in payload:
        raise MissingFieldError("result", "solana-rpc")
    return payload["result"]


# ---------- public ----------

def fetch(mint: str, timeout: float = DEFAULT_TIMEOUT,
          endpoint: Optional[RedactedUrl] = None) -> MintReport:
    """getAccountInfo(jsonParsed) on the mint.  Absent keys raise, never default."""
    addr = (mint or "").strip()
    if not addr:
        raise SolanaRPCError("bad_request", "empty mint")

    result = _request("getAccountInfo", [addr, {"encoding": "jsonParsed",
                                                "commitment": "confirmed"}],
                      timeout, endpoint)
    value = (result or {}).get("value") if isinstance(result, dict) else None
    if not isinstance(value, dict):
        raise SolanaRPCError("no_data", f"mint account {addr} not found")

    program_id = value.get("owner")
    if not program_id:
        raise MissingFieldError("owner", "solana-rpc")

    parsed = ((value.get("data") or {}).get("parsed") or {}) if isinstance(value.get("data"), dict) else {}
    if parsed.get("type") != "mint":
        raise SolanaRPCError("not_a_mint", f"account {addr} is not a mint account")
    info = parsed.get("info")
    if not isinstance(info, dict):
        raise MissingFieldError("data.parsed.info", "solana-rpc")

    # jsonParsed emits these keys with a null value when the authority is
    # revoked -- so an ABSENT key means "unknown", which is a reject.
    for required in ("mintAuthority", "freezeAuthority", "isInitialized"):
        if required not in info:
            raise MissingFieldError(required, "solana-rpc")

    extensions: Dict[str, Any] = {}
    for ext in info.get("extensions") or []:
        if isinstance(ext, dict) and ext.get("extension"):
            # NO default.  ``state`` must stay absent when the node did not
            # return it, so dangerous_extensions() can tell "the RPC could not
            # parse this" apart from "the RPC says this extension is inert".
            extensions[str(ext["extension"])] = ext.get("state")

    return MintReport(
        mint=addr,
        program_id=str(program_id),
        mint_authority=info["mintAuthority"],
        freeze_authority=info["freezeAuthority"],
        decimals=info.get("decimals"),
        is_initialized=bool(info["isInitialized"]),
        extensions=extensions,
        raw=value,
    )


def dangerous_extensions(report: MintReport) -> List[str]:
    """Token-2022 extensions that let someone seize / freeze / tax / block you.

    FAIL CLOSED ON UNREADABLE STATE.  An extension whose ``state`` the node did
    not return, or returned in a shape we cannot read, is DANGEROUS.  Coercing
    it to an empty dict made ``permanentDelegate`` (can move your tokens at
    will) and ``transferHook`` (can block the sell) read as harmless -- the two
    extensions that cost you the whole position.
    """
    findings: List[str] = []
    for name, raw_state in (report.extensions or {}).items():
        if name in BENIGN_EXTENSIONS:
            continue
        if not isinstance(raw_state, dict):
            findings.append(f"{name}: state is {type(raw_state).__name__}, not a readable "
                            f"object -- cannot prove it is harmless")
            continue
        state = raw_state
        if name == "permanentDelegate":
            if "delegate" not in state:
                findings.append("permanentDelegate state does not say who the delegate is")
            elif state.get("delegate"):
                findings.append("permanentDelegate can move your tokens at will")
            continue
        if name == "transferHook":
            if "programId" not in state or "authority" not in state:
                findings.append("transferHook state is incomplete "
                                "(no programId/authority to check)")
            elif state.get("programId") or state.get("authority"):
                findings.append("transferHook can block or reroute transfers")
            continue
        if name == "transferFeeConfig":
            newer = state.get("newerTransferFee")
            bps = newer.get("transferFeeBasisPoints") if isinstance(newer, dict) else None
            if "transferFeeConfigAuthority" not in state or bps is None:
                findings.append("transferFeeConfig state is incomplete "
                                "(no authority / no fee to check)")
            elif state.get("transferFeeConfigAuthority"):
                findings.append("transferFeeConfig has a live authority (fee can be raised)")
            elif str(bps) != "0":
                findings.append(f"transferFeeConfig charges {bps} bps")
            continue
        if name == "defaultAccountState":
            if str(state.get("accountState", "")).lower() != "initialized":
                findings.append("defaultAccountState freezes new accounts by default")
            continue
        if name in ("pausable", "pausableConfig"):
            findings.append("pausable: all transfers can be halted")
            continue
        if name == "nonTransferable":
            findings.append("nonTransferable: the token can never be sold")
            continue
        findings.append(f"unrecognised Token-2022 extension {name!r}")
    return findings


__all__ = ["fetch", "dangerous_extensions", "MintReport", "RedactedUrl", "rpc_url",
           "KNOWN_TOKEN_PROGRAMS", "TOKEN_PROGRAM", "TOKEN_2022_PROGRAM",
           "BENIGN_EXTENSIONS", "PUBLIC_RPC_URL", "RPC_URL_ENV", "DEFAULT_TIMEOUT"]
