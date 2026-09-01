from types import SimpleNamespace

from brokers.paper import PaperBroker
from models import Position


class State:
    def __init__(self):
        self.meta = {}
        self.positions = []

    def get_meta(self, key, default=None):
        return self.meta.get(key, default)

    def set_meta(self, key, value):
        self.meta[key] = value

    def get_positions_by_venue(self, venue):
        return [position for position in self.positions if position.venue == venue]


def test_equity_uses_pair_id_price_not_entry_price():
    state = State()
    state.positions.append(
        Position(
            pair_id="binance:BTC/USDT",
            venue="binance",
            symbol="BTC/USDT",
            qty=2,
            entry_price=50,
        )
    )
    broker = PaperBroker(
        SimpleNamespace(fee_rate=0.001, slippage_bps=5),
        state,
        venue="binance",
        initial_capital=100,
    )

    assert broker.get_equity({"binance:BTC/USDT": 80}) == 260


def test_paper_execution_has_a_durable_order_identifier():
    state = State()
    broker = PaperBroker(
        SimpleNamespace(fee_rate=0.001, slippage_bps=5),
        state,
        venue="binance",
        initial_capital=1000,
    )

    result = broker.buy("BTC/USDT", qty=1, price_hint=100, client_order_id="intent-1")

    assert result.order_id == "paper:intent-1"
