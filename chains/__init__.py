"""On-chain execution back-ends for the Minas auto-buy bot.

One module pair per chain (``<chain>_client.py`` = RPC/read helpers,
``<chain>_swap.py`` = the money-moving path), plus two shared modules:

* ``chains.errors``      - typed exception family for every chain
* ``chains.wallet_keys`` - file-backed signing keys (``SecretKey``)

Deliberately empty of re-exports: modules are added by independent workers
and importing them here would make the package fail to import whenever one
of them is missing or mid-edit. Always import the concrete module, e.g.
``from chains import bsc_swap``.
"""

__all__ = []
