import unittest
from types import SimpleNamespace

from market_data import MarketData


class FakeConfig:
    candle_limit = 3
    polymarket_history_interval = "1d"
    polymarket_fidelity_minutes = 15

    @staticmethod
    def resolve_polymarket_market(symbol):
        return {"token_id": "token-123"}


class FakePublicClient:
    def get_price_history(self, **kwargs):
        self.request = kwargs
        return (
            SimpleNamespace(t=1, p=0.40),
            SimpleNamespace(t=2, p=0.60),
            SimpleNamespace(t=3, p=0.50),
            SimpleNamespace(t=4, p=0.55),
        )


class MarketDataTests(unittest.TestCase):
    def test_polymarket_samples_become_synthetic_ohlc(self):
        data = object.__new__(MarketData)
        data.config = FakeConfig()
        client = FakePublicClient()
        data._get_polymarket_client = lambda: client

        frame = data._fetch_polymarket_history("example-yes")

        self.assertEqual(len(frame), 3)
        self.assertEqual(frame.iloc[0]["open"], 0.40)
        self.assertEqual(frame.iloc[0]["high"], 0.60)
        self.assertEqual(frame.iloc[0]["low"], 0.40)
        self.assertEqual(frame.iloc[-1]["close"], 0.55)
        self.assertEqual(client.request["token_id"], "token-123")
