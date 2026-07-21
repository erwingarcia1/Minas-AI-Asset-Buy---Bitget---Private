"""memebuy — the guarded meme-coin buy pipeline for the Minas auto-buy bot.

This package owns sequencing and the guardrails only.  The money-moving and
chain-specific work lives in sibling packages (payout/, chains/, safety/) which
are imported lazily inside functions, so importing memebuy never requires them
and never touches the network.

Safety posture, in one place:
  * dry-run is the default; live mode needs config armed=true AND the
    restart-only MEME_BUY_ARMED=1 environment variable
  * caps (per-trade + rolling 24h, persisted in SQLite) are enforced before the
    first money-moving call and survive restarts
  * withdrawal destinations come only from the config allowlist
  * the kill-switch file is checked before every stage
  * every safety check fails closed: missing data is never treated as safe
"""

from .caps import (  # noqa: F401
    CapResult,
    check_address_allowlist,
    check_daily_cap,
    check_per_trade_cap,
    check_token_allowlist,
    kill_switch_engaged,
)
from .config import (  # noqa: F401
    ChainConfig,
    ConfigError,
    MemeBuyConfig,
    env_arm_acknowledged,
    load_meme_buy_config,
)
from .orchestrator import (  # noqa: F401
    Deps,
    MemeBuyOrchestrator,
    RunResult,
    run_once,
)
from .state import MemeBuyState  # noqa: F401

__all__ = [
    "CapResult",
    "ChainConfig",
    "ConfigError",
    "Deps",
    "MemeBuyConfig",
    "MemeBuyOrchestrator",
    "MemeBuyState",
    "RunResult",
    "check_address_allowlist",
    "check_daily_cap",
    "check_per_trade_cap",
    "check_token_allowlist",
    "env_arm_acknowledged",
    "kill_switch_engaged",
    "load_meme_buy_config",
    "run_once",
]
