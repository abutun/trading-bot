import unittest

from config import Config
from risk import RiskManager


class FakeState:
    def __init__(self):
        self.meta = {}
        self.positions = {}
        self.unresolved = set()

    def get_meta(self, key, default=None):
        return self.meta.get(key, default)

    def set_meta(self, key, value):
        self.meta[key] = value

    def get_position(self, pair_id):
        return self.positions.get(pair_id)

    def get_all_positions(self):
        return list(self.positions.values())

    def has_unresolved_order(self, pair_id):
        return pair_id in self.unresolved


class RiskManagerTests(unittest.TestCase):
    def test_first_update_persists_daily_baseline_and_daily_halt(self):
        config = Config(max_daily_loss_pct=2, max_total_drawdown_pct=99)
        state = FakeState()
        manager = RiskManager(config, state)

        manager.update(1000)
        self.assertEqual(state.get_meta("daily_start_equity"), "1000")
        self.assertEqual(state.get_meta("halted_daily"), "false")

        manager.update(979)
        self.assertTrue(manager.halted())

    def test_unresolved_order_blocks_entry(self):
        config = Config()
        state = FakeState()
        state.unresolved.add("binance:BTC/USDT")
        manager = RiskManager(config, state)

        self.assertFalse(manager.can_open("binance:BTC/USDT", 1000))

    def test_initial_peak_is_persisted_for_total_drawdown(self):
        config = Config(max_daily_loss_pct=99, max_total_drawdown_pct=10)
        state = FakeState()
        manager = RiskManager(config, state)

        manager.update(1000)
        self.assertEqual(state.get_meta("peak_equity"), "1000")
        manager.update(900)
        self.assertTrue(manager.total_halted())
