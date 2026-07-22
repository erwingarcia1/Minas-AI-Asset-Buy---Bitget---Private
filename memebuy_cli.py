#!/usr/bin/env python3
"""Standalone entrypoint for the meme-buy subsystem.

THE EXISTING SPOT BOT IS UNTOUCHED.
==================================
`autobuy.py` is NOT modified by this subsystem and does NOT import it.  The
meme-buy pipeline has no hook in the running trade loop: the only way to invoke
it is to run THIS script by hand.  Deploying the meme-buy packages therefore
cannot change the behaviour of the live spot bot, which keeps trading exactly as
it did before.

Safety posture of this CLI
--------------------------
  --dry-run          default; nothing here moves money
  --status           read-only summary of config / arming / caps / journal
  --check-token      runs ONLY safety.gate.screen_token — read-only HTTP GETs,
                     no exchange calls, no signing, spends nothing
  --check-credentials  reports whether each role's three credential FILES are
                     present and readable, and what that role is allowed to do.
                     ZERO network calls, and the files are never opened, so no
                     secret value can reach the terminal or a CI log.
  --run-once         REFUSES unless BOTH of:
                       * meme_buy.armed = true in the config file, and
                       * --i-understand-this-spends-real-money on the CLI
                     and even then the orchestrator still requires the
                     restart-only MEME_BUY_ARMED=1 env var before it will leave
                     dry-run (see memebuy/config.py).  Three independent
                     inputs must agree before a single satoshi moves.

Exit codes:  0 ok / gate pass · 1 refused or gate reject · 2 usage or config error
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

DEFAULT_CONFIG = os.path.join("data", "config.json")

ACK_FLAG = "--i-understand-this-spends-real-money"

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2


# ---------- helpers ----------


def _load_config(path: str):
    """Read the config file and validate the meme_buy block.  Never partial."""
    from memebuy.config import ConfigError, load_meme_buy_config

    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        raise SystemExit("config file not found: %s" % path)
    except (OSError, ValueError) as exc:
        raise SystemExit("config file unreadable (%s): %s" % (path, exc))

    if "meme_buy" not in raw:
        raise SystemExit(
            "config %s has no 'meme_buy' block — the subsystem is not configured "
            "(this is the safe default; the spot bot is unaffected)" % path
        )
    try:
        return load_meme_buy_config(raw)
    except ConfigError as exc:
        raise SystemExit("invalid meme_buy config: %s" % exc)


def _armed_env() -> bool:
    from memebuy.config import env_arm_acknowledged

    return env_arm_acknowledged()


# ---------- subcommands ----------


def cmd_status(args) -> int:
    from memebuy import caps
    from memebuy.state import DEFAULT_DB_PATH, MemeBuyState

    cfg = _load_config(args.config)
    env_ok = _armed_env()
    would_be_live = bool(cfg.armed) and env_ok and not cfg.dry_run

    print("meme-buy status")
    print("  config file .............. %s" % args.config)
    print("  config armed ............. %s" % cfg.armed)
    print("  MEME_BUY_ARMED=1 in env .. %s" % env_ok)
    print("  config dry_run ........... %s" % cfg.dry_run)
    print("  EFFECTIVE MODE ........... %s" % ("LIVE" if would_be_live else "DRY-RUN"))
    print("  per-trade cap (USDT) ..... %.8g" % cfg.per_trade_usdt_cap)
    print("  daily cap (USDT) ......... %.8g" % cfg.daily_usdt_cap)
    print("  kill switch file ......... %s" % cfg.kill_switch_file)
    print("  kill switch engaged ...... %s" % caps.kill_switch_engaged(cfg.kill_switch_file))
    print("  bitget SUB key prefix .... %s (stage 1 transfer)" % cfg.bitget_sub_prefix)
    print("  bitget MAIN key prefix ... %s (stage 2 withdrawal)" % cfg.bitget_main_prefix)
    print("      run --check-credentials to see which files are actually present")

    for name in caps.SUPPORTED_CHAINS:
        chain_cfg = cfg.chains.get(name)
        if chain_cfg is None or not chain_cfg.enabled:
            print("  chain %-7s ........... disabled" % name)
            continue
        print("  chain %-7s ........... enabled" % name)
        print("      withdraw address ..... %s" % chain_cfg.withdraw_address)
        print("      allowlisted tokens ... %d" % len(chain_cfg.token_allowlist))
        print("      slippage (bps) ....... %d" % chain_cfg.slippage_bps)
        print("      swap backend ......... %s%s"
              % (chain_cfg.swap_backend, " (gasless)" if chain_cfg.gasless else ""))

    db_path = cfg.db_path or DEFAULT_DB_PATH
    print("  journal db ............... %s" % db_path)
    # --status is strictly read-only: never CREATE the journal as a side effect
    # of asking a question.  MemeBuyState() would happily create it.
    if not os.path.exists(db_path):
        print("  spent in last 24h (USDT) . n/a (journal does not exist yet)")
    else:
        try:
            state = MemeBuyState(db_path)
            print("  spent in last 24h (USDT) . %.8g" % state.spent_last_24h())
        except Exception as exc:  # a locked/corrupt journal must not crash --status
            print("  spent in last 24h (USDT) . unavailable (%s)" % exc)

    print("\n  the spot bot (autobuy.py) is not affected by any of the above")
    return EXIT_OK


def cmd_check_credentials(args) -> int:
    """Are the two Bitget key sets on disk?  Read-only; opens no file, no network.

    Prints PATHS and permissions only.  The credential files themselves are
    never opened here, so there is no code path by which a key, secret or
    passphrase could reach stdout, a terminal scrollback or a CI log.
    """
    from memebuy import credentials

    # Prefixes are config; if the config is missing or invalid, still answer the
    # question using the documented defaults rather than refusing outright.
    sub_prefix, main_prefix = credentials.DEFAULT_SUB_PREFIX, credentials.DEFAULT_MAIN_PREFIX
    try:
        cfg = _load_config(args.config)
    except SystemExit as exc:
        print("note: using default prefixes (%s)\n" % exc)
    else:
        sub_prefix, main_prefix = cfg.bitget_sub_prefix, cfg.bitget_main_prefix

    print("meme-buy credential check")
    print("  read-only: no network calls, no files opened, no secret values printed\n")

    all_complete = True
    for role, prefix in ((credentials.ROLE_SUB, sub_prefix), (credentials.ROLE_MAIN, main_prefix)):
        creds = credentials.inspect_role(role, prefix)
        all_complete = all_complete and creds.complete
        print("  %s-role key (prefix %s)" % (role.upper(), prefix))
        for handle in creds.files:
            print("    %-34s %s" % (handle.env_var, handle.describe()))
        print("    status ......................... %s"
              % ("READY" if creds.complete else
                 "INCOMPLETE — missing/unreadable: %s" % ", ".join(creds.missing)))
        for line in creds.may():
            print("    may ............................ %s" % line)
        for line in creds.may_not():
            print("    may NOT ........................ %s" % line)
        print("")

    print("  a live run needs BOTH: stage 1 (transfer) is signed with the SUB key,")
    print("  stage 2 (the irreversible withdrawal) with the MAIN key.")
    return EXIT_OK if all_complete else EXIT_REFUSED


def cmd_check_token(args) -> int:
    """Safety gate ONLY.  Read-only; no exchange calls, no signing, no spend."""
    from safety.gate import screen_token

    chain, address = args.check_token
    print("running safety gate only — read-only, spends nothing")
    print("  chain=%s address=%s\n" % (chain, address))

    verdict = screen_token(chain, address)
    for check in verdict.checks:
        print("  [%s] %-22s %s" % ("PASS" if check.passed else "FAIL", check.name, check.detail))
    print("\n  verdict: %s" % verdict)
    return EXIT_OK if verdict.passed else EXIT_REFUSED


def cmd_run_once(args) -> int:
    cfg = _load_config(args.config)

    # --- the refusal gate ------------------------------------------------
    if not cfg.armed:
        print(
            "REFUSING: meme_buy.armed is false in %s.\n"
            "  --run-once does nothing until the config is deliberately armed."
            % args.config,
            file=sys.stderr,
        )
        return EXIT_REFUSED
    if not args.acknowledged:
        print(
            "REFUSING: %s was not passed.\n"
            "  --run-once can move real funds; the acknowledgement flag is required."
            % ACK_FLAG,
            file=sys.stderr,
        )
        return EXIT_REFUSED
    if not (args.chain and args.token and args.amount):
        print(
            "REFUSING: --run-once requires --chain, --token and --amount.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    # ---------------------------------------------------------------------

    from memebuy.orchestrator import MemeBuyOrchestrator

    orch = MemeBuyOrchestrator(cfg)
    if not orch.armed:
        # config said armed, but the restart-only env var did not agree.
        print(
            "NOTE: MEME_BUY_ARMED=1 is not set in this process environment, so the\n"
            "      orchestrator stays in dry-run regardless of the config flag.",
            file=sys.stderr,
        )

    print("mode: %s" % ("LIVE" if not orch.dry_run else "DRY-RUN"))
    result = orch.run_once(args.chain, args.token, float(args.amount))
    print(json.dumps(result.__dict__, indent=2, default=str))
    return EXIT_OK if result.ok else EXIT_REFUSED


# ---------- argument parsing ----------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memebuy_cli.py",
        description=(
            "Manual entrypoint for the meme-buy subsystem. The spot bot "
            "(autobuy.py) does not use this and is unaffected by it."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    parser.add_argument("--verbose", action="store_true", help="debug logging")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="default; show what would happen and move nothing",
    )
    mode.add_argument("--status", action="store_true", help="read-only status summary")
    mode.add_argument(
        "--check-token",
        nargs=2,
        metavar=("CHAIN", "ADDRESS"),
        help="run ONLY the safety gate on a token; read-only, spends nothing",
    )
    mode.add_argument(
        "--check-credentials",
        action="store_true",
        help="report which Bitget credential files each role has; no network, no secrets printed",
    )
    mode.add_argument(
        "--run-once",
        action="store_true",
        help="execute the pipeline once; refuses unless armed in config AND %s" % ACK_FLAG,
    )

    parser.add_argument("--chain", help="bsc|solana (with --run-once)")
    parser.add_argument("--token", help="token/mint address (with --run-once)")
    parser.add_argument("--amount", type=float, help="USDT amount (with --run-once)")
    parser.add_argument(
        ACK_FLAG,
        dest="acknowledged",
        action="store_true",
        help="required acknowledgement for --run-once; without it --run-once refuses",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.check_credentials:
        return cmd_check_credentials(args)
    if args.check_token:
        return cmd_check_token(args)
    if args.run_once:
        return cmd_run_once(args)
    if args.status:
        return cmd_status(args)

    # default: --dry-run
    print("meme-buy CLI — dry-run (default). Nothing was moved.")
    print("  --status                      show config, arming state and caps")
    print("  --check-credentials           which Bitget key files each role has")
    print("  --check-token CHAIN ADDRESS   safety gate only (read-only)")
    print("  --run-once ...                requires armed config AND %s" % ACK_FLAG)
    print("\nthe spot bot (autobuy.py) is untouched by this subsystem")
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
