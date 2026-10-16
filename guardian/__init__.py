"""Minas AI Guardian - 24/7 exchange account-compromise monitoring.

The internal package/CLI codename is `sentinel`; the product is Minas AI
Guardian.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    # Derived from installed metadata rather than written down a second time:
    # a hand-maintained copy here is one more place to disagree with
    # pyproject.toml, which is exactly the drift G-46's version criterion is
    # about. tests/test_project_consistency.py pins the two together.
    __version__ = version("sentinel")
except PackageNotFoundError:  # running from a source tree with no install
    __version__ = "unknown"
