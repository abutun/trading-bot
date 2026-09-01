from types import SimpleNamespace

import pytest

from brokers.base import BrokerOrderRejectedError, BrokerOrderUncertainError
from brokers.binance_broker import BinanceBroker


class FakeExchange:
    def __init__(
        self,
        *,
        status="closed",
        normalized_price="101.00",
        fee=None,
        fees=None,
        include_fee=True,
        my_trades=None,
        cost=100.5,
    ):
        self.status = status
        self.normalized_price = normalized_price
        self.fee = {"cost": 0.1, "currency": "USDT"} if fee is None else fee
        self.fees = fees
        self.include_fee = include_fee
        self.my_trades = my_trades
        self.cost = cost
        self.calls = []
        self.my_trade_calls = []

    def amount_to_precision(self, _symbol, amount):
        return f"{amount:.4f}"

    def price_to_precision(self, _symbol, _price):
        return self.normalized_price

    def create_order(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": "exchange-1"}

    def fetch_order(self, order_id, symbol):
        assert order_id == "exchange-1"
        assert symbol == "BTC/USDT"
        order = {
            "id": order_id,
            "status": self.status,
            "filled": 1.0 if self.status == "closed" else 0.0,
            "average": 100.5,
            "cost": self.cost,
        }
        if self.include_fee:
            order["fee"] = self.fee
        if self.fees is not None:
            order["fees"] = self.fees
        return order

    def fetch_my_trades(self, symbol, since, limit, params):
        self.my_trade_calls.append((symbol, since, limit, params))
        if self.my_trades is None:
            raise RuntimeError("trade endpoint unavailable")
        return self.my_trades


def _broker(exchange):
    broker = object.__new__(BinanceBroker)
    broker.config = SimpleNamespace(
        max_order_slippage_bps=100,
        max_order_notional=1_000.0,
        quote_currency="USDT",
        binance_order_mode="ioc_limit",
    )
    broker.ex = exchange
    return broker


def test_binance_buy_is_ioc_limit_with_client_order_id_and_terminal_fill():
    exchange = FakeExchange()
    broker = _broker(exchange)

    result = broker.buy("BTC/USDT", qty=1, price_hint=100, client_order_id="intent-1")

    assert result.order_id == "exchange-1"
    assert result.price == 100.5
    assert result.fee == 0.1
    request = exchange.calls[-1]
    assert request["type"] == "limit"
    assert request["price"] == 101.0
    assert request["params"] == {"timeInForce": "IOC", "newClientOrderId": "intent-1"}


def test_binance_nonterminal_order_is_uncertain_and_cannot_be_retried():
    broker = _broker(FakeExchange(status="open"))

    with pytest.raises(BrokerOrderUncertainError) as raised:
        broker.buy("BTC/USDT", qty=1, price_hint=100, client_order_id="intent-1")

    assert raised.value.external_order_id == "exchange-1"


def test_binance_precision_that_widens_limit_is_rejected_before_submission():
    exchange = FakeExchange(normalized_price="101.01")
    broker = _broker(exchange)

    with pytest.raises(BrokerOrderRejectedError, match="precision"):
        broker.buy("BTC/USDT", qty=1, price_hint=100, client_order_id="intent-1")

    assert exchange.calls == []


def test_binance_buy_rejects_worst_case_notional_above_hard_cap():
    exchange = FakeExchange()
    broker = _broker(exchange)
    broker.config.max_order_notional = 100.0

    with pytest.raises(BrokerOrderRejectedError, match="worst-case notional"):
        broker.buy("BTC/USDT", qty=1, price_hint=100, client_order_id="intent-1")

    assert exchange.calls == []


def test_binance_buy_persists_net_spendable_base_after_base_fee():
    broker = _broker(FakeExchange(fee={"cost": 0.01, "currency": "BTC"}))

    result = broker.buy("BTC/USDT", qty=1, price_hint=100, client_order_id="intent-1")

    assert result.qty == pytest.approx(0.99)
    assert result.price == 100.5
    assert result.fee == pytest.approx(1.005)


def test_binance_sell_reports_full_base_inventory_debit_after_base_fee():
    broker = _broker(FakeExchange(fee={"cost": 0.01, "currency": "BTC"}))

    result = broker.sell("BTC/USDT", qty=1, price_hint=100, client_order_id="intent-1")

    # The filled amount is 1 BTC, while the exchange also deducts 0.01 BTC as
    # commission. StateStore therefore sees the real 1.01 BTC inventory debit.
    assert result.qty == pytest.approx(1.01)
    assert result.fee == pytest.approx(1.005)


def test_binance_nonzero_fee_without_currency_requires_reconciliation():
    broker = _broker(FakeExchange(fee={"cost": 0.01, "currency": ""}))

    with pytest.raises(BrokerOrderUncertainError, match="fee currency") as raised:
        broker.buy("BTC/USDT", qty=1, price_hint=100, client_order_id="intent-1")

    assert raised.value.external_order_id == "exchange-1"


def test_binance_missing_fee_data_requires_reconciliation():
    broker = _broker(FakeExchange(include_fee=False))

    with pytest.raises(BrokerOrderUncertainError, match="omitted fee data") as raised:
        broker.buy("BTC/USDT", qty=1, price_hint=100, client_order_id="intent-1")

    assert raised.value.external_order_id == "exchange-1"


def test_binance_missing_order_fee_reconciles_matched_trade_fees():
    exchange = FakeExchange(
        include_fee=False,
        my_trades=[
            {
                "order": "exchange-1",
                "amount": 1.0,
                "cost": 100.5,
                "fee": {"cost": 0.01, "currency": "BTC"},
            }
        ],
    )
    broker = _broker(exchange)

    result = broker.buy("BTC/USDT", qty=1, price_hint=100, client_order_id="intent-1")

    assert result.qty == pytest.approx(0.99)
    assert result.fee == pytest.approx(1.005)
    assert exchange.my_trade_calls == [
        ("BTC/USDT", None, None, {"orderId": "exchange-1"})
    ]


def test_binance_unmatched_trade_fee_fallback_requires_reconciliation():
    broker = _broker(
        FakeExchange(
            include_fee=False,
            my_trades=[
                {
                    "order": "other-order",
                    "amount": 1.0,
                    "cost": 100.5,
                    "fee": {"cost": 0.01, "currency": "BTC"},
                }
            ],
        )
    )

    with pytest.raises(BrokerOrderUncertainError, match="unmatched") as raised:
        broker.buy("BTC/USDT", qty=1, price_hint=100, client_order_id="intent-1")

    assert raised.value.external_order_id == "exchange-1"
