"""Guardian is read-only monitoring, and this file is the proof that it stays
separate from the money (CLAUDE.md, "Guardian (read-only monitoring)").

Three promises, each checked against the source rather than against the
behaviour of the code that exists today, because the risk is the module
somebody adds next year:

1. Nothing under `guardian/`, and none of the API's Guardian modules, imports
   a trading transport or a module that decides a purchase, and nothing in
   them calls an order, withdrawal or transfer method. The Bitget permission
   table they share with key verification lives in a leaf module that imports
   only the standard library.
2. The trading side never reads a `guardian_*` table, and there is no foreign
   key between the Guardian tables and `api_keys`: a monitoring key and a
   purchase key never meet in the schema.
3. The exchange clients the engine builds expose only `fetch_*` reads, and the
   platform's purchase-key verifier is never named under `guardian/`.
"""
from __future__ import annotations

import ast
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GUARDIAN_PACKAGE = ROOT / "guardian"

# The API's Guardian modules. Some are written by PR3 and later; a path that
# does not exist yet is skipped, one that exists is scanned.
API_GUARDIAN_GLOBS = (
    "api/services/guardian_*.py",
    "api/models/guardian.py",
    "api/routes/guardian.py",
    "api/schemas/guardian.py",
)

# Modules that carry a trading transport or decide what happens to a purchase.
# Matched on any dotted component, so `bitget_client`, `api.services.
# exchange_client` and `from api.services import purchase_executor` are all
# caught.
FORBIDDEN_MODULES = frozenset({
    "exchange_client", "bitget_client", "binance_client", "exchange_errors", "exchange_gateway",
    "purchase_executor", "key_verification",
})

# Method names that place an order or move money, on any object.
FORBIDDEN_CALLS = frozenset({"place_buy", "place_market_buy", "withdraw", "create_order", "transfer", "cancel_order"})

# Public methods an exchange client may expose besides `fetch_*`.
READ_SURFACE_ALLOWLIST = frozenset({"close"})

LEAF_MODULE = ROOT / "api/services/bitget_permissions.py"

# The trading side: the scheduler jobs, the executor, the gateway and every
# withdrawal module. None of them may so much as name a Guardian table.
TRADING_GLOBS = (
    "api/tasks/**/*.py",
    "api/services/purchase_executor.py",
    "api/services/exchange_gateway.py",
    "api/services/*withdraw*.py",
)


def _python_files(globs) -> list[pathlib.Path]:
    files: set[pathlib.Path] = set()
    for pattern in globs:
        files.update(p for p in ROOT.glob(pattern) if p.is_file() and "__pycache__" not in p.parts)
    return sorted(files)


def _guardian_sources() -> list[pathlib.Path]:
    package = sorted(p for p in GUARDIAN_PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)
    return package + _python_files(API_GUARDIAN_GLOBS)


def _rel(path: pathlib.Path) -> str:
    return str(path.relative_to(ROOT))


def _parse(path: pathlib.Path) -> ast.AST:
    return ast.parse(path.read_text(), filename=str(path))


