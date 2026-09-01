from types import SimpleNamespace

import pytest

from brokers.base import BrokerOrderRejectedError, BrokerOrderUncertainError
from brokers.evm_broker import EVMBroker


def _broker():
    broker = object.__new__(EVMBroker)
    broker.config = SimpleNamespace(
        max_order_slippage_bps=100,
        evm_slippage_bps=50,
        max_order_notional=100,
    )
    return broker


def test_evm_expected_quote_cannot_exceed_reference_slippage_cap():
    broker = _broker()

    with pytest.raises(BrokerOrderRejectedError, match="exceeds"):
        broker._assert_expected_price("buy", expected_price=101.01, price_hint=100)

    broker._assert_expected_price("buy", expected_price=101.0, price_hint=100)
    broker._assert_expected_price("sell", expected_price=99.0, price_hint=100)


def test_evm_base_unit_conversion_rounds_down_without_float_overrun():
    assert EVMBroker._to_units(1.23456789, 6) == 1_234_567
    assert EVMBroker._to_units(0.0, 6) == 0


def test_evm_buy_amount_out_min_never_compounds_global_and_local_slippage():
    broker = _broker()

    # A 100 USDC buy quoted at 100.50 USDC/base is still inside the 1% global
    # cap for a 100 USDC reference. Applying the additional 0.5% local buffer
    # to this edge quote would widen the final fill beyond the global cap.
    minimum = broker._amount_out_minimum(
        side="buy",
        amount_in_wei=100_000_000,
        expected_out_wei=995_025,
        input_decimals=6,
        output_decimals=6,
        price_hint=100.0,
    )

    # ceil(100 / 101) in six-decimal base units; it is stricter than the
    # quote-local 0.5% deterioration floor of 990_050 units.
    assert minimum == 990_100


def test_evm_sell_amount_out_min_keeps_global_reference_floor_at_quote_edge():
    broker = _broker()

    # The executable quote is exactly at the 1% global sell floor. Reducing it
    # by local slippage would compound the limits, so the reference floor wins.
    minimum = broker._amount_out_minimum(
        side="sell",
        amount_in_wei=1_000_000,
        expected_out_wei=99_000_000,
        input_decimals=6,
        output_decimals=6,
        price_hint=100.0,
    )

    assert minimum == 99_000_000


def test_evm_amount_out_min_honors_tighter_local_quote_deterioration_cap():
    broker = _broker()

    minimum = broker._amount_out_minimum(
        side="buy",
        amount_in_wei=100_000_000,
        expected_out_wei=1_000_000,
        input_decimals=6,
        output_decimals=6,
        price_hint=100.0,
    )

    # The quote implies a 100 price; 0.5% local deterioration is stricter than
    # the 1% global reference allowance.
    assert minimum == 995_000


def test_evm_buy_rejects_actual_rounded_quote_input_above_hard_cap():
    broker = _broker()
    broker._mapping = lambda _symbol: {"base": "base", "quote": "quote"}
    broker._decimals = lambda _address: 6

    with pytest.raises(BrokerOrderRejectedError, match="quote notional"):
        broker.buy("BASE/QUOTE", qty=1.01, price_hint=100)


def test_evm_buy_hard_cap_uses_token_unit_rounding_not_float_product():
    broker = _broker()

    # The fraction below one quote-token base unit is not sent on-chain, so
    # the true input remains exactly at the 100-unit cap.
    quote_in = broker._quote_in_units(1.000000009, 100, 6)
    assert quote_in == 100_000_000
    broker._assert_buy_notional(quote_in, 6)


def test_evm_mined_revert_is_uncertain_and_retains_transaction_hash():
    class FakeEth:
        @staticmethod
        def wait_for_transaction_receipt(_tx_hash, timeout):
            assert timeout == 30
            return {"status": 0, "blockNumber": 123}

    broker = object.__new__(EVMBroker)
    broker.config = SimpleNamespace(evm_receipt_timeout_seconds=30, evm_min_confirmations=1)
    broker.w3 = SimpleNamespace(eth=FakeEth())

    with pytest.raises(BrokerOrderUncertainError, match="reverted after broadcast") as raised:
        broker._wait_for_confirmations("raw-hash", "0xdeadbeef")

    assert raised.value.external_order_id == "0xdeadbeef"
