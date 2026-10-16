from __future__ import annotations

from unittest.mock import MagicMock, patch

import ccxt
import pytest

from guardian.exchange import BitgetClient, TamperSignal


def test_authentication_error_becomes_tamper_signal():
    with patch("guardian.exchange.ccxt.bitget") as mock_bitget_cls:
        mock_exchange = MagicMock()
        mock_exchange.fetch_balance.side_effect = ccxt.AuthenticationError("bad key")
        mock_bitget_cls.return_value = mock_exchange

        client = BitgetClient("key", "secret", "pass")
        with pytest.raises(TamperSignal):
            client.fetch_balance()


def test_permission_denied_becomes_tamper_signal():
    with patch("guardian.exchange.ccxt.bitget") as mock_bitget_cls:
        mock_exchange = MagicMock()
        mock_exchange.fetch_balance.side_effect = ccxt.PermissionDenied("no permission")
        mock_bitget_cls.return_value = mock_exchange

        client = BitgetClient("key", "secret", "pass")
        with pytest.raises(TamperSignal):
            client.fetch_balance()


def test_transient_network_error_is_not_a_tamper_signal():
    with patch("guardian.exchange.ccxt.bitget") as mock_bitget_cls:
        mock_exchange = MagicMock()
        mock_exchange.fetch_balance.side_effect = ccxt.NetworkError("timeout")
        mock_bitget_cls.return_value = mock_exchange

        client = BitgetClient("key", "secret", "pass")
        with pytest.raises(ccxt.NetworkError):
            client.fetch_balance()


def test_secrets_are_redacted_from_error_messages():
    with patch("guardian.exchange.ccxt.bitget") as mock_bitget_cls:
        mock_exchange = MagicMock()
        mock_exchange.fetch_balance.side_effect = ccxt.ExchangeError("failed for key super-secret-key-123")
        mock_bitget_cls.return_value = mock_exchange

        client = BitgetClient("super-secret-key-123", "secret", "pass")
        with pytest.raises(ccxt.ExchangeError) as exc_info:
            client.fetch_balance()
        assert "super-secret-key-123" not in str(exc_info.value)
