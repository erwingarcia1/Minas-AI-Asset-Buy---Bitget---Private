"""Every module in the package imports, and every lazy import has a target.

Written after `guardian/triage.py` (the AI incident narrative behind
`sentinel triage --label`) was overwritten wholesale by a new module that
wanted the same name. The full suite stayed green throughout: `cmd_triage`
imports `TriageEngine` lazily, inside the function, and nothing anywhere
executed that import — so the command would have crashed at runtime, in
production, for whoever next asked for a triage narrative. The overwrite was
only noticed because `git status` said `M` where a new file should say `??`.

Two guards, because those are two different holes:

- importing every module catches a module broken outright;
- resolving every *lazy* `from X import Y` catches a name deleted from a
  module that still imports fine — which is exactly the triage case, and
  which the first guard alone would have missed.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import guardian

ROOT = Path(guardian.__file__).parent


def _all_modules() -> list[str]:
    return [
        name
        for _, name, _ in pkgutil.walk_packages(guardian.__path__, prefix="guardian.")
    ]


def test_every_module_in_the_package_imports():
    failures = []
    for name in _all_modules():
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - the report is the point
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    assert failures == [], "modules that no longer import:\n  " + "\n  ".join(failures)


def _lazy_imports() -> list[tuple[str, int, str, list[str]]]:
    """Every `from guardian.X import Y` that sits inside a function or class.

    Module-level imports are exercised by the test above the moment the module
    loads; these are the ones nothing exercises until the code path runs.
    """
    found = []
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if (isinstance(inner, ast.ImportFrom) and inner.module
                        and inner.module.startswith("guardian")):
                    found.append((
                        str(path.relative_to(ROOT.parent)), inner.lineno,
                        inner.module, [a.name for a in inner.names],
                    ))
    return found


def test_every_lazy_import_resolves():
    """The triage hole specifically: a lazily-imported name deleted from a
    module that itself still imports cleanly."""
    lazy = _lazy_imports()
    assert lazy, "expected lazy imports; if they are all gone, delete this test"

    failures = []
    for filename, lineno, module, names in lazy:
        try:
            mod = importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{filename}:{lineno}: import {module}: {exc}")
            continue
        for name in names:
            if not hasattr(mod, name):
                failures.append(
                    f"{filename}:{lineno}: `from {module} import {name}` — "
                    f"{module} no longer defines {name}"
                )
    assert failures == [], (
        "lazy imports whose target is gone (these fail at runtime, not at "
        "import time, so nothing else catches them):\n  " + "\n  ".join(failures)
    )
