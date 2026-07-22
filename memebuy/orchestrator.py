"""Meme-buy stage machine: sequences the pipeline and enforces the guardrails.

    0 select candidate from the CONFIGURED ALLOWLIST (no discovery in v1)
    1 sub-account -> main-account USDT transfer
    2 on-chain withdrawal      <-- IRREVERSIBILITY BOUNDARY
    3 wait for on-chain arrival (poll the destination wallet, bounded timeout)
    4 safety gate (point-in-time, immediately before the swap)
    5 swap on BSC   /   6 swap on Solana
    7 journal outcome

Every stage is journaled before it acts and after it returns, and the kill
switch is checked before every stage — including before stage 0.

Sibling packages (payout/, chains/, safety/) are imported lazily *inside*
functions so this module imports cleanly whether or not they exist yet, and so
that an import error in one of them is a normal, journaled, fail-closed abort
instead of a crash at process start.

Expected sibling contracts:
  payout.bitget_payout.BitgetPayout(account_role="sub"|"main")
      .transfer_sub_to_main(coin, amount, client_oid, dry_run)     SUB key only
      .withdraw_onchain(coin, chain, address, amount, client_oid, allowlist, dry_run)
                                                                   MAIN key only
      .get_withdrawal_status(client_oid, start_ms, end_ms)
  chains.bsc_swap.buy_token(...)     -> {"tx_hash": ..., "amount_in": ..., ...}
  chains.solana_swap.buy_token(...)  -> {"signature": ..., "in_amount": ..., ...}
  chains.bitget_swap.buy_token(...)  -> {"backend": "bitget", ...}
  safety.gate.screen_token(chain, address, price_impact_bps=None) -> GateVerdict

TWO KEYS, TWO OBJECTS.  Stage 1 is signed with the SUB account's own key and
stage 2 with a separate, IP-allowlisted MAIN key: Bitget never grants a
sub-account key withdraw permission, so one credential cannot do both.  This
module therefore builds TWO BitgetPayout instances from two different env-var
prefixes (default BITGET_SUB / BITGET_MAIN) and never uses one where the other
belongs — payout/ refuses the wrong role locally, and so does _payout() here.
A live run checks the MAIN credentials at stage 0, before the transfer, because
discovering them missing after stage 1 leaves funds parked in the main account
and a human reconciling by hand.

SWAP BACKEND.  Per chain, `swap_backend` picks who executes the swap once the
USDT has landed in our own wallet: "native" (PancakeSwap V2 / Jupiter) or
"bitget" (Bitget Wallet SwapX).  Whichever is chosen, the callee must accept
`dry_run` as a real named parameter — see _bind_swap_kwargs, which refuses a
signature that cannot prove it honours it.

Bitget endpoints behind the payout contract:
  https://www.bitget.com/api-doc/spot/account/Wallet-Transfer
  https://www.bitget.com/api-doc/spot/account/Wallet-Withdrawal
  https://www.bitget.com/api-doc/spot/account/Get-WithdrawRecords
Bitget error 43118 (duplicate clientOid) means "already submitted": it is a
success-lookup, never a retry.

Stage 3 needs a read-only destination-wallet balance probe.  It is looked up
lazily as ``chains.<chain>_swap.get_usdt_balance(address)`` and can be injected
via ``Deps.balance_probe``.  If neither exists, arrival cannot be proven and the
run stops there: the exchange's own "success" status is not accepted as proof
that the funds are spendable on-chain.
"""

from __future__ import annotations

import inspect
import json
import logging
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional

from . import caps, credentials
from .config import (
    SWAP_BACKEND_BITGET,
    SWAP_BACKEND_NATIVE,
    SWAP_BACKENDS,
    ConfigError,
    MemeBuyConfig,
    env_arm_acknowledged,
)
from .state import DEFAULT_DB_PATH, DuplicateClientOid, MemeBuyState, StateError

log = logging.getLogger("memebuy.orchestrator")

# ---------- stages ----------

STAGE_SELECT = 0
STAGE_TRANSFER = 1
STAGE_WITHDRAW = 2  # irreversibility boundary
STAGE_ARRIVAL = 3
STAGE_GATE = 4
STAGE_SWAP_BSC = 5
STAGE_SWAP_SOLANA = 6
STAGE_OUTCOME = 7

STAGE_NAMES = {
    0: "select",
    1: "transfer_sub_to_main",
    2: "withdraw_onchain",
    3: "await_arrival",
    4: "safety_gate",
    5: "swap_bsc",
    6: "swap_solana",
    7: "outcome",
}

COIN = "USDT"
# Bitget withdrawal `chain` parameter values.
BITGET_CHAIN = {caps.CHAIN_BSC: "BEP20", caps.CHAIN_SOLANA: "SOL"}
# The exchange deducts its withdrawal fee from the amount sent, so "arrived"
# cannot mean "exactly the amount requested".
ARRIVAL_TOLERANCE = 0.95
DUPLICATE_OID_CODES = {"43118", 43118}
# Parameter names whose values are credential-bearing and must never be logged
# verbatim.  The orchestrator holds no key material, but a configured rpc_url
# usually embeds an API key.
_SECRET_PARAMS = frozenset({"rpc_url", "api_key", "key", "secret", "private_key"})


