from __future__ import annotations

import logging

import ccxt
import pandas as pd


logger = logging.getLogger(__name__)


class MarketData:
    def __init__(self, config):
        self.config = config

        exchange_class = getattr(ccxt, config.market_data_exchange)

        params = {
            "enableRateLimit": True,
        }

        if config.market_data_exchange == "binance" and config.binance_api_key:
            params["apiKey"] = config.binance_api_key
            params["secret"] = config.binance_api_secret

        self.ex = exchange_class(params)

        if config.market_data_exchange == "binance" and config.binance_testnet:
            self.ex.set_sandbox_mode(True)

        try:
            self.ex.load_markets()
        except Exception as exc:
            logger.warning("Failed to load markets for %s: %s", config.market_data_exchange, exc)

    def fetch_ohlcv(self, symbol: str, limit: int | None = None) -> pd.DataFrame:
        limit = limit or self.config.candle_limit

        raw = self.ex.fetch_ohlcv(
            symbol=symbol,
            timeframe=self.config.interval,
            limit=limit,
        )

        df = pd.DataFrame(
            raw,
            columns=["ts", "open", "high", "low", "close", "volume"],
        )

        return df
