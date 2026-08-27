from __future__ import annotations

import logging
from typing import Any

import ccxt
import pandas as pd


logger = logging.getLogger(__name__)


class MarketData:
    """Fetch price history for exchange and Polymarket outcome-token pairs."""

    def __init__(self, config):
        self.config = config
        self._polymarket_client: Any | None = None
        self.ex: Any | None = None

        if not any(pair.venue != "polymarket" for pair in config.trading_pairs):
            return

        try:
            exchange_class = getattr(ccxt, config.market_data_exchange)
        except AttributeError as exc:
            raise ValueError(
                f"Unknown ccxt exchange id: {config.market_data_exchange!r}"
            ) from exc

        params = {"enableRateLimit": True}
        if config.market_data_exchange == "binance" and config.binance_api_key:
            params["apiKey"] = config.binance_api_key
            params["secret"] = config.binance_api_secret

        self.ex = exchange_class(params)

        if config.market_data_exchange == "binance" and config.binance_testnet:
            self.ex.set_sandbox_mode(True)

        try:
            self.ex.load_markets()
        except Exception as exc:
            logger.warning(
                "Failed to load markets for %s: %s", config.market_data_exchange, exc
            )

    def _get_polymarket_client(self):
        if self._polymarket_client is None:
            try:
                from polymarket import PublicClient
            except ImportError as exc:  # pragma: no cover - dependency failure path
                raise RuntimeError(
                    "polymarket-client is required for Polymarket market data"
                ) from exc
            self._polymarket_client = PublicClient()
        return self._polymarket_client

    def fetch_ohlcv(
        self, symbol: str, limit: int | None = None, venue: str = "binance"
    ) -> pd.DataFrame:
        if venue == "polymarket":
            return self._fetch_polymarket_history(symbol)

        limit = limit or self.config.candle_limit
        if self.ex is None:
            raise RuntimeError("No ccxt exchange client is configured for this bot")

        raw = self.ex.fetch_ohlcv(
            symbol=symbol,
            timeframe=self.config.interval,
            limit=limit,
        )
        return pd.DataFrame(
            raw,
            columns=["ts", "open", "high", "low", "close", "volume"],
        )

    def _fetch_polymarket_history(self, symbol: str) -> pd.DataFrame:
        """Convert Polymarket observed-price history into strategy-compatible bars.

        The public CLOB exposes a sampled price series, not exchange OHLCV.
        Each sample therefore becomes a synthetic bar whose open is the prior
        sample and whose high/low span that movement; volume is unavailable and
        set to zero. This preserves price movement for the ATR stop calculation
        without pretending to have candle volume data.
        """
        token_id = self.config.resolve_polymarket_market(symbol)["token_id"]
        history = self._get_polymarket_client().get_price_history(
            token_id=token_id,
            interval=self.config.polymarket_history_interval,
            fidelity=self.config.polymarket_fidelity_minutes,
        )

        if not history:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])

        points = [(point.t, float(point.p)) for point in history]
        frame = pd.DataFrame(points, columns=["ts", "close"])
        frame = frame.drop_duplicates(subset="ts", keep="last").sort_values("ts")
        frame["open"] = frame["close"].shift(1).fillna(frame["close"])
        frame["high"] = frame[["open", "close"]].max(axis=1)
        frame["low"] = frame[["open", "close"]].min(axis=1)
        frame["volume"] = 0.0
        return (
            frame[["ts", "open", "high", "low", "close", "volume"]]
            .tail(self.config.candle_limit)
            .reset_index(drop=True)
        )

    def close(self) -> None:
        if self._polymarket_client is not None:
            self._polymarket_client.close()
