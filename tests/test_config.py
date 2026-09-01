import os
import unittest
from unittest.mock import patch

from config import Config, LIVE_TRADING_CONFIRMATION


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
            config = Config.from_env(load_dotenv_file=False)

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
                Config.from_env(load_dotenv_file=False)

        env["TRADING_PAIRS"] = "polymarket:example-yes"
        env["POLYMARKET_MARKETS"] = "not json"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "valid JSON"):
                Config.from_env(load_dotenv_file=False)

    def test_live_mode_requires_explicit_confirmation_and_ioc_binance(self):
        env = {
            "DEPLOYMENT_ENV": "production",
            "BOT_MODE": "live",
            "TRADING_PAIRS": "binance:BTC/USDT",
            "POSTGRES_USER": "bot",
            "POSTGRES_PASSWORD": "secret",
            "POSTGRES_SSLMODE": "verify-full",
            "POSTGRES_SSLROOTCERT": "/run/secrets/postgres-ca.pem",
            "BINANCE_API_KEY": "live-api-key",
            "BINANCE_API_SECRET": "live-api-secret",
            "BINANCE_ORDER_MODE": "ioc_limit",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "LIVE_TRADING_CONFIRMATION"):
                Config.from_env(load_dotenv_file=False)

        env["LIVE_TRADING_CONFIRMATION"] = LIVE_TRADING_CONFIRMATION
        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env(load_dotenv_file=False)
        self.assertEqual(config.mode, "live")

    def test_live_mode_requires_production_environment(self):
        env = {
            "DEPLOYMENT_ENV": "staging",
            "BOT_MODE": "live",
            "LIVE_TRADING_CONFIRMATION": LIVE_TRADING_CONFIRMATION,
            "TRADING_PAIRS": "binance:BTC/USDT",
            "POSTGRES_USER": "bot",
            "POSTGRES_PASSWORD": "secret",
            "BINANCE_API_KEY": "live-api-key",
            "BINANCE_API_SECRET": "live-api-secret",
            "BINANCE_ORDER_MODE": "ioc_limit",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "DEPLOYMENT_ENV=production"):
                Config.from_env(load_dotenv_file=False)

    def test_rejects_misspelled_boolean_instead_of_flipping_to_mainnet(self):
        env = {
            "TRADING_PAIRS": "binance:BTC/USDT",
            "POSTGRES_USER": "bot",
            "POSTGRES_PASSWORD": "secret",
            "BINANCE_TESTNET": "ture",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "BINANCE_TESTNET"):
                Config.from_env(load_dotenv_file=False)

    def test_production_rejects_insecure_database_and_dashboard_defaults(self):
        env = {
            "DEPLOYMENT_ENV": "production",
            "BOT_MODE": "paper",
            "TRADING_PAIRS": "binance:BTC/USDT",
            "POSTGRES_USER": "bot",
            "POSTGRES_PASSWORD": "secret",
            "POSTGRES_SSLMODE": "prefer",
            "DASHBOARD_USERNAME": "admin",
            "DASHBOARD_PASSWORD": "short",
            "DASHBOARD_SECRET_KEY": "short",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "DASHBOARD_SECURE_COOKIES"):
                Config.from_env(load_dotenv_file=False, runtime_role="dashboard")

    def test_production_dsn_must_carry_its_own_secure_sslmode(self):
        env = {
            "DEPLOYMENT_ENV": "production",
            "BOT_MODE": "paper",
            "TRADING_PAIRS": "binance:BTC/USDT",
            "POSTGRES_DSN": "postgresql://bot:secret@db.internal/trading_bot",
            "POSTGRES_SSLMODE": "require",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "sslmode=verify-full"):
                Config.from_env(load_dotenv_file=False, runtime_role="bot")

    def test_production_dsn_cannot_hide_insecure_duplicate_sslmode(self):
        env = {
            "DEPLOYMENT_ENV": "production",
            "BOT_MODE": "paper",
            "TRADING_PAIRS": "binance:BTC/USDT",
            "POSTGRES_DSN": (
                "postgresql://bot:secret@db.internal/trading_bot?"
                "sslmode=verify-full&sslmode=disable&sslrootcert=/run/secrets/postgres-ca.pem"
            ),
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "sslmode=verify-full"):
                Config.from_env(load_dotenv_file=False, runtime_role="bot")

    def test_dashboard_rejects_trading_credentials_and_public_placeholders(self):
        env = {
            "DEPLOYMENT_ENV": "production",
            "BOT_MODE": "paper",
            "TRADING_PAIRS": "binance:BTC/USDT",
            "POSTGRES_USER": "dashboard",
            "POSTGRES_PASSWORD": "replace-me",
            "POSTGRES_SSLMODE": "verify-full",
            "POSTGRES_SSLROOTCERT": "/run/secrets/postgres-ca.pem",
            "DASHBOARD_SECURE_COOKIES": "true",
            "DASHBOARD_USERNAME": "replace-with-a-non-default-user",
            "DASHBOARD_PASSWORD": "replace-with-at-least-16-characters",
            "DASHBOARD_SECRET_KEY": "replace-with-at-least-32-characters",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "DASHBOARD_USERNAME"):
                Config.from_env(load_dotenv_file=False, runtime_role="dashboard")

        env.update(
            {
                "POSTGRES_PASSWORD": "dashboard-db-password",
                "DASHBOARD_USERNAME": "operator",
                "DASHBOARD_PASSWORD": "a-unique-dashboard-password",
                "DASHBOARD_SECRET_KEY": "a-unique-32-character-dashboard-secret",
                "BINANCE_API_SECRET": "should-not-reach-dashboard",
            }
        )
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "trading credentials"):
                Config.from_env(load_dotenv_file=False, runtime_role="dashboard")

    def test_dashboard_does_not_load_a_global_secret_manager(self):
        env = {
            "SECRET_MANAGER": "aws",
            "TRADING_PAIRS": "binance:BTC/USDT",
            "POSTGRES_USER": "dashboard",
            "POSTGRES_PASSWORD": "dashboard-db-password",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "global SECRET_MANAGER"):
                Config.from_env(load_dotenv_file=False, runtime_role="dashboard")

    def test_pair_id_reserves_room_for_meta_keys(self):
        symbol = "X" * 107  # 'binance:' + 107 = 115, beyond the 128-char meta key.
        env = {
            "TRADING_PAIRS": f"binance:{symbol}",
            "POSTGRES_USER": "bot",
            "POSTGRES_PASSWORD": "secret",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "database limits"):
                Config.from_env(load_dotenv_file=False)
