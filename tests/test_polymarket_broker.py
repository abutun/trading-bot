import sys
import types
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

from brokers.polymarket_broker import (
    PolymarketBroker,
    PolymarketOrderUncertain,
)
from brokers.base import BrokerOrderRejectedError


class FakeConfig:
    polymarket_slippage_bps = 100
    max_order_slippage_bps = 100
    max_order_notional = 100

    @staticmethod
    def resolve_polymarket_market(symbol):
        if symbol != "example-yes":
            raise ValueError("unknown symbol")
        return {"token_id": "token-123"}


class FakeState:
    def __init__(self, positions=None):
        self.positions = positions or []

    def get_positions_by_venue(self, venue):
        assert venue == "polymarket"
        return self.positions


class FakeMarketOrderArgsV2:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeOrderType:
    FOK = "FOK"


class FakeAssetType:
    COLLATERAL = "COLLATERAL"


class FakeBalanceAllowanceParams:
    def __init__(self, *, asset_type):
        self.asset_type = asset_type


@dataclass
class FakeSignedOrder:
    makerAmount: str
    takerAmount: str


class FakeClient:
    def __init__(
        self,
        *,
        response,
        signed_order,
        tick_size="0.001",
        balance_response=None,
        post_error=None,
    ):
        self.response = response
        self.signed_order = signed_order
        self.tick_size = tick_size
        self.balance_response = balance_response or {"balance": "0"}
        self.post_error = post_error
        self.created_args = []
        self.posted = []
        self.balance_params = []

    def get_tick_size(self, token_id):
        assert token_id == "token-123"
        return self.tick_size

    def create_market_order(self, order_args):
        self.created_args.append(order_args)
        return self.signed_order

    def post_order(self, order, *, order_type):
        self.posted.append((order, order_type))
        if self.post_error:
            raise self.post_error
        return self.response

    def get_balance_allowance(self, *, params):
        self.balance_params.append(params)
        return self.balance_response


class RecordingApiCreds:
    def __init__(self, api_key, api_secret, api_passphrase):
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase


class RecordingClobClient:
    instances = []
    derived_creds = RecordingApiCreds("derived-key", "derived-secret", "derived-pass")

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.derived = False
        self.set_creds = None
        type(self).instances.append(self)

    def derive_api_key(self):
        self.derived = True
        return type(self).derived_creds

    def set_api_creds(self, creds):
        self.set_creds = creds


class PolymarketBrokerTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient(
            response={
                "success": True,
                "orderID": "buy-order",
                "status": "matched",
                "takingAmount": "20",
                "makingAmount": "10.1",
                "errorMsg": "",
            },
            signed_order=FakeSignedOrder("10100000", "20000000"),
        )
        self.broker = self._broker_with_client(self.client)

    @staticmethod
    def _broker_with_client(client, *, state=None):
        broker = object.__new__(PolymarketBroker)
        broker.config = FakeConfig()
        broker.state = state or FakeState()
        broker.client = client
        broker._asset_type = FakeAssetType
        broker._balance_allowance_params_cls = FakeBalanceAllowanceParams
        broker._market_order_args_cls = FakeMarketOrderArgsV2
        broker._order_type_fok = FakeOrderType.FOK
        return broker

    @staticmethod
    def _init_config(**overrides):
        values = {
            "polymarket_private_key": "0xabc",
            "polymarket_clob_host": "https://clob.polymarket.com",
            "polymarket_chain_id": 137,
            "polymarket_api_key": "configured-key",
            "polymarket_api_secret": "configured-secret",
            "polymarket_api_passphrase": "configured-pass",
            "polymarket_signature_type": 0,
            "polymarket_funder_address": "",
            "polymarket_derive_api_credentials": False,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    @staticmethod
    def _v2_sdk_modules():
        package = types.ModuleType("py_clob_client_v2")
        package.__path__ = []
        client_module = types.ModuleType("py_clob_client_v2.client")
        client_module.ClobClient = RecordingClobClient
        types_module = types.ModuleType("py_clob_client_v2.clob_types")
        types_module.ApiCreds = RecordingApiCreds
        types_module.AssetType = FakeAssetType
        types_module.BalanceAllowanceParams = FakeBalanceAllowanceParams
        types_module.MarketOrderArgsV2 = FakeMarketOrderArgsV2
        types_module.OrderType = FakeOrderType
        return {
            "py_clob_client_v2": package,
            "py_clob_client_v2.client": client_module,
            "py_clob_client_v2.clob_types": types_module,
        }

    def test_buy_uses_v2_market_order_metadata_and_exact_fill(self):
        result = self.broker.buy(
            "example-yes", qty=20, price_hint=0.5, client_order_id="intent-123"
        )

        self.assertEqual(result.qty, 20)
        self.assertEqual(result.price, 0.505)
        self.assertEqual(result.order_id, "buy-order")
        request = self.client.created_args[-1]
        self.assertEqual(request.token_id, "token-123")
        self.assertEqual(request.side, "BUY")
        self.assertEqual(request.order_type, "FOK")
        self.assertAlmostEqual(request.amount, 10.1)
        self.assertAlmostEqual(request.price, 0.505)
        self.assertTrue(request.metadata.startswith("0x"))
        self.assertEqual(len(request.metadata), 66)
        self.assertEqual(self.client.posted, [(self.client.signed_order, "FOK")])

    def test_sell_uses_v2_fok_floor_and_exact_signed_amounts(self):
        self.client.signed_order = FakeSignedOrder("20000000", "11880000")
        self.client.response = {
            "success": True,
            "orderID": "sell-order",
            "status": "matched",
            "makingAmount": "20",
            "takingAmount": "11.88",
            "errorMsg": "",
        }

        result = self.broker.sell("example-yes", qty=20, price_hint=0.6)

        self.assertEqual(result.qty, 20)
        self.assertEqual(result.price, 0.594)
        request = self.client.created_args[-1]
        self.assertEqual(request.side, "SELL")
        self.assertAlmostEqual(request.amount, 20)
        self.assertAlmostEqual(request.price, 0.594)
        self.assertEqual(self.client.posted[-1][1], "FOK")

    def test_fixed_point_response_amounts_are_accepted_only_when_exact(self):
        self.client.response.update(
            {"takingAmount": "20000000", "makingAmount": "10100000"}
        )

        result = self.broker.buy("example-yes", qty=20, price_hint=0.5)

        self.assertEqual(result.qty, 20)
        self.assertEqual(result.price, 0.505)

    def test_partial_fok_response_becomes_typed_uncertain_with_order_id(self):
        self.client.response["orderID"] = "partial-order"
        self.client.response["takingAmount"] = "19.99"

        with self.assertRaises(PolymarketOrderUncertain) as raised:
            self.broker.buy("example-yes", qty=20, price_hint=0.5)

        self.assertEqual(raised.exception.external_order_id, "partial-order")
        self.assertIn("partial-order", str(raised.exception))

    def test_rejected_response_is_never_reported_as_a_zero_fill(self):
        self.client.response = {
            "success": False,
            "orderID": "rejected-order",
            "status": "delayed",
            "errorMsg": "rate limit exceeded",
        }

        with self.assertRaises(PolymarketOrderUncertain) as raised:
            self.broker.buy("example-yes", qty=20, price_hint=0.5)

        self.assertEqual(raised.exception.external_order_id, "rejected-order")
        self.assertIn("rate limit exceeded", str(raised.exception))

    def test_non_terminal_success_response_is_uncertain(self):
        self.client.response["orderID"] = "delayed-order"
        self.client.response["status"] = "delayed"

        with self.assertRaises(PolymarketOrderUncertain) as raised:
            self.broker.buy("example-yes", qty=20, price_hint=0.5)

        self.assertEqual(raised.exception.external_order_id, "delayed-order")

    def test_malformed_success_response_without_order_id_is_uncertain(self):
        self.client.response.pop("orderID")

        with self.assertRaises(PolymarketOrderUncertain) as raised:
            self.broker.buy("example-yes", qty=20, price_hint=0.5)

        self.assertEqual(raised.exception.external_order_id, "")
        self.assertIn("omitted orderID", str(raised.exception))

    def test_invalid_fill_value_is_uncertain_and_retains_order_id(self):
        self.client.response["orderID"] = "bad-fill-order"
        self.client.response["takingAmount"] = "NaN"

        with self.assertRaises(PolymarketOrderUncertain) as raised:
            self.broker.buy("example-yes", qty=20, price_hint=0.5)

        self.assertEqual(raised.exception.external_order_id, "bad-fill-order")

    def test_transport_failure_after_signing_is_uncertain(self):
        self.client.post_error = OSError("connection reset")

        with self.assertRaises(PolymarketOrderUncertain) as raised:
            self.broker.buy("example-yes", qty=20, price_hint=0.5)

        self.assertEqual(raised.exception.external_order_id, "")
        self.assertIn("confirmation failed", str(raised.exception))

    def test_sell_refuses_non_closeable_fractional_share_remainder(self):
        with self.assertRaises(BrokerOrderRejectedError):
            self.broker.sell("example-yes", qty=20.001, price_hint=0.6)

        self.assertEqual(self.client.created_args, [])
        self.assertEqual(self.client.posted, [])

    def test_buy_rejects_worst_case_notional_above_hard_cap_before_signing(self):
        self.broker.config.max_order_notional = 10

        with self.assertRaises(BrokerOrderRejectedError) as raised:
            self.broker.buy("example-yes", qty=20, price_hint=0.5)

        self.assertIn("worst-case notional", str(raised.exception))
        self.assertEqual(self.client.created_args, [])
        self.assertEqual(self.client.posted, [])

    def test_buy_rejects_sdk_signed_quote_above_hard_cap_before_posting(self):
        # Request amount is 9.59 pUSD (19 * 0.505), but the compromised or
        # incompatible SDK payload would spend 10.1 pUSD. The exact signed
        # quote is checked immediately before the only side-effecting call.
        self.broker.config.max_order_notional = 10

        with self.assertRaises(BrokerOrderRejectedError) as raised:
            self.broker.buy("example-yes", qty=19, price_hint=0.5)

        self.assertIn("worst-case notional", str(raised.exception))
        self.assertEqual(len(self.client.created_args), 1)
        self.assertEqual(self.client.posted, [])

    def test_equity_uses_v2_balance_allowance_dictionary(self):
        self.client.balance_response = {"balance": "123456789", "allowances": {}}
        state = FakeState(
            [
                SimpleNamespace(
                    pair_id="polymarket:example-yes", qty=2, entry_price=0.4
                )
            ]
        )
        broker = self._broker_with_client(self.client, state=state)

        equity = broker.get_equity({"polymarket:example-yes": 0.5})

        self.assertAlmostEqual(equity, 124.456789)
        self.assertEqual(self.client.balance_params[-1].asset_type, "COLLATERAL")

    def test_malformed_balance_response_fails_closed(self):
        self.client.balance_response = {"allowances": {}}

        with self.assertRaises(RuntimeError):
            self.broker.get_equity({})

    def test_init_uses_configured_v2_credentials(self):
        RecordingClobClient.instances = []
        with patch.dict(sys.modules, self._v2_sdk_modules()):
            broker = PolymarketBroker(self._init_config(), FakeState())

        client = RecordingClobClient.instances[-1]
        self.assertIs(broker.client, client)
        self.assertEqual(client.kwargs["host"], "https://clob.polymarket.com")
        self.assertEqual(client.kwargs["chain_id"], 137)
        self.assertEqual(client.kwargs["signature_type"], 0)
        self.assertFalse(client.kwargs["retry_on_error"])
        self.assertEqual(client.kwargs["creds"].api_key, "configured-key")
        self.assertFalse(client.derived)

    def test_init_derives_only_when_explicitly_enabled(self):
        RecordingClobClient.instances = []
        config = self._init_config(
            polymarket_api_key="",
            polymarket_api_secret="",
            polymarket_api_passphrase="",
            polymarket_derive_api_credentials=True,
        )
        with patch.dict(sys.modules, self._v2_sdk_modules()):
            PolymarketBroker(config, FakeState())

        client = RecordingClobClient.instances[-1]
        self.assertTrue(client.derived)
        self.assertIs(client.set_creds, RecordingClobClient.derived_creds)

    def test_init_rejects_partial_credentials_even_with_derivation_enabled(self):
        RecordingClobClient.instances = []
        config = self._init_config(
            polymarket_api_secret="",
            polymarket_derive_api_credentials=True,
        )
        with patch.dict(sys.modules, self._v2_sdk_modules()):
            with self.assertRaises(ValueError):
                PolymarketBroker(config, FakeState())

        self.assertEqual(RecordingClobClient.instances, [])

    def test_init_requires_explicit_derivation_when_credentials_are_absent(self):
        RecordingClobClient.instances = []
        config = self._init_config(
            polymarket_api_key="",
            polymarket_api_secret="",
            polymarket_api_passphrase="",
        )
        with patch.dict(sys.modules, self._v2_sdk_modules()):
            with self.assertRaises(ValueError):
                PolymarketBroker(config, FakeState())

        self.assertEqual(RecordingClobClient.instances, [])

    def test_init_requires_funder_for_non_eoa_signature(self):
        RecordingClobClient.instances = []
        config = self._init_config(polymarket_signature_type=3)
        with patch.dict(sys.modules, self._v2_sdk_modules()):
            with self.assertRaises(ValueError):
                PolymarketBroker(config, FakeState())

        self.assertEqual(RecordingClobClient.instances, [])


if __name__ == "__main__":
    unittest.main()
