from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from bot import TradingBot
from brokers.base import BrokerOrderRejectedError, BrokerOrderUncertainError
from config import Pair
from market_data import StaleMarketDataError
from models import ExecutionResult, Position, Signal


class FakeState:
    def __init__(self):
        self.meta: dict[str, str] = {}
        self.positions: dict[str, Position] = {}
        self.orders: dict[str, dict] = {}
        self.equity: list[float] = []
        self._intent_number = 0

    def get_meta(self, key, default=None):
        return self.meta.get(key, default)

    def set_meta(self, key, value):
        self.meta[key] = value

    def get_position(self, pair_id):
        return self.positions.get(pair_id)

    def get_all_positions(self):
        return list(self.positions.values())

    def get_positions_by_venue(self, venue):
        return [pos for pos in self.positions.values() if pos.venue == venue]

    def has_unresolved_order(self, pair_id):
        return any(
            order["pair_id"] == pair_id and order["status"] in {"pending", "unknown"}
            for order in self.orders.values()
        )

    def has_any_unresolved_order(self):
        return any(order["status"] in {"pending", "unknown"} for order in self.orders.values())

    def create_order_intent(self, *, pair_id, venue, symbol, side, qty, price):
        self._intent_number += 1
        intent = f"intent-{self._intent_number}"
        self.orders[intent] = {
            "pair_id": pair_id,
            "venue": venue,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": price,
            "status": "pending",
            "broker_order_id": "",
        }
        return intent

    def mark_order_submitted(self, intent, broker_order_id):
        self.orders[intent]["broker_order_id"] = broker_order_id

    def mark_order_unknown(self, intent, error):
        self.orders[intent]["status"] = "unknown"
        self.orders[intent]["error"] = error

    def mark_order_rejected(self, intent, error):
        self.orders[intent]["status"] = "rejected"
        self.orders[intent]["error"] = error

    def complete_entry(self, intent, pos, result, _ts):
        self.orders[intent]["status"] = "filled"
        self.positions[pos.pair_id] = pos

    def complete_exit(self, intent, pos, result, _ts):
        self.orders[intent]["status"] = "filled"
        self.positions.pop(pos.pair_id, None)
        return 0.0

    def set_latest_price(self, pair_id, price, ts, source):
        self.meta[f"last_price:{pair_id}"] = str(price)

    def record_equity(self, _ts, equity):
        self.equity.append(equity)

    def ping(self):
        return True

    def acquire_bot_lock(self):
        return None

    def close(self):
        return None


class FreshData:
    def __init__(self, price=100.0):
        now = datetime.now(timezone.utc)
        self.frame = pd.DataFrame(
            [
                [now - timedelta(minutes=30), price, price + 1, price - 1, price, 1],
                [now - timedelta(minutes=15), price, price + 1, price - 1, price, 1],
                [now, price, price + 1, price - 1, price, 1],
            ],
            columns=["ts", "open", "high", "low", "close", "volume"],
        )

    def fetch_ohlcv(self, *_args, **_kwargs):
        return self.frame

    def validate_for_trading(self, _frame, _venue):
        return self.frame["ts"].iloc[-1]

    def close(self):
        return None


class StaleData(FreshData):
    def validate_for_trading(self, _frame, _venue):
        raise StaleMarketDataError("stale")


class BuySignal:
    def generate(self, _frame):
        return Signal(action=1, stop_loss=90.0, take_profit=110.0)


class HoldSignal:
    def generate(self, _frame):
        return Signal()


class SellSignal:
    def generate(self, _frame):
        return Signal(action=-1)


class UncertainBroker:
    venue = "binance"

    def get_equity(self, _prices):
        return 1000.0

    def buy(self, *_args):
        raise BrokerOrderUncertainError("request timed out", "venue-order-9")

    def sell(self, *_args):
        raise AssertionError("not expected")


class BadFillBroker:
    venue = "binance"

    def get_equity(self, _prices):
        return 1000.0

    def buy(self, *_args):
        return ExecutionResult(qty=1.0, price=105.0, order_id="venue-order-10")

    def sell(self, *_args):
        raise AssertionError("not expected")


class BrokenEquityBroker(BadFillBroker):
    def get_equity(self, _prices):
        raise ConnectionError("balance endpoint unavailable")


class OutOfSlippageFirstExitBroker:
    venue = "binance"

    def __init__(self):
        self.sells: list[str] = []

    def get_equity(self, _prices):
        return 1_000.0

    def buy(self, *_args):
        raise AssertionError("not expected")

    def sell(self, symbol, qty, _price_hint, *_args):
        self.sells.append(symbol)
        # 98 is outside the configured 100-bps sell floor from reference 100.
        return ExecutionResult(qty=qty, price=98.0, order_id=f"exit-{len(self.sells)}")


class RejectedExitBroker:
    venue = "binance"

    def get_equity(self, _prices):
        return 1_000.0

    def buy(self, *_args):
        raise AssertionError("not expected")

    def sell(self, *_args):
        raise BrokerOrderRejectedError("IOC did not fill")


