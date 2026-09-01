import math

import numpy as np
import pandas as pd

from models import Signal


class StrategyInputError(ValueError):
    """The strategy was asked to evaluate invalid market data."""


class EMARSI:
    def __init__(self, config):
        self.config = config

    def _compute(self, df: pd.DataFrame) -> pd.DataFrame:
        out = self._validated_frame(df)

        close = out["close"]
        high = out["high"]
        low = out["low"]

        out["ema_fast"] = close.ewm(span=self.config.ema_fast, adjust=False).mean()
        out["ema_slow"] = close.ewm(span=self.config.ema_slow, adjust=False).mean()

        delta = close.diff()
        gain = (
            delta.clip(lower=0.0)
            .ewm(alpha=1 / self.config.rsi_period, adjust=False)
            .mean()
        )
        loss = (
            (-delta.clip(upper=0.0))
            .ewm(alpha=1 / self.config.rsi_period, adjust=False)
            .mean()
        )

        rs = gain / loss.replace(0.0, 1e-12)
        out["rsi"] = (100 - (100 / (1 + rs))).fillna(50.0)
        out.loc[(gain == 0) & (loss == 0), "rsi"] = 50.0

        prev_close = close.shift(1)
        tr = pd.concat(
            [
                (high - low),
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        )

        out["atr"] = (
            tr.max(axis=1)
            .ewm(alpha=1 / self.config.atr_period, adjust=False)
            .mean()
            .fillna(0.0)
        )

        out["stop_loss"] = (
            out["close"] - self.config.stop_loss_atr_mult * out["atr"]
        )
        out["take_profit"] = (
            out["close"] + self.config.take_profit_atr_mult * out["atr"]
        )

        out["action"] = 0

        buy_condition = (out["ema_fast"] > out["ema_slow"]) & (out["rsi"] < 70)
        sell_condition = (out["ema_fast"] < out["ema_slow"]) & (out["rsi"] > 30)

        out.loc[buy_condition, "action"] = 1
        out.loc[sell_condition, "action"] = -1

        warmup = max(self.config.ema_slow, self.config.rsi_period, self.config.atr_period) + 1
        out["action"] = out["action"].where(out.index >= warmup, 0)

        return out

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._compute(df)

    def generate(self, df: pd.DataFrame) -> Signal:
        if df is None or len(df) == 0:
            return Signal(0, 0.0, 0.0)

        warmup = max(self.config.ema_slow, self.config.rsi_period, self.config.atr_period) + 1
        if len(df) <= warmup:
            return Signal(0, 0.0, 0.0)

        out = self._compute(df)
        last = out.iloc[-1]

        action = int(last["action"])
        stop_loss = float(last["stop_loss"])
        take_profit = float(last["take_profit"])
        last_price = float(last["close"])
        # A flat/invalid ATR does not provide a meaningful protected entry.
        # Treat it as no signal instead of opening a position with a stop at
        # the entry price.
        if action == 1 and (
            not math.isfinite(stop_loss)
            or not math.isfinite(take_profit)
            or stop_loss <= 0
            or stop_loss >= last_price
            or take_profit <= last_price
        ):
            action = 0

        return Signal(
            action=action,
            stop_loss=stop_loss if math.isfinite(stop_loss) else 0.0,
            take_profit=take_profit if math.isfinite(take_profit) else 0.0,
        )

    @staticmethod
    def _validated_frame(df: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(df, pd.DataFrame):
            raise StrategyInputError("Strategy input must be a pandas DataFrame")
        required = ("close", "high", "low")
        missing = [column for column in required if column not in df.columns]
        if missing:
            raise StrategyInputError(
                f"Strategy input is missing columns: {', '.join(missing)}"
            )
        out = df.copy()
        for column in required:
            out[column] = pd.to_numeric(out[column], errors="coerce")
        values = out.loc[:, list(required)].to_numpy(dtype=float)
        if not np.isfinite(values).all() or (values <= 0).any():
            raise StrategyInputError("Strategy input contains non-finite or non-positive prices")
        if (out["high"] < out["low"]).any() or (
            out["high"] < out[["close", "low"]].max(axis=1)
        ).any() or (
            out["low"] > out[["close", "high"]].min(axis=1)
        ).any():
            raise StrategyInputError("Strategy input contains inconsistent high/low bounds")
        return out
