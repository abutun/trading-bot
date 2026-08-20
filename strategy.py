import pandas as pd

from models import Signal


class EMARSI:
    def __init__(self, config):
        self.config = config

    def _compute(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()

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

        out = self._compute(df)
        last = out.iloc[-1]

        return Signal(
            action=int(last["action"]),
            stop_loss=float(last["stop_loss"]),
            take_profit=float(last["take_profit"]),
        )
