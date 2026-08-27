import unittest
from types import SimpleNamespace

from brokers.polymarket_broker import PolymarketBroker


class FakeConfig:
    polymarket_slippage_bps = 100

    @staticmethod
    def resolve_polymarket_market(symbol):
        if symbol != "example-yes":
            raise ValueError("unknown symbol")
        return {"token_id": "token-123"}


class FakeClient:
    def __init__(self):
        self.calls = []

    def place_market_order(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["side"] == "BUY":
            return SimpleNamespace(
                ok=True,
                taking_amount="20",
                making_amount="10",
                order_id="buy-order",
            )
        return SimpleNamespace(
            ok=True,
            making_amount="20",
            taking_amount="12",
            order_id="sell-order",
        )


class PolymarketBrokerTests(unittest.TestCase):
    def setUp(self):
        self.broker = object.__new__(PolymarketBroker)
        self.broker.config = FakeConfig()
        self.broker.client = FakeClient()

    def test_buy_interprets_pusd_and_share_amounts(self):
        result = self.broker.buy("example-yes", qty=20, price_hint=0.5)

        self.assertEqual(result.qty, 20)
        self.assertEqual(result.price, 0.5)
        request = self.broker.client.calls[-1]
        self.assertEqual(request["token_id"], "token-123")
        self.assertEqual(request["amount"], "10.0")
        self.assertEqual(request["order_type"], "FOK")

    def test_sell_interprets_share_and_pusd_amounts(self):
        result = self.broker.sell("example-yes", qty=20, price_hint=0.6)

        self.assertEqual(result.qty, 20)
        self.assertEqual(result.price, 0.6)
        request = self.broker.client.calls[-1]
        self.assertEqual(request["shares"], "20")
        self.assertEqual(request["order_type"], "FOK")
