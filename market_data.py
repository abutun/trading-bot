from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

import ccxt
import numpy as np
import pandas as pd


logger = logging.getLogger(__name__)


REQUIRED_OHLCV_COLUMNS = ("ts", "open", "high", "low", "close", "volume")


class MarketDataError(RuntimeError):
    """Market data is missing or violates an invariant needed for trading."""


class StaleMarketDataError(MarketDataError):
    """The most recent market observation is too old to safely trade."""


class MarketData:
    """Fetch and validate public price history for all configured venues.

    The bot never treats an old, malformed, or incomplete data response as a
    valid quote.  Historical backtests can still fetch old candles; only the
    live trading path calls :meth:`validate_for_trading`.
    """

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

        params: dict[str, Any] = {"enableRateLimit": True, "timeout": 15_000}
        # Credentials are optional for market data, but can grant higher rate
        # limits. Never log them or require them for a read-only fetch.
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
                "Failed to load markets for %s: %s", config.market_data_exchange, type(exc).__name__
            )

    def _get_polymarket_client(self):
        if self._polymarket_client is None:
            try:
                from py_clob_client_v2.client import ClobClient
            except ImportError as exc:  # pragma: no cover - dependency failure path
                raise RuntimeError(
                    "py-clob-client-v2 is required for Polymarket market data"
                ) from exc
            self._polymarket_client = ClobClient(
                host=getattr(self.config, "polymarket_clob_host", "https://clob.polymarket.com"),
                chain_id=getattr(self.config, "polymarket_chain_id", 137),
            )
        return self._polymarket_client

    def fetch_ohlcv(
        self, symbol: str, limit: int | None = None, venue: str = "binance"
    ) -> pd.DataFrame:
        if venue == "polymarket":
            return self._fetch_polymarket_history(symbol)
        if venue not in {"binance", "evm"}:
            raise MarketDataError(f"Unsupported market-data venue {venue!r}")
        if self.ex is None:
            raise MarketDataError("No CCXT market-data client is configured")

        limit = limit or self.config.candle_limit
        try:
            raw = self.ex.fetch_ohlcv(
                symbol=symbol,
                timeframe=self.config.interval,
                limit=limit,
            )
        except Exception as exc:
            raise MarketDataError(
                f"{self.config.market_data_exchange} OHLCV request failed for {symbol}"
            ) from exc
        frame = pd.DataFrame(raw, columns=REQUIRED_OHLCV_COLUMNS)
        return self._normalize_ohlcv(frame, source=f"{self.config.market_data_exchange}:{symbol}")

    def _fetch_polymarket_history(self, symbol: str) -> pd.DataFrame:
        """Build explicit synthetic OHLCV bars from CLOB V2 price samples.

        Polymarket returns sampled outcome-token prices rather than exchange
        candles.  Open/high/low are therefore derived solely from adjacent
        samples; volume is unavailable and is represented as zero, never as
        actual traded volume.
        """
        try:
            from py_clob_client_v2.clob_types import PricesHistoryParams
        except ImportError as exc:  # pragma: no cover - dependency failure path
            raise RuntimeError(
                "py-clob-client-v2 is required for Polymarket market data"
            ) from exc

        token_id = self.config.resolve_polymarket_market(symbol)["token_id"]
        try:
            response = self._get_polymarket_client().get_prices_history(
                PricesHistoryParams(
                    market=token_id,
                    interval=self.config.polymarket_history_interval,
                    fidelity=self.config.polymarket_fidelity_minutes,
                )
            )
        except Exception as exc:
            raise MarketDataError(
                f"Polymarket V2 price-history request failed for {symbol}"
            ) from exc

        history = self._history_points(response)
        if not history:
            return pd.DataFrame(columns=REQUIRED_OHLCV_COLUMNS)

        points: list[tuple[Any, float]] = []
        for point in history:
            timestamp, price = self._history_point_values(point)
            points.append((timestamp, price))
        frame = pd.DataFrame(points, columns=["ts", "close"])
        frame["open"] = frame["close"].shift(1).fillna(frame["close"])
        frame["high"] = frame[["open", "close"]].max(axis=1)
        frame["low"] = frame[["open", "close"]].min(axis=1)
        frame["volume"] = 0.0
        frame = frame[["ts", "open", "high", "low", "close", "volume"]]
        frame = self._normalize_ohlcv(frame, source=f"polymarket:{symbol}")
        return frame.tail(self.config.candle_limit).reset_index(drop=True)

    @staticmethod
    def _history_points(response: Any) -> Iterable[Any]:
        if response is None:
            return []
        if isinstance(response, dict):
            for key in ("history", "data", "prices"):
                candidate = response.get(key)
                if isinstance(candidate, (list, tuple)):
                    return candidate
            return []
        if isinstance(response, (list, tuple)):
            return response
        return []

    @staticmethod
    def _history_point_values(point: Any) -> tuple[Any, float]:
        if isinstance(point, dict):
            timestamp = point.get("t", point.get("timestamp", point.get("ts")))
            price = point.get("p", point.get("price"))
        else:
            timestamp = getattr(point, "t", getattr(point, "timestamp", None))
            price = getattr(point, "p", getattr(point, "price", None))
        if timestamp is None or price is None:
            raise MarketDataError("Polymarket returned a history point without time or price")
        try:
            numeric_price = float(price)
        except (TypeError, ValueError) as exc:
            raise MarketDataError("Polymarket returned a non-numeric history price") from exc
        return timestamp, numeric_price

    @staticmethod
    def _timestamps_to_utc(values: pd.Series) -> pd.Series:
        if pd.api.types.is_datetime64_any_dtype(values):
            parsed = pd.to_datetime(values, utc=True, errors="coerce")
        else:
            numeric = pd.to_numeric(values, errors="coerce")
            if numeric.isna().any():
                # Accept ISO timestamps for test fixtures / alternate providers,
                # but normalize all successful values to UTC timestamps.
                parsed = pd.to_datetime(values, utc=True, errors="coerce")
            else:
                largest = float(numeric.abs().max()) if len(numeric) else 0.0
                if largest >= 1e14:
                    unit = "us"
                elif largest >= 1e11:
                    unit = "ms"
                else:
                    unit = "s"
                parsed = pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")
        if parsed.isna().any():
            raise MarketDataError("Market data contains an invalid timestamp")
        return parsed

    def _normalize_ohlcv(self, frame: pd.DataFrame, source: str) -> pd.DataFrame:
        if frame.empty:
            return pd.DataFrame(columns=REQUIRED_OHLCV_COLUMNS)
        missing = [column for column in REQUIRED_OHLCV_COLUMNS if column not in frame.columns]
        if missing:
            raise MarketDataError(f"{source} is missing OHLCV columns: {', '.join(missing)}")
        normalized = frame.loc[:, list(REQUIRED_OHLCV_COLUMNS)].copy()
        normalized["ts"] = self._timestamps_to_utc(normalized["ts"])
        for column in ("open", "high", "low", "close", "volume"):
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
        values = normalized[["open", "high", "low", "close", "volume"]]
        if values.isna().any().any() or not np.isfinite(
            values.to_numpy(dtype=float)
        ).all():
            raise MarketDataError(f"{source} contains non-finite OHLCV values")
        if (normalized[["open", "high", "low", "close"]] <= 0).any().any():
            raise MarketDataError(f"{source} contains a non-positive price")
        if (normalized["volume"] < 0).any():
            raise MarketDataError(f"{source} contains negative volume")
        if (
            (normalized["high"] < normalized["low"]).any()
            or (normalized["high"] < normalized[["open", "close"]].max(axis=1)).any()
            or (normalized["low"] > normalized[["open", "close"]].min(axis=1)).any()
        ):
            raise MarketDataError(f"{source} contains inconsistent OHLC bounds")
        normalized = (
            normalized.drop_duplicates(subset="ts", keep="last")
            .sort_values("ts")
            .reset_index(drop=True)
        )
        return normalized

    def validate_for_trading(self, frame: pd.DataFrame, venue: str) -> pd.Timestamp:
        """Return the fresh last timestamp or raise before a trading decision."""
        if frame is None or frame.empty:
            raise MarketDataError("No market-data candles were returned")
        # Frames passed by callers may be fakes/tests, so repeat the invariant
        # validation rather than trusting that they originated in fetch_ohlcv.
        normalized = self._normalize_ohlcv(frame, source=f"{venue} trading data")
        last_timestamp = normalized["ts"].iloc[-1]
        if not isinstance(last_timestamp, pd.Timestamp):
            last_timestamp = pd.Timestamp(last_timestamp)
        if last_timestamp.tzinfo is None:
            last_timestamp = last_timestamp.tz_localize("UTC")
        now = pd.Timestamp(datetime.now(timezone.utc))
        age_seconds = (now - last_timestamp).total_seconds()
        # A candle timestamp is normally its opening time, so an actively
        # updating 15m candle may be almost one interval old. The configured
        # max age is a grace period *after* that expected cadence.
        expected_cadence = self._expected_cadence_seconds(venue)
        permitted_age = expected_cadence + self.config.market_data_max_age_seconds
        if age_seconds < -60 or age_seconds > permitted_age:
            raise StaleMarketDataError(
                f"{venue} market data age {age_seconds:.1f}s exceeds permitted "
                f"{permitted_age}s"
            )
        return last_timestamp

    def _expected_cadence_seconds(self, venue: str) -> int:
        if venue == "polymarket":
            return max(60, int(self.config.polymarket_fidelity_minutes) * 60)
        return self._interval_to_seconds(self.config.interval)

    @staticmethod
    def _interval_to_seconds(interval: str) -> int:
        raw = str(interval).strip().lower()
        units = {"m": 60, "h": 3600, "d": 86_400, "w": 604_800}
        if len(raw) >= 2 and raw[-1] in units:
            try:
                amount = int(raw[:-1])
            except ValueError:
                amount = 0
            if amount > 0:
                return amount * units[raw[-1]]
        # Invalid exchange interval will be rejected by CCXT; this conservative
        # fallback avoids treating data as fresh indefinitely in the meantime.
        return 60

    def close(self) -> None:
        for client in (self.ex, self._polymarket_client):
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # pragma: no cover - shutdown best effort
                    logger.debug("Market-data client close failed", exc_info=True)