class Abort(Exception):
    """A guardrail said no.  Expected, journaled, never a stack trace."""

    def __init__(self, reason: str, stage: int, status: str = "aborted") -> None:
        super().__init__(reason)
        self.reason = reason
        self.stage = stage
        self.status = status
        self.run_id: Optional[int] = None


@dataclass
class RunResult:
    ok: bool
    status: str
    stage: int
    reason: Optional[str] = None
    run_id: Optional[int] = None
    client_oid: Optional[str] = None
    dry_run: bool = True
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Deps:
    """Injection points.  All optional; None means "lazy-import the real one".

    ``payout_sub`` and ``payout_main`` are the two Bitget keys and are NOT
    interchangeable.  ``payout`` is the legacy single-object slot: it stands in
    for both roles and exists so an in-process caller can hand over one test
    double.  Production leaves all three None and gets two real, role-checked
    BitgetPayout instances built from two different env-var prefixes.
    """

    payout: Any = None
    payout_sub: Any = None
    payout_main: Any = None
    bsc_buy: Optional[Callable[..., Mapping]] = None
    solana_buy: Optional[Callable[..., Mapping]] = None
    bitget_buy: Optional[Callable[..., Mapping]] = None
    screen_token: Optional[Callable[..., Any]] = None
    balance_probe: Optional[Callable[[str, str], float]] = None


# ---------- helpers ----------


