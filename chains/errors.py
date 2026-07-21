"""Typed errors for the on-chain (DEX) back-ends.

Mirrors the CEX convention in ``exchange_errors.py``: every error carries
``(code, message, http_status=None)``, a class attribute naming the venue,
and stringifies as ``"{exchange} error {code}: {message}"``. Here the venue
is the chain/router rather than an exchange, and ``code`` is an uppercase
slug (there is no HTTP business-code to echo), e.g.::

    raise InsufficientGas("BNB_FLOOR", "0.0004 BNB < floor 0.0030 BNB")
    # -> "bsc error BNB_FLOOR: 0.0004 BNB < floor 0.0030 BNB"

``http_status`` is kept only so callers can catch chain and exchange errors
with the same handler; RPC transports may populate it (429 from a public
node), otherwise it stays ``None``.

Hierarchy (all catchable as ``ChainError``)::

    ChainError
      +- SwapReverted     the node/router told us the tx would revert, or it did
      +- InsufficientGas  not enough native coin to pay for gas
      +- QuoteStale       we could not get a usable quote / route

Shared by every chain module in this package.
"""

try:  # the repo is flat, so this normally resolves
    from exchange_errors import ExchangeAPIError as _BaseError
except ImportError:  # pragma: no cover - stand-alone / partial checkouts
    class _BaseError(Exception):
        """Fallback with the same shape as exchange_errors.ExchangeAPIError."""

        exchange = "exchange"

        def __init__(self, code, message, http_status=None):
            super().__init__(message)
            self.code = code
            self.message = message
            self.http_status = http_status

        def __str__(self):
            return f"{self.exchange} error {self.code}: {self.message}"


# ---------- public ----------

class ChainError(_BaseError):
    """Base for anything that goes wrong talking to a chain.

    Subclasses (or callers) override ``exchange`` per chain, e.g. the BSC
    modules set it to ``"bsc"``; the Solana modules set ``"solana"``.
    """

    exchange = "chain"

    def __init__(self, code, message, http_status=None):
        # Not delegating to super() unconditionally: the real ExchangeAPIError
        # already stores these three, but re-assigning is harmless and keeps
        # the attributes present under the fallback base too.
        super().__init__(code, message, http_status)
        self.code = code
        self.message = message
        self.http_status = http_status

    def __str__(self):
        return f"{self.exchange} error {self.code}: {self.message}"


class SwapReverted(ChainError):
    """The swap (or approve) reverted, or a pre-flight said it would.

    Raised by the ``estimate_gas`` pre-flight *before* anything is broadcast,
    and again if a mined receipt comes back with ``status == 0``.
    """


class InsufficientGas(ChainError):
    """Native-coin balance is below the configured floor to pay for gas.

    A USDT-only wallet cannot even send the approve, so this fails closed
    before any quoting or signing happens.
    """


class QuoteStale(ChainError):
    """No usable route/quote: no pair, zero output, or the quote aged out."""


def chain_error_family(chain):
    """Return chain-labelled subclasses of the four errors above.

    ``chain_error_family("bsc")`` gives a namespace whose ``InsufficientGas``
    stringifies as ``"bsc error ..."``. Build it ONCE per chain and import that
    namespace - never mutate ``ChainError.exchange`` in place, since these
    classes are shared with every other chain in the package.

    Callers still catch the shared base classes; the subclasses only change
    the label::

        BSC = chain_error_family("bsc")
        try:
            ...
        except InsufficientGas as exc:   # catches BSC.InsufficientGas too
            ...
    """
    from types import SimpleNamespace

    family = {}
    for base in (ChainError, SwapReverted, InsufficientGas, QuoteStale):
        family[base.__name__] = type(
            f"{chain.capitalize()}{base.__name__}", (base,),
            {"exchange": chain, "__doc__": base.__doc__},
        )
    return SimpleNamespace(chain=chain, **family)
