from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from market_data import MarketData, MarketDataError, StaleMarketDataError


class FakeConfig:
    candle_limit = 3
    polymarket_history_interval = "1d"
    polymarket_fidelity_minutes = 15
    polymarket_clob_host = "https://clob.polymarket.com"
    polymarket_chain_id = 137
    market_data_max_age_seconds = 60
    interval = "15m"

    @staticmethod
    def resolve_polymarket_market(symbol):
        return {"token_id": "token-123"}


class FakePublicClient:
    def get_prices_history(self, params):
        self.request = params
        return [
            SimpleNamespace(t=1_700_000_000, p=0.40),
            SimpleNamespace(t=1_700_000_100, p=0.60),
            SimpleNamespace(t=1_700_000_200, p=0.50),
            SimpleNamespace(t=1_700_000_300, p=0.55),
        ]


def _data_with_fake_client():
    data = object.__new__(MarketData)
    data.config = FakeConfig()
    client = FakePublicClient()
    data._get_polymarket_client = lambda: client
    return data, client


def test_polymarket_samples_become_synthetic_ohlc():
    data, client = _data_with_fake_client()

    frame = data._fetch_polymarket_history("example-yes")

    assert len(frame) == 3
    assert frame.iloc[0]["open"] == 0.40
    assert frame.iloc[0]["high"] == 0.60
    assert frame.iloc[0]["low"] == 0.40
    assert frame.iloc[-1]["close"] == 0.55
    assert client.request.market == "token-123"
    assert client.request.interval == "1d"
    assert client.request.fidelity == 15


def test_invalid_ohlcv_is_rejected_before_a_signal_is_generated():
    data, _ = _data_with_fake_client()
    frame = pd.DataFrame(
        [[1_700_000_000, 1.0, 0.4, 0.6, 0.5, 0.0]],
        columns=["ts", "open", "high", "low", "close", "volume"],
    )

    with pytest.raises(MarketDataError, match="inconsistent OHLC"):
        data._normalize_ohlcv(frame, source="test")


def test_stale_trading_data_is_rejected_but_historical_fetch_is_allowed():
    data, _ = _data_with_fake_client()
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    frame = pd.DataFrame(
        [[stale, 1.0, 1.1, 0.9, 1.0, 0.0]],
        columns=["ts", "open", "high", "low", "close", "volume"],
    )

    with pytest.raises(StaleMarketDataError):
        data.validate_for_trading(frame, "polymarket")