def _is_duplicate_oid(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    if code in DUPLICATE_OID_CODES or str(code) in {"43118"}:
        return True
    return "43118" in str(exc)


def _pick(resp: Any, *keys: str) -> Optional[str]:
    if not isinstance(resp, Mapping):
        return None
    for key in keys:
        value = resp.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _safe_url(value: Any) -> Any:
    """Strip credentials from a URL before it reaches a log line.

    Operator RPC endpoints routinely carry the API key in the path or query
    (``https://host/v2/<KEY>``, ``https://host/?api-key=<KEY>``), so logging the
    configured ``rpc_url`` verbatim publishes a credential to every log sink.
    Scheme + host is all a log reader needs to identify the endpoint.
    """
    if not isinstance(value, str) or "://" not in value:
        return value
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return "<redacted-url>"
    host = parts.hostname or ""
    if parts.port:
        host = "%s:%d" % (host, parts.port)
    return "%s://%s/<redacted>" % (parts.scheme, host)


def _loggable(key: str, value: Any) -> Any:
    return _safe_url(value) if key in _SECRET_PARAMS else value


def _verdict_to_dict(verdict: Any) -> Dict[str, Any]:
    if isinstance(verdict, Mapping):
        return dict(verdict)
    to_dict = getattr(verdict, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
            if isinstance(data, Mapping):
                return dict(data)
        except Exception:  # a verdict that cannot describe itself is not a pass
            pass
    return {k: getattr(verdict, k, None) for k in ("passed", "checks", "reasons")}


class MemeBuyOrchestrator:
    """Long-lived; construct once at process start, then call run_once()."""

    def __init__(
        self,
        cfg: MemeBuyConfig,
        state: Optional[MemeBuyState] = None,
        deps: Optional[Deps] = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._cfg = cfg
        self._deps = deps or Deps()
        self._clock = clock
        self._sleep = sleep
        # ARMED is snapshotted here, once, and never re-read.  It needs BOTH the
        # config flag and the process-scoped MEME_BUY_ARMED=1 environment
        # variable, so a hot config reload (or anything that can write the
        # config file, including the webapp) cannot arm a running bot.  Arming
        # requires an explicit restart with the env var set — a deliberate,
        # logged, human act.  Disarming, by contrast, is instant: the kill
        # switch file, checked before every stage.
        self._armed = bool(cfg.armed) and env_arm_acknowledged(env)
        self._state = state or MemeBuyState(cfg.db_path or DEFAULT_DB_PATH)
        self._env = env
        # One client per role, built at most once (from_env opens key files).
        self._payouts: Dict[str, Any] = {}
        log.info(
            "meme-buy orchestrator constructed: armed=%s dry_run=%s caps=%.8g/%.8g kill_switch=%s",
            self._armed,
            self.dry_run,
            cfg.per_trade_usdt_cap,
            cfg.daily_usdt_cap,
            cfg.kill_switch_file,
        )

    # ---------- properties ----------

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def dry_run(self) -> bool:
        """True unless the bot is armed AND the config asks for live mode."""
        return (not self._armed) or bool(self._cfg.dry_run)

    # ---------- lazy sibling resolution ----------

    def _prefix_for(self, role: str) -> str:
        return (
            self._cfg.bitget_sub_prefix
            if role == credentials.ROLE_SUB
            else self._cfg.bitget_main_prefix
        )

    def _payout(self, role: str, stage: int):
        """The payout client for ONE role.  Roles are never interchanged.

        A sub key cannot withdraw and a main key must not be the transfer
        source, so handing stage 2 the stage 1 object is a credential mix-up
        that payout/ would reject at the door — but only after the transfer had
        already moved money.  Verify the role here, before the call.
        """
        cached = self._payouts.get(role)
        if cached is not None:
            return cached
        explicit = (
            self._deps.payout_sub
            if role == credentials.ROLE_SUB
            else self._deps.payout_main
        )
        if explicit is None and self._deps.payout is not None:
            explicit = self._deps.payout  # legacy single-object injection
        if explicit is not None:
            self._assert_role(explicit, role, stage)
            self._payouts[role] = explicit
            return explicit

        from payout.bitget_payout import BitgetPayout  # lazy by design

        kwargs: Dict[str, Any] = {}
        if role == credentials.ROLE_MAIN:
            # payout/ refuses to withdraw without a per-call ceiling; the
            # per-trade cap IS that ceiling, so the two cannot drift apart.
            kwargs["max_withdrawal"] = self._cfg.per_trade_usdt_cap
        prefix = self._prefix_for(role)
        creds = credentials.inspect_role(role, prefix, self._env)
        if self.dry_run and not creds.complete:
            # A simulation signs nothing, so demanding key files just to print
            # the intended call would make dry-run useless on a fresh box.  The
            # client still carries its role, so the role checks still apply.
            log.info(
                "[DRY-RUN] %s-role Bitget credentials are incomplete (prefix %s); "
                "using an unauthenticated client — nothing is signed or sent",
                role,
                prefix,
            )
            client = BitgetPayout(account_role=role, **kwargs)
        else:
            client = BitgetPayout.from_env(account_role=role, prefix=prefix, **kwargs)
        self._assert_role(client, role, stage)
        self._payouts[role] = client
        return client

    @staticmethod
    def _assert_role(client: Any, role: str, stage: int) -> None:
        """Refuse an object that says it holds the other account's key."""
        actual = getattr(client, "account_role", None)
        if actual is None:  # an injected double that does not model roles
            return
        if str(actual).strip().lower() != role:
            raise Abort(
                "stage %d needs the %s-role Bitget key but was handed a %r-role "
                "client; the two credentials are not interchangeable"
                % (stage, role, actual),
                stage,
            )

    def _screen_token(self) -> Callable[..., Any]:
        if self._deps.screen_token is not None:
            return self._deps.screen_token
        from safety.gate import screen_token  # lazy by design

        return screen_token

    def _swap_label(self, chain: str, backend: str) -> str:
        if backend == SWAP_BACKEND_BITGET:
            return "chains.bitget_swap.buy_token"
        return "chains.%s_swap.buy_token" % chain

    def _swap_fn(self, chain: str, backend: str = SWAP_BACKEND_NATIVE):
        """The configured backend's buy_token, or a clean abort.

        chains/bitget_swap.py is a separate deliverable and may simply not be
        deployed; importing it lazily turns its absence into a journaled abort
        at this stage rather than an ImportError at process start that would
        take the spot bot's own module import down with it.
        """
        stage = STAGE_SWAP_BSC if chain == caps.CHAIN_BSC else STAGE_SWAP_SOLANA
        if backend not in SWAP_BACKENDS:
            # load_meme_buy_config rejects these, but ChainConfig is public and
            # hand-constructible.  An unrecognised router must abort, never fall
            # off the end of this chain of ifs into the live native one.
            raise Abort(
                "swap_backend %r for %s is not one of %s; refusing to guess a router"
                % (backend, chain, ", ".join(SWAP_BACKENDS)),
                stage,
            )
        if backend == SWAP_BACKEND_BITGET:
            if self._deps.bitget_buy is not None:
                return self._deps.bitget_buy
            try:
                from chains.bitget_swap import buy_token  # lazy by design
            except Exception as exc:
                raise Abort(
                    "swap_backend='bitget' is configured for %s but "
                    "chains.bitget_swap is unavailable (%s); refusing to fall "
                    "back to another router" % (chain, exc),
                    stage,
                )
            return buy_token
        if chain == caps.CHAIN_BSC:
            if self._deps.bsc_buy is not None:
                return self._deps.bsc_buy
            from chains.bsc_swap import buy_token  # lazy by design

            return buy_token
        if self._deps.solana_buy is not None:
            return self._deps.solana_buy
        from chains.solana_swap import buy_token  # lazy by design

        return buy_token

    def _probe_fn(self, chain: str) -> Optional[Callable[[str, str], float]]:
        if self._deps.balance_probe is not None:
            return self._deps.balance_probe
        module = "chains.bsc_swap" if chain == caps.CHAIN_BSC else "chains.solana_swap"
        try:
            mod = __import__(module, fromlist=["get_usdt_balance"])
            fn = getattr(mod, "get_usdt_balance", None)
        except Exception:  # missing sibling -> no proof of arrival available
            return None
        if fn is None:
            return None
        return lambda _chain, address: fn(address)

    # ---------- guardrails ----------

    def _guard(self, stage: int) -> None:
        """Kill switch, before EVERY stage."""
        if caps.kill_switch_engaged(self._cfg.kill_switch_file):
            raise Abort(
                "kill switch engaged (%s) before stage %d/%s"
                % (self._cfg.kill_switch_file, stage, STAGE_NAMES.get(stage, "?")),
                stage,
                status="killed",
            )

    def _check_credentials(self) -> None:
        """Both Bitget keys must be on disk BEFORE stage 1 moves anything.

        Only a live run needs them (a dry run signs nothing), and an injected
        payout object supplies its own credentials.  Otherwise: discovering the
        MAIN key missing at stage 2 leaves the USDT sitting in the main account
        with a half-finished run behind it, so check both roles up front and say
        exactly which environment variable is wrong.
        """
        deps = self._deps
        if not self.dry_run:
            for role, injected in (
                (credentials.ROLE_SUB, deps.payout_sub or deps.payout),
                (credentials.ROLE_MAIN, deps.payout_main or deps.payout),
            ):
                if injected is not None:
                    continue
                creds = credentials.inspect_role(role, self._prefix_for(role), self._env)
                if not creds.complete:
                    raise Abort(
                        credentials.missing_report(creds)
                        + " Refusing to start: stage %d would fail after money had "
                        "already moved."
                        % (
                            STAGE_TRANSFER
                            if role == credentials.ROLE_SUB
                            else STAGE_WITHDRAW
                        ),
                        STAGE_SELECT,
                    )
        # Build and role-verify BOTH clients here, in both modes.  _payout()
        # checks the role, but stage 2 only reaches it after stage 1 has already
        # transferred: a mix-up found there is one found after money moved.  The
        # message still names the stage that needs the key; the abort is stage 0,
        # where nothing has happened.
        for role, stage in (
            (credentials.ROLE_SUB, STAGE_TRANSFER),
            (credentials.ROLE_MAIN, STAGE_WITHDRAW),
        ):
            try:
                self._payout(role, stage)
            except Abort as exc:
                raise Abort(exc.reason, STAGE_SELECT, exc.status)

    def _log_intent(self, call: str, dry: Optional[bool] = None, **params: Any) -> None:
        prefix = "[DRY-RUN] would call" if (self.dry_run if dry is None else dry) else "calling"
        log.info(
            "%s %s(%s)",
            prefix,
            call,
            ", ".join("%s=%r" % (k, _loggable(k, v)) for k, v in params.items()),
        )

    # ---------- public ----------

    def run_once(
        self,
        chain: str,
        token: str,
        usdt_amount: float,
        client_oid: Optional[str] = None,
    ) -> RunResult:
        """Execute the stage machine once.  NEVER raises.

        Requirement (g): the spot bot loop calls this inline.  Any exception in
        this subsystem — including a broken sibling import or a corrupt journal
        — must degrade to a returned RunResult, never bubble into that loop.
        """
        try:
            return self._execute(chain, token, usdt_amount, client_oid)
        except Abort as exc:
            log.warning("meme-buy abort at stage %d: %s", exc.stage, exc.reason)
            return RunResult(
                ok=False,
                status=exc.status,
                stage=exc.stage,
                reason=exc.reason,
                run_id=exc.run_id,
                client_oid=client_oid,
                dry_run=self.dry_run,
            )
        except (ConfigError, StateError) as exc:
            log.error("meme-buy configuration/journal failure: %s", exc)
            return RunResult(
                False, "failed", -1, str(exc),
                run_id=getattr(exc, "memebuy_run_id", None),
                client_oid=client_oid, dry_run=self.dry_run,
            )
        except Exception as exc:  # deliberately broad: see docstring
            log.exception("meme-buy unexpected failure (contained): %s", exc)
            return RunResult(
                False, "failed", -1, str(exc),
                run_id=getattr(exc, "memebuy_run_id", None),
                client_oid=client_oid, dry_run=self.dry_run,
            )

    # ---------- stage machine ----------

    def _execute(self, chain, token, usdt_amount, client_oid) -> RunResult:
        cfg = self._cfg
        dry = self.dry_run

        # --- stage 0: selection + all pre-spend guardrails -------------------
        self._guard(STAGE_SELECT)
        chain = str(chain or "").strip().lower()
        chain_cfg = cfg.chain(chain)  # ConfigError if unknown/disabled

        token_norm = caps.normalize_address(chain, token)
        if token_norm is None:
            raise Abort("token %r is not a valid %s address" % (token, chain), STAGE_SELECT)

        for check in (
            caps.check_token_allowlist(chain, token_norm, chain_cfg.token_allowlist),
            caps.check_per_trade_cap(usdt_amount, cfg.per_trade_usdt_cap),
            caps.check_daily_cap(
                usdt_amount, self._state.spent_last_24h(), cfg.daily_usdt_cap
            ),
            # No dynamic destinations: the address we are about to use must be
            # the configured, allowlisted one.
            caps.check_address_allowlist(
                chain_cfg.withdraw_address, chain_cfg.withdraw_allowlist
            ),
        ):
            if not check:
                raise Abort(check.reason or "guardrail denied", STAGE_SELECT)

        amount = float(usdt_amount)

        # --- persist the run (and its client_oid) before anything moves ------
        oid = client_oid or "mb-%s-%s" % (chain[:3], uuid.uuid4().hex[:16])
        existing = self._state.find_run_by_client_oid(oid)
        if existing:
            return self._resume(existing, chain_cfg, token_norm, amount)

        try:
            run_id = self._state.begin_run(oid, chain, token_norm, amount, dry_run=dry)
        except DuplicateClientOid:
            # Raced with another process between lookup and insert.
            existing = self._state.find_run_by_client_oid(oid)
            if existing is None:
                raise
            return self._resume(existing, chain_cfg, token_norm, amount)

        ctx: Dict[str, Any] = {
            "run_id": run_id,
            "chain": chain,
            "chain_cfg": chain_cfg,
            "token": token_norm,
            "amount": amount,
            "dry": dry,
            "client_oid": oid,
            "transfer_oid": "%s-t" % oid,
            "withdraw_oid": "%s-w" % oid,
            "baseline": None,
            "out": {},
        }
        self._state.journal(
            run_id,
            STAGE_SELECT,
            "begin",
            {"chain": chain, "token": token_norm, "usdt": amount, "dry_run": dry},
        )
        self._state.advance(run_id, STAGE_SELECT, status="running")
        return self._pipeline(ctx, STAGE_TRANSFER)

    def _pipeline(self, ctx: Dict[str, Any], from_stage: int) -> RunResult:
        """Run stages >= from_stage, journaling any abort against the run row."""
        try:
            return self._pipeline_inner(ctx, from_stage)
        except Abort as exc:
            exc.run_id = ctx["run_id"]
            self._safe_fail(ctx["run_id"], exc.reason, exc.stage, exc.status)
            raise
        except Exception as exc:
            self._safe_fail(ctx["run_id"], str(exc), None, "failed")
            # Carry the run id out so the caller can find the journal entry.
            setattr(exc, "memebuy_run_id", ctx["run_id"])
            raise

    def _safe_fail(self, run_id, error, stage, status) -> None:
        """Journaling must never mask the original failure."""
        try:
            self._state.fail(run_id, error, stage, status=status)
        except Exception:  # pragma: no cover - journal already broken
            log.exception("could not journal failure for run %s", run_id)

    def _pipeline_inner(self, ctx: Dict[str, Any], from_stage: int) -> RunResult:
        # Before stage 1, never between stage 1 and stage 2.
        self._check_credentials()
        if from_stage <= STAGE_TRANSFER:
            self._stage(ctx, STAGE_TRANSFER, self._stage_transfer)
        if from_stage <= STAGE_WITHDRAW:
            self._stage(ctx, STAGE_WITHDRAW, self._stage_withdraw)
        if from_stage <= STAGE_ARRIVAL:
            self._stage(ctx, STAGE_ARRIVAL, self._stage_arrival)
        # The gate runs point-in-time, immediately before the swap — not at
        # config time, not before the withdrawal.
        self._stage(ctx, STAGE_GATE, self._stage_gate)
        swap_stage = (
            STAGE_SWAP_BSC if ctx["chain"] == caps.CHAIN_BSC else STAGE_SWAP_SOLANA
        )
        self._stage(ctx, swap_stage, self._stage_swap)

        self._guard(STAGE_OUTCOME)
        self._state.journal(ctx["run_id"], STAGE_OUTCOME, "begin", None)
        self._state.complete(ctx["run_id"], STAGE_OUTCOME, error=None)
        return RunResult(
            ok=True,
            status="completed",
            stage=STAGE_OUTCOME,
            run_id=ctx["run_id"],
            client_oid=ctx["client_oid"],
            dry_run=ctx["dry"],
            detail=dict(ctx["out"]),
        )

    def _stage(self, ctx, stage: int, fn) -> None:
        """Kill switch -> journal 'begin' -> act -> journal 'end'."""
        self._guard(stage)
        run_id = ctx["run_id"]
        self._state.journal(run_id, stage, "begin", {"name": STAGE_NAMES[stage]})
        fields = fn(ctx) or {}
        self._state.advance(run_id, stage, **fields)

    def _persist_before_spend(self, ctx, stage: int, **fields) -> None:
        """Write the client_oid to disk, then count the amount, then send."""
        self._state.advance(ctx["run_id"], stage, **fields)
        self._state.mark_committed(ctx["run_id"])

    # ---------- stage 1: sub -> main ----------

    def _stage_transfer(self, ctx) -> Dict[str, Any]:
        # SUB key: the endpoint takes no fromUserId, so it can only ever move
        # this sub-account's own balance up to the main account.
        payout = self._payout(credentials.ROLE_SUB, STAGE_TRANSFER)
        oid = ctx["transfer_oid"]
        self._persist_before_spend(ctx, STAGE_TRANSFER, transfer_client_oid=oid)
        self._log_intent(
            "BitgetPayout.transfer_sub_to_main",
            dry=ctx["dry"],
            coin=COIN,
            amount=ctx["amount"],
            client_oid=oid,
            dry_run=ctx["dry"],
        )
        try:
            resp = payout.transfer_sub_to_main(
                coin=COIN, amount=ctx["amount"], client_oid=oid, dry_run=ctx["dry"]
            )
        except Exception as exc:
            if not _is_duplicate_oid(exc):
                raise
            # 43118: the exchange already accepted this exact oid.  That IS the
            # lookup answer for a transfer — accepted, do not resend.
            log.warning("transfer oid %s already submitted (43118); treating as sent", oid)
            resp = {"duplicate_client_oid": True}
        ctx["out"]["transfer"] = resp
        return {"transfer_id": _pick(resp, "transfer_id", "transferId", "id", "orderId")}

    # ---------- stage 2: on-chain withdrawal (irreversible) ----------

    def _stage_withdraw(self, ctx) -> Dict[str, Any]:
        chain_cfg = ctx["chain_cfg"]
        address = chain_cfg.withdraw_address
        check = caps.check_address_allowlist(address, chain_cfg.withdraw_allowlist)
        if not check:
            raise Abort(check.reason or "destination not allowlisted", STAGE_WITHDRAW)

        # Baseline the destination balance BEFORE the withdrawal so stage 3 can
        # prove arrival by delta rather than trusting the exchange's status.
        if not ctx["dry"]:
            ctx["baseline"] = self._read_balance(ctx["chain"], address)

        # MAIN key, from the IP-allowlisted host.  A sub key is refused by
        # payout/ outright — Bitget never grants it withdraw permission.
        payout = self._payout(credentials.ROLE_MAIN, STAGE_WITHDRAW)
        oid = ctx["withdraw_oid"]
        self._persist_before_spend(ctx, STAGE_WITHDRAW, withdraw_client_oid=oid)
        self._log_intent(
            "BitgetPayout.withdraw_onchain",
            dry=ctx["dry"],
            coin=COIN,
            chain=BITGET_CHAIN[ctx["chain"]],
            address=address,
            amount=ctx["amount"],
            client_oid=oid,
            dry_run=ctx["dry"],
        )
        try:
            resp = payout.withdraw_onchain(
                coin=COIN,
                chain=BITGET_CHAIN[ctx["chain"]],
                address=address,
                amount=ctx["amount"],
                client_oid=oid,
                allowlist=list(chain_cfg.withdraw_allowlist),
                dry_run=ctx["dry"],
            )
        except Exception as exc:
            if not _is_duplicate_oid(exc):
                raise
            log.warning("withdrawal oid %s already submitted (43118); looking it up", oid)
            resp = self._lookup_withdrawal(payout, oid)
        ctx["out"]["withdraw"] = resp
        return {
            "withdraw_id": _pick(resp, "order_id", "orderId", "withdraw_id", "id"),
            "withdraw_tx_hash": _pick(resp, "tx_hash", "txHash", "tx_id", "txId"),
        }

    # ---------- stage 3: wait for on-chain arrival ----------

    def _stage_arrival(self, ctx) -> Dict[str, Any]:
        if ctx["dry"]:
            log.info("[DRY-RUN] would poll %s wallet for %.8g USDT", ctx["chain"], ctx["amount"])
            return {}

        address = ctx["chain_cfg"].withdraw_address
        baseline = ctx["baseline"]
        if baseline is None:
            raise Abort("no balance baseline; cannot prove arrival", STAGE_ARRIVAL)
        target = baseline + ctx["amount"] * ARRIVAL_TOLERANCE
        deadline = self._clock() + self._cfg.arrival_timeout_s
        last: Optional[float] = None
        while self._clock() < deadline:
            self._guard(STAGE_ARRIVAL)  # the operator can stop us mid-wait
            try:
                last = self._read_balance(ctx["chain"], address)
            except Abort:  # a guardrail said no: never retry our way past it
                raise
            except Exception as exc:  # transient RPC error: keep polling, never assume
                log.warning("balance probe failed (retrying): %s", exc)
                last = None
            if last is not None and last >= target:
                ctx["out"]["arrival_balance"] = last
                # The exchange takes its withdrawal fee out of the amount sent,
                # so what LANDED is the ceiling on what the swap may spend.
                ctx["arrived"] = last - baseline
                ctx["out"]["arrival_delta"] = ctx["arrived"]
                return {}
            self._sleep(self._cfg.arrival_poll_interval_s)
        raise Abort(
            "funds did not arrive at %s within %.0fs (last balance %r, needed %.8g)"
            % (address, self._cfg.arrival_timeout_s, last, target),
            STAGE_ARRIVAL,
            status="arrival_timeout",
        )

    def _read_balance(self, chain: str, address: str) -> float:
        probe = self._probe_fn(chain)
        if probe is None:
            raise Abort(
                "no destination balance probe available for %s; arrival cannot be "
                "proven and the exchange's own status is not accepted as proof" % chain,
                STAGE_ARRIVAL,
            )
        value = probe(chain, address)
        number = caps._as_amount(value)
        if number is None:
            raise ValueError("balance probe returned %r" % (value,))
        return number

    # ---------- stage 4: safety gate (point-in-time) ----------

    def _gate_address(self, chain: str, token: str) -> str:
        """The form of the address safety.gate will accept.

        caps stores EVM addresses lowercased (its own canonical form), but
        ``safety.gate`` check ``02_address_format`` requires an EIP-55
        *checksummed* address and rejects all-lowercase on purpose.  Handing it
        our canonical form would fail every BSC screen — after the irreversible
        withdrawal.  Convert with safety's own helper so the two units agree; if
        it is unavailable, pass the canonical form through and let the gate
        reject it (fail-closed, never a silent pass).
        """
        if chain != caps.CHAIN_BSC:
            return token
        try:
            from safety._address import to_checksum_address  # lazy by design

            return to_checksum_address(token)
        except Exception as exc:
            log.warning("cannot checksum %s for the safety gate: %s", token, exc)
            return token

    def _stage_gate(self, ctx) -> Dict[str, Any]:
        try:
            verdict = self._screen_token()(
                ctx["chain"],
                self._gate_address(ctx["chain"], ctx["token"]),
                price_impact_bps=None,
            )
        except Exception as exc:
            # Missing module, timeout, 429, non-200 — all REJECT.
            raise Abort("safety gate unavailable: %s" % exc, STAGE_GATE, "gate_rejected")
        data = _verdict_to_dict(verdict)
        blob = json.dumps(data, default=str)[:8000]
        ctx["out"]["gate"] = data
        if data.get("passed") is not True:  # absent/None is NOT a pass
            self._state.advance(ctx["run_id"], STAGE_GATE, gate_verdict=blob)
            raise Abort(
                "safety gate REJECT: %s" % (data.get("reasons") or "no reason given"),
                STAGE_GATE,
                status="gate_rejected",
            )
        return {"gate_verdict": blob}

    # ---------- stages 5/6: swap ----------

    def _stage_swap(self, ctx) -> Dict[str, Any]:
        chain_cfg = ctx["chain_cfg"]
        stage = STAGE_SWAP_BSC if ctx["chain"] == caps.CHAIN_BSC else STAGE_SWAP_SOLANA
        backend = chain_cfg.swap_backend or SWAP_BACKEND_NATIVE
        label = self._swap_label(ctx["chain"], backend)
        buy = self._swap_fn(ctx["chain"], backend)
        amount = self._spendable(ctx, stage)
        wanted = dict(
            usdt_amount=amount,
            slippage_bps=chain_cfg.slippage_bps,
            rpc_url=chain_cfg.rpc_url,
            min_native_balance=chain_cfg.min_native_balance,
            dry_run=ctx["dry"],
        )
        if ctx["chain"] == caps.CHAIN_BSC:
            wanted["token_address"] = ctx["token"]
            # The swap spends from, and delivers to, the wallet we withdrew to.
            wanted["recipient"] = chain_cfg.withdraw_address
        else:
            wanted["token_mint"] = ctx["token"]
            wanted["mint"] = ctx["token"]
            # Passing taker explicitly means a dry run never opens the key file.
            wanted["taker"] = chain_cfg.withdraw_address
        if backend == SWAP_BACKEND_BITGET:
            # One aggregator serves both chains, so it must be told which one;
            # it is also the only backend that can sponsor gas.  The token is
            # still passed under its chain's own keyword (token_address on BSC,
            # mint on Solana) — never both, so the callee cannot pick the wrong.
            wanted["chain"] = ctx["chain"]
            wanted["gasless"] = bool(chain_cfg.gasless)
        kwargs = self._bind_swap_kwargs(buy, wanted, ctx["chain"], label)
        self._log_intent(label, dry=ctx["dry"], **kwargs)
        # A broadcast tx cannot be un-sent, and a timed-out swap may still land.
        # Record the attempt BEFORE it happens so a resume refuses to repeat it.
        self._state.advance(ctx["run_id"], stage, status="running")
        resp = buy(**kwargs)
        ctx["out"]["swap"] = resp
        return {"swap_tx_hash": _pick(resp, "tx_hash", "txHash", "signature")}

    def _spendable(self, ctx, stage: int) -> float:
        """How much USDT the swap may spend: never more than actually arrived.

        The requested amount is the pre-fee figure handed to the exchange; the
        wallet receives it minus the withdrawal fee.  Asking the router for the
        full amount either reverts (gas burnt) or, when the wallet holds unrelated
        USDT, quietly spends more than this trade was sized for.
        """
        amount = ctx["amount"]
        if ctx["dry"]:
            return amount
        arrived = ctx.get("arrived")
        if arrived is None:
            raise Abort(
                "arrived amount unknown; refusing to size a live swap from the "
                "requested amount alone",
                stage,
            )
        amount = min(amount, arrived)
        if amount <= 0:
            raise Abort("nothing arrived to swap (delta %r)" % (arrived,), stage)
        return amount

    def _bind_swap_kwargs(
        self, fn, wanted: Dict[str, Any], chain: str, label: Optional[str] = None
    ) -> Dict[str, Any]:
        """Pass only what the swap function actually accepts — or refuse.

        The chains/ package is written independently, so its exact keyword
        names may drift.  Dropping an unknown keyword silently is fine for a
        hint like `rpc_url`; dropping `dry_run` would turn a simulation into a
        real trade, so a signature that cannot take our safety-critical
        arguments is a hard abort instead of a best-effort call.  The
        orchestrator also never supplies a key or a web3 handle: key material
        is resolved inside chains/ from its *_FILE env var and must never pass
        through this call frame.

        A ``**kwargs`` tail is NOT evidence that `dry_run` is honoured — it
        accepts every keyword, including one the callee then ignores and
        broadcasts anyway.  The safety-critical names must therefore appear as
        real, named parameters no matter what the tail looks like, and a
        callable we cannot introspect at all is refused outright.

        This applies identically to every backend: swapping the configured
        router must not be a way around the dry_run proof.
        """
        stage = STAGE_SWAP_BSC if chain == caps.CHAIN_BSC else STAGE_SWAP_SOLANA
        label = label or ("chains.%s_swap.buy_token" % chain)
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError) as exc:  # C builtins / exotic callables
            raise Abort(
                "%s signature cannot be inspected (%s); its handling of dry_run "
                "cannot be proven" % (label, exc),
                stage,
            )
        named = {
            name
            for name, p in params.items()
            if p.kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        }
        for required in ("dry_run", "usdt_amount", "slippage_bps"):
            if required not in named:
                raise Abort(
                    "%s does not accept %r; refusing to call it" % (label, required),
                    stage,
                )
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            kwargs = dict(wanted)
        else:
            kwargs = {k: v for k, v in wanted.items() if k in params}
        unfilled = [
            name
            for name, p in params.items()
            if p.default is inspect.Parameter.empty
            and p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,  # unfillable by keyword at all
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
            and name not in kwargs
        ]
        if unfilled:
            raise Abort(
                "%s requires %s, which the orchestrator does not hold"
                % (label, ", ".join(unfilled)),
                stage,
            )
        return kwargs

    # ---------- resume ----------

    def _resume(
        self, row: Mapping[str, Any], chain_cfg, token: str, amount: float
    ) -> RunResult:
        """Idempotent restart: look the run up, never resend blindly."""
        run_id = int(row["id"])
        stage = int(row["stage"] or 0)
        oid = str(row["client_oid"])
        # A client_oid identifies one specific buy.  Reusing it for a different
        # target would resume the wrong run's money.
        if str(row["chain"]) != chain_cfg.name or str(row["token"]) != token:
            raise Abort(
                "client_oid %s already belongs to %s/%s; refusing to reuse it for %s/%s"
                % (oid, row["chain"], row["token"], chain_cfg.name, token),
                stage,
                status="resume_blocked",
            )
        # The mode comes from THIS process, never from the row.  Inheriting the
        # row's dry_run would let a disarmed process pick up a live run and send
        # real money (and an armed one replay a simulation for real).  Arming is
        # a restart-only decision; a run that spans two modes is an operator
        # reconciliation, not something to guess at.
        row_dry = bool(row["dry_run"])
        if row_dry != self.dry_run:
            raise Abort(
                "client_oid %s belongs to a %s run but this process is %s; refusing "
                "to resume across modes"
                % (
                    oid,
                    "dry-run" if row_dry else "live",
                    "dry-run" if self.dry_run else "live",
                ),
                stage,
                status="resume_blocked",
            )
        # A swap that was started may already be on the wire or in a block.
        # There is no per-oid swap lookup, so a repeat is a second real trade.
        if stage >= STAGE_SWAP_BSC and row["status"] != "completed":
            raise Abort(
                "a swap was already attempted for %s (stage %d); refusing to repeat "
                "it — reconcile the wallet, then clear or complete the run" % (oid, stage),
                stage,
                status="resume_blocked",
            )
        if row["status"] == "completed":
            return RunResult(
                True, "already_completed", STAGE_OUTCOME, "run already completed",
                run_id, oid, bool(row["dry_run"]),
            )

        ctx: Dict[str, Any] = {
            "run_id": run_id,
            "chain": str(row["chain"]),
            "chain_cfg": chain_cfg,
            "token": str(row["token"]),
            "amount": float(row["usdt_amount"] or amount),
            "dry": self.dry_run,
            "client_oid": oid,
            "transfer_oid": "%s-t" % oid,
            "withdraw_oid": "%s-w" % oid,
            "baseline": None,
            "out": {},
        }
        self._state.journal(run_id, stage, "resume", {"status": row["status"]})

        if stage >= STAGE_WITHDRAW and row["withdraw_client_oid"]:
            # Withdrawal records live under the MAIN key that submitted them.
            payout = self._payout(credentials.ROLE_MAIN, STAGE_WITHDRAW)
            resp = self._lookup_withdrawal(payout, str(row["withdraw_client_oid"]))
            state_str = str(_pick(resp, "status", "state") or "").lower()
            if state_str not in ("success", "successful", "finished", "completed"):
                raise Abort(
                    "withdrawal %s is %r on resume; not resending — operator must "
                    "reconcile" % (row["withdraw_client_oid"], state_str or "unknown"),
                    STAGE_WITHDRAW,
                    status="resume_blocked",
                )
            ctx["out"]["withdraw"] = resp
            if not ctx["dry"]:
                ctx["baseline"] = 0.0  # post-withdrawal: any balance >= target counts
            return self._pipeline(ctx, STAGE_ARRIVAL)

        if stage >= STAGE_TRANSFER and row["transfer_client_oid"]:
            # A transfer was submitted and we never saw the response.  There is
            # no per-oid transfer lookup in the contract, so resending could
            # double-spend: stop and hand it to a human.
            raise Abort(
                "transfer %s was already submitted; refusing to resend (reconcile "
                "manually, then clear or complete the run)" % row["transfer_client_oid"],
                STAGE_TRANSFER,
                status="resume_blocked",
            )

        # Nothing was ever sent: safe to continue from the top of the pipeline.
        return self._pipeline(ctx, STAGE_TRANSFER)

    def _lookup_withdrawal(self, payout, oid: str) -> Mapping[str, Any]:
        now_ms = int(self._clock() * 1000)
        try:
            resp = payout.get_withdrawal_status(
                client_oid=oid, start_ms=now_ms - 7 * 86400_000, end_ms=now_ms
            )
        except Exception as exc:
            raise Abort(
                "withdrawal status lookup for %s failed: %s" % (oid, exc),
                STAGE_WITHDRAW,
                status="resume_blocked",
            )
        if isinstance(resp, list):
            resp = resp[0] if resp else {}
        return resp if isinstance(resp, Mapping) else {}


# ---------- module-level entrypoint ----------


def run_once(
    cfg: MemeBuyConfig,
    chain: str,
    token: str,
    usdt_amount: float,
    state: Optional[MemeBuyState] = None,
    deps: Optional[Deps] = None,
    client_oid: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> RunResult:
    """Convenience wrapper.  Never raises.

    Production should build one MemeBuyOrchestrator at process start and reuse
    it, so the armed snapshot is taken exactly once.  This wrapper is still safe
    because arming also requires the restart-only MEME_BUY_ARMED env var.
    """
    try:
        orch = MemeBuyOrchestrator(cfg, state=state, deps=deps, env=env)
    except Exception as exc:
        log.exception("meme-buy orchestrator construction failed (contained): %s", exc)
        return RunResult(False, "failed", -1, str(exc), dry_run=True)
    return orch.run_once(chain, token, usdt_amount, client_oid=client_oid)