def _config(mode="live"):
    return SimpleNamespace(
        mode=mode,
        trading_pairs=[Pair("binance", "BTC/USDT")],
        paper_initial_capital=1000.0,
        order_cooldown_seconds=0,
        use_closed_candles=True,
        max_daily_loss_pct=2.0,
        max_total_drawdown_pct=10.0,
        max_consecutive_failures=3,
        max_open_positions=3,
        max_position_pct=10.0,
        max_total_exposure_pct=30.0,
        max_order_notional=100.0,
        max_order_slippage_bps=100,
        min_notional=10.0,
        loop_seconds=60,
    )


def _bot(config, state, data, strategy, broker):
    return TradingBot(
        config,
        state=state,
        market_data=data,
        strategy=strategy,
        brokers={"binance": broker},
    )


def test_uncertain_external_order_is_persisted_and_halts_automation():
    state = FakeState()
    bot = _bot(_config(), state, FreshData(), HoldSignal(), UncertainBroker())
    pair = bot.config.trading_pairs[0]

    with pytest.raises(BrokerOrderUncertainError):
        bot._submit_order(pair, "buy", qty=1.0, price=100.0)

    intent = state.orders["intent-1"]
    assert intent["status"] == "unknown"
    assert intent["broker_order_id"] == "venue-order-9"
    assert bot.risk.halted()


def test_data_failure_never_falls_back_to_an_old_price_or_submits_order():
    state = FakeState()
    bot = _bot(_config(), state, StaleData(), BuySignal(), BadFillBroker())

    assert bot.step() is False
    assert state.orders == {}
    assert state.get_meta("bot_consecutive_failures") == "1"


def test_fill_outside_global_slippage_is_recorded_then_safety_halted():
    state = FakeState()
    bot = _bot(_config(), state, FreshData(), BuySignal(), BadFillBroker())

    assert bot.step() is False
    assert state.orders["intent-1"]["status"] == "filled"
    assert state.positions["binance:BTC/USDT"].entry_price == 105.0
    assert bot.risk.halted()


def test_equity_failure_is_not_silently_ignored():
    state = FakeState()
    bot = _bot(_config(), state, FreshData(), HoldSignal(), BrokenEquityBroker())

    assert bot.step() is False
    assert state.equity == []
    assert state.get_meta("bot_consecutive_failures") == "1"


def test_preflight_rejects_an_unprotected_carried_position():
    state = FakeState()
    pair_id = "binance:BTC/USDT"
    state.positions[pair_id] = Position(
        pair_id=pair_id,
        venue="binance",
        symbol="BTC/USDT",
        qty=1,
        entry_price=100,
        stop_loss=0,
        take_profit=110,
        opened_at=datetime.now(timezone.utc).isoformat(),
    )
    bot = _bot(_config(), state, FreshData(), HoldSignal(), BadFillBroker())

    with pytest.raises(RuntimeError, match="stop_loss"):
        bot.preflight(verify_market_data=False)
    assert bot.risk.halted()


def test_out_of_slippage_exit_halts_before_a_second_position_can_submit():
    state = FakeState()
    config = _config()
    config.trading_pairs = [Pair("binance", "BTC/USDT"), Pair("binance", "ETH/USDT")]
    for pair in config.trading_pairs:
        state.positions[pair.pair_id] = Position(
            pair_id=pair.pair_id,
            venue=pair.venue,
            symbol=pair.symbol,
            qty=1,
            entry_price=100,
            stop_loss=90,
            take_profit=110,
            opened_at=datetime.now(timezone.utc).isoformat(),
        )
    broker = OutOfSlippageFirstExitBroker()
    bot = _bot(config, state, FreshData(), SellSignal(), broker)

    assert bot.step() is False
    assert broker.sells == ["BTC/USDT"]
    assert bot.risk.halted()


def test_rejected_exit_is_a_safety_halt_not_a_successful_cycle():
    state = FakeState()
    pair = Pair("binance", "BTC/USDT")
    state.positions[pair.pair_id] = Position(
        pair_id=pair.pair_id,
        venue=pair.venue,
        symbol=pair.symbol,
        qty=1,
        entry_price=100,
        stop_loss=90,
        take_profit=110,
        opened_at=datetime.now(timezone.utc).isoformat(),
    )
    bot = _bot(_config(), state, FreshData(), SellSignal(), RejectedExitBroker())

    assert bot.step() is False
    assert state.orders["intent-1"]["status"] == "rejected"
    assert state.positions[pair.pair_id].qty == 1
    assert bot.risk.halted()
    assert state.meta["halt_reason"].startswith("exit_rejected:")


def test_rejected_risk_liquidation_does_not_report_success_or_continue():
    state = FakeState()
    pair = Pair("binance", "BTC/USDT")
    state.positions[pair.pair_id] = Position(
        pair_id=pair.pair_id,
        venue=pair.venue,
        symbol=pair.symbol,
        qty=1,
        entry_price=100,
        stop_loss=90,
        take_profit=110,
        opened_at=datetime.now(timezone.utc).isoformat(),
    )
    state.meta.update(
        {
            "day": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "daily_start_equity": "1000",
            "peak_equity": "1000",
            "halted_daily": "true",
        }
    )
    bot = _bot(_config(), state, FreshData(), SellSignal(), RejectedExitBroker())

    assert bot.step() is False
    assert bot.risk.halted()
    assert state.meta["halt_reason"].startswith("risk_limit_exit_rejected:")
