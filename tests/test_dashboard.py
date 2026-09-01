import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from werkzeug.middleware.proxy_fix import ProxyFix

from dashboard.app import HEARTBEAT_KEY, create_app
from models import Position


class FakeState:
    def __init__(self, *, meta=None, positions=None, fail_meta=False):
        self.meta = dict(meta or {})
        self.positions = list(positions or [])
        self.fail_meta = fail_meta
        self.meta_reads = []
        self.closed = 0

    def get_meta(self, key, default=None):
        self.meta_reads.append(key)
        if self.fail_meta:
            raise RuntimeError("database unavailable")
        return self.meta.get(key, default)

    def get_all_positions(self):
        return self.positions

    def get_equity_history(self, limit=2000):
        return []

    def get_recent_trades(self, limit=30):
        return []

    def get_unresolved_orders(self):
        return []

    def close(self):
        self.closed += 1


def _config(**overrides):
    values = {
        "deployment_env": "development",
        "dashboard_username": "operator",
        "dashboard_password": "correct horse battery staple",
        "dashboard_secret_key": "x" * 48,
        "dashboard_secure_cookies": False,
        "dashboard_heartbeat_stale_seconds": 180,
        "dashboard_login_max_attempts": 5,
        "dashboard_login_window_seconds": 300,
        "dashboard_login_lockout_seconds": 900,
        "trusted_proxy_count": 0,
        "loop_seconds": 60,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class DashboardTests(unittest.TestCase):
    def make_client(self, state=None, **config_overrides):
        state = state or FakeState()
        app = create_app(
            _config(**config_overrides), state_factory=lambda _config: state
        )
        app.config.update(TESTING=True)
        return app.test_client(), state, app

    @staticmethod
    def csrf_token(client):
        response = client.get("/login")
        assert response.status_code == 200
        with client.session_transaction() as stored_session:
            return stored_session["csrf_token"]

    def login(self, client):
        token = self.csrf_token(client)
        response = client.post(
            "/login",
            data={
                "csrf_token": token,
                "username": "operator",
                "password": "correct horse battery staple",
            },
        )
        self.assertEqual(response.status_code, 302)

    def test_health_is_liveness_and_readiness_requires_db_heartbeat(self):
        now = datetime.now(timezone.utc).isoformat()
        state = FakeState(meta={HEARTBEAT_KEY: now})
        client, state, _app = self.make_client(state)

        self.assertEqual(client.get("/healthz").status_code, 200)
        ready = client.get("/readyz")
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.get_json(), {"status": "ready"})

        state.meta[HEARTBEAT_KEY] = (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).isoformat()
        self.assertEqual(client.get("/readyz").status_code, 503)

        state.fail_meta = True
        self.assertEqual(client.get("/healthz").status_code, 200)
        self.assertEqual(client.get("/readyz").status_code, 503)

    def test_security_headers_and_proxy_trust_are_explicit(self):
        client, _state, app = self.make_client(
            deployment_env="production",
            dashboard_secure_cookies=True,
            trusted_proxy_count=1,
        )
        response = client.get("/healthz")

        self.assertIn(
            "script-src 'self' 'unsafe-inline'",
            response.headers["Content-Security-Policy"],
        )
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("Strict-Transport-Security", response.headers)
        self.assertIsInstance(app.wsgi_app, ProxyFix)

        local_client, _state, _app = self.make_client(dashboard_secure_cookies=False)
        self.assertNotIn(
            "Strict-Transport-Security", local_client.get("/healthz").headers
        )

        staging_client, _state, _app = self.make_client(
            deployment_env="staging", dashboard_secure_cookies=True
        )
        self.assertNotIn(
            "Strict-Transport-Security", staging_client.get("/healthz").headers
        )

    def test_login_lockout_and_session_rotation(self):
        client, _state, _app = self.make_client(
            dashboard_login_max_attempts=2,
            dashboard_login_lockout_seconds=60,
        )
        token = self.csrf_token(client)
        with client.session_transaction() as stored_session:
            stored_session["pre_auth_value"] = "must be discarded"
            original_csrf = stored_session["csrf_token"]

        first = client.post(
            "/login",
            data={"csrf_token": token, "username": "operator", "password": "wrong"},
        )
        self.assertEqual(first.status_code, 200)
        locked = client.post(
            "/login",
            data={"csrf_token": token, "username": "operator", "password": "wrong"},
        )
        self.assertEqual(locked.status_code, 429)
        self.assertEqual(locked.headers["Retry-After"], "60")

        still_locked = client.post(
            "/login",
            data={
                "csrf_token": token,
                "username": "operator",
                "password": "correct horse battery staple",
            },
        )
        self.assertEqual(still_locked.status_code, 429)

        # A fresh client verifies that successful authentication clears all
        # pre-authentication session material and issues a fresh CSRF nonce.
        client, _state, _app = self.make_client()
        token = self.csrf_token(client)
        with client.session_transaction() as stored_session:
            stored_session["pre_auth_value"] = "must be discarded"
            original_csrf = stored_session["csrf_token"]
        success = client.post(
            "/login",
            data={
                "csrf_token": token,
                "username": "operator",
                "password": "correct horse battery staple",
            },
        )
        self.assertEqual(success.status_code, 302)
        with client.session_transaction() as stored_session:
            self.assertTrue(stored_session["authenticated"])
            self.assertIn("session_nonce", stored_session)
            self.assertNotIn("pre_auth_value", stored_session)
            self.assertNotEqual(stored_session["csrf_token"], original_csrf)

    def test_login_rejects_encoded_open_redirects(self):
        client, _state, _app = self.make_client()
        token = self.csrf_token(client)
        response = client.post(
            "/login",
            data={
                "csrf_token": token,
                "username": "operator",
                "password": "correct horse battery staple",
                "next": "/%2f%2fevil.example",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")

    def test_dashboard_uses_persisted_price_and_marks_stale_or_unknown(self):
        position = Position(
            pair_id="binance:BTC/USDT",
            venue="binance",
            symbol="BTC/USDT",
            qty=2.0,
            entry_price=100.0,
        )
        now = datetime.now(timezone.utc)
        state = FakeState(
            positions=[position],
            meta={
                HEARTBEAT_KEY: now.isoformat(),
                "last_price:binance:BTC/USDT": json.dumps(
                    {"price": 123.45, "ts": now.isoformat(), "source": "binance"}
                ),
            },
        )
        client, state, _app = self.make_client(state)
        self.login(client)
        fresh = client.get("/")
        self.assertEqual(fresh.status_code, 200)
        self.assertIn(b"123.4500", fresh.data)
        self.assertIn(b">fresh<", fresh.data)
        self.assertIn(b"binance", fresh.data)
        self.assertIn("last_price:binance:BTC/USDT", state.meta_reads)

        state.meta["last_price:binance:BTC/USDT"] = json.dumps(
            {
                "price": 123.45,
                "ts": (now - timedelta(hours=1)).isoformat(),
                "source": "binance",
            }
        )
        stale = client.get("/")
        self.assertIn(b">stale<", stale.data)

        state.meta.pop("last_price:binance:BTC/USDT")
        unavailable = client.get("/")
        self.assertIn(b">Unavailable<", unavailable.data)

    def test_index_contains_no_market_data_client(self):
        with open("dashboard/app.py", encoding="utf-8") as source_file:
            source = source_file.read()
        self.assertNotIn("fetch_ohlcv", source)
        self.assertNotIn("from market_data import MarketData", source)


if __name__ == "__main__":
    unittest.main()
