import os
import unittest
from unittest.mock import patch

from config import Config


class ConfigTests(unittest.TestCase):
    def test_parses_polymarket_pair_and_market_mapping(self):
        env = {
            "BOT_MODE": "paper",
            "TRADING_PAIRS": "binance:BTC/USDT,polymarket:example-yes",
            "POSTGRES_USER": "bot",
            "POSTGRES_PASSWORD": "secret",
            "POLYMARKET_MARKETS": '{"example-yes":{"token_id":"123"}}',
        }
        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env()

        self.assertEqual(config.trading_pairs[-1].pair_id, "polymarket:example-yes")
        self.assertEqual(config.resolve_polymarket_market("example-yes")["token_id"], "123")
        self.assertIn("dbname=trading_bot", config.database_dsn)

    def test_rejects_invalid_pair_and_json_mapping(self):
        env = {
            "TRADING_PAIRS": "not-a-pair",
            "POSTGRES_USER": "bot",
            "POSTGRES_PASSWORD": "secret",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "venue:symbol"):
                Config.from_env()

        env["TRADING_PAIRS"] = "polymarket:example-yes"
        env["POLYMARKET_MARKETS"] = "not json"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "valid JSON"):
                Config.from_env()