def _imported_names(tree: ast.AST):
    """(lineno, dotted-components) for every import, including the ones
    inside functions: a lazy import is still an import."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name.split(".")
        elif isinstance(node, ast.ImportFrom):
            components = (node.module or "").split(".") if node.module else []
            for alias in node.names:
                yield node.lineno, components + [alias.name]


# --- the scan can see what it claims to scan -----------------------------------


def test_the_scan_sees_the_sources_it_claims_to_scan():
    """An absence is only worth something when the thing scanned is there."""
    sources = {_rel(p) for p in _guardian_sources()}
    for required in ("guardian/onboarding.py", "guardian/exchange.py", "guardian/poller.py",
                     "guardian/accounts.py", "guardian/keyreplacement.py", "api/models/guardian.py"):
        assert required in sources, f"{required} missing from the scan"
    assert len(sources) >= 40, sorted(sources)
    assert LEAF_MODULE.exists()


# --- 1. Guardian code never touches a trading module or an order method --------


def test_guardian_code_imports_no_trading_module():
    failures = []
    for path in _guardian_sources():
        for lineno, components in _imported_names(_parse(path)):
            hit = FORBIDDEN_MODULES.intersection(components)
            if hit:
                failures.append(f"{_rel(path)}:{lineno}: imports {'.'.join(components)} ({', '.join(sorted(hit))})")
    assert failures == [], "Guardian code importing a trading module:\n  " + "\n  ".join(failures)


def test_guardian_code_calls_no_order_withdrawal_or_transfer_method():
    """Attribute access, bare calls and definitions. A loop variable called
    `transfer` is data and is not flagged; `client.transfer(...)` is."""
    failures = []
    for path in _guardian_sources():
        for node in ast.walk(_parse(path)):
            name = None
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_CALLS:
                name = node.attr
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
                name = node.func.id
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FORBIDDEN_CALLS:
                name = node.name
            if name is not None:
                failures.append(f"{_rel(path)}:{node.lineno}: {name}")
    assert failures == [], "Guardian code naming an order/withdrawal/transfer method:\n  " + "\n  ".join(failures)


def test_the_shared_bitget_permission_table_is_a_leaf_module():
    """`guardian/onboarding.py` imports it, so it may import nothing that
    could lead back to the trading transport: standard library only."""
    tree = _parse(LEAF_MODULE)
    imported = sorted({components[0] for _, components in _imported_names(tree)})
    outside_stdlib = [name for name in imported if name not in sys.stdlib_module_names]
    assert outside_stdlib == [], f"bitget_permissions.py imports beyond the standard library: {outside_stdlib}"
    defined = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert {"guardian_bitget_read_only", "interpret_bitget_authorities", "interpret_unified_permissions"} <= defined


def test_verify_api_key_is_never_named_under_guardian():
    """The platform's verifier proves a key can TRADE. Nothing in the monitor
    has a reason to say its name."""
    hits = [_rel(p) for p in GUARDIAN_PACKAGE.rglob("*.py")
            if "__pycache__" not in p.parts and "verify_api_key" in p.read_text()]
    assert hits == [], hits


# --- 2. the trading side never meets a Guardian table ---------------------------


def test_trading_code_never_mentions_a_guardian_table():
    files = _python_files(TRADING_GLOBS)
    names = {_rel(p) for p in files}
    assert "api/services/purchase_executor.py" in names and "api/services/exchange_gateway.py" in names
    assert "api/tasks/scheduler.py" in names
    assert any("withdraw" in n for n in names), names
    hits = [_rel(p) for p in files if "guardian_" in p.read_text()]
    assert hits == [], f"trading code mentioning a guardian_ table or module: {hits}"


def test_no_foreign_key_between_guardian_tables_and_api_keys():
    import api.models  # noqa: F401 - registers every table on Base.metadata
    from api.database import Base

    tables = Base.metadata.tables
    guardian_tables = [t for name, t in tables.items() if name.startswith("guardian_")]
    assert len(guardian_tables) >= 19, sorted(t.name for t in guardian_tables)
    assert "api_keys" in tables

    crossings = []
    for table in guardian_tables:
        for fk in table.foreign_keys:
            if fk.column.table.name == "api_keys":
                crossings.append(f"{table.name}.{fk.parent.name} -> api_keys")
    for fk in tables["api_keys"].foreign_keys:
        if fk.column.table.name.startswith("guardian_"):
            crossings.append(f"api_keys.{fk.parent.name} -> {fk.column.table.name}")
    assert crossings == [], crossings


# --- 3. the engine's exchange clients only read -----------------------------------


def _client_classes():
    from guardian.exchange import ExchangeClient

    seen, queue = [], [ExchangeClient]
    while queue:
        cls = queue.pop()
        seen.append(cls)
        queue.extend(cls.__subclasses__())
    return seen


def test_every_public_method_of_every_exchange_client_is_a_fetch():
    classes = _client_classes()
    assert {cls.exchange_id for cls in classes} >= {"bitget", "bybit", "okx", "binance"}
    offenders = []
    for cls in classes:
        for name, member in vars(cls).items():
            if name.startswith("_"):
                continue
            if isinstance(member, (staticmethod, classmethod, property)) or callable(member):
                if not name.startswith("fetch_") and name not in READ_SURFACE_ALLOWLIST:
                    offenders.append(f"{cls.__name__}.{name}")
    assert offenders == [], f"exchange client methods that are not reads: {offenders}"


@pytest.mark.parametrize("exchange", ["bitget", "bybit", "okx", "binance"])
def test_make_client_builds_a_read_only_surface(exchange):
    """What `make_client` hands the poller has nothing to call but reads;
    no network is touched (the client is built, not used)."""
    from guardian.exchange import ExchangeClient, make_client

    client = make_client(exchange, "k", "s", "p")
    assert isinstance(client, ExchangeClient)
    public = [n for n in dir(client) if not n.startswith("_") and callable(getattr(client, n))]
    assert public, "a client with no methods is not a client"
    assert all(n.startswith("fetch_") or n in READ_SURFACE_ALLOWLIST for n in public), public
    assert FORBIDDEN_CALLS.isdisjoint(dir(client))
