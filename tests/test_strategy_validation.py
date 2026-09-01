from types import SimpleNamespace

import pandas as pd
import pytest

from strategy import EMARSI, StrategyInputError


def _strategy():
    return EMARSI(
        SimpleNamespace(
            ema_fast=2,
            ema_slow=3,
            rsi_period=2,
            atr_period=2,
            stop_loss_atr_mult=2.0,
            take_profit_atr_mult=3.0,
        )
    )


def test_strategy_rejects_nonfinite_and_inconsistent_prices():
    frame = pd.DataFrame({"close": [1.0], "high": [0.5], "low": [1.1]})

    with pytest.raises(StrategyInputError):
        _strategy().compute(frame)


def test_strategy_returns_hold_until_warmup_is_complete():
    frame = pd.DataFrame(
        {"close": [1.0, 1.1, 1.2], "high": [1.1, 1.2, 1.3], "low": [0.9, 1.0, 1.1]}
    )

    assert _strategy().generate(frame).action == 0
