"""Shared exception type for all exchange REST clients.

Every venue client (Bitget, Bybit, OKX, Binance) raises a subclass of
ExchangeAPIError so callers can catch one type regardless of exchange.

Contract (relied on by the clients and their tests):
    err = SomeExchangeAPIError(code, message, http_status=None)
    err.code        -> str venue/business error code, or one of the synthetic
                       codes "NETWORK" (transport failure), "HTTP" (non-JSON
                       response) and "SYMBOL" (unknown instrument)
    err.message     -> human-readable message from the venue
    err.http_status -> HTTP status code when a response was received, else None
    err.exchange    -> class attribute naming the venue (e.g. "Bybit")

str(err) preserves the bot's historical log/notification format,
"Bitget error {code}: {message}", generalized per exchange:
"{exchange} error {code}: {message}".
"""


class ExchangeAPIError(Exception):
    """Base error for all exchange client failures."""

    exchange = "Exchange"

    def __init__(self, code, message, http_status=None):
        self.code = str(code)
        self.message = message
        self.http_status = http_status
        super().__init__(f"{self.exchange} error {self.code}: {message}")
