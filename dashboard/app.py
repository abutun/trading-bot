from __future__ import annotations

import hmac
import json
import logging
import math
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any
from urllib.parse import unquote, urlparse

from flask import (
    Flask,
    abort,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from config import Config
from state import StateStore

logger = logging.getLogger(__name__)

HEARTBEAT_KEY = "bot_heartbeat"
LAST_PRICE_PREFIX = "last_price:"
HEARTBEAT_STALE_SECONDS = 180
DEFAULT_LOGIN_MAX_ATTEMPTS = 5
DEFAULT_LOGIN_WINDOW_SECONDS = 300
DEFAULT_LOGIN_LOCKOUT_SECONDS = 900
DEFAULT_LOGIN_RATE_LIMIT_ENTRIES = 10_000

# The templates intentionally contain a small amount of inline CSS and chart
# JavaScript. Keep this policy explicit rather than silently allowing remote
# assets or plugin content to execute in the dashboard.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'"
)


def _as_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return (
            value.astimezone(timezone.utc)
            if value.tzinfo
            else value.replace(tzinfo=timezone.utc)
        )
    parsed = datetime.fromisoformat(value)
    return (
        parsed.astimezone(timezone.utc)
        if parsed.tzinfo
        else parsed.replace(tzinfo=timezone.utc)
    )


def _config_int(
    config: Any,
    names: tuple[str, ...],
    default: int,
    *,
    minimum: int = 0,
) -> int:
    """Read an optional dashboard setting without breaking old configs."""

    for name in names:
        raw = getattr(config, name, None)
        if raw is None or raw == "":
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid dashboard setting %s", name)
            continue
        if value >= minimum:
            return value
        logger.warning("Ignoring dashboard setting %s below %s", name, minimum)
    return default


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class LoginRateLimiter:
    """Small, bounded, process-local login lockout tracker.

    The dashboard deliberately has no external cache dependency. The lockout
    applies within each Gunicorn worker, so deployments should also rate-limit
    this endpoint at their reverse proxy/WAF when using multiple workers.
    """

    def __init__(
        self,
        *,
        max_attempts: int,
        window_seconds: int,
        lockout_seconds: int,
        max_entries: int,
    ) -> None:
        self.max_attempts = max(1, max_attempts)
        self.window_seconds = max(1, window_seconds)
        self.lockout_seconds = max(1, lockout_seconds)
        self.max_entries = max(100, max_entries)
        self._attempts: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._last_seen: dict[str, float] = {}
        self._lock = threading.RLock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        for key, attempts in list(self._attempts.items()):
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if not attempts:
                self._attempts.pop(key, None)
                if key not in self._locked_until:
                    self._last_seen.pop(key, None)

        for key, locked_until in list(self._locked_until.items()):
            if locked_until <= now:
                self._locked_until.pop(key, None)
                if key not in self._attempts:
                    self._last_seen.pop(key, None)

        # A public endpoint must not allow an unbounded in-memory keyspace.
        keys = set(self._attempts) | set(self._locked_until)
        if len(keys) > self.max_entries:
            oldest = sorted(keys, key=lambda key: self._last_seen.get(key, 0.0))
            for key in oldest[: len(keys) - self.max_entries]:
                self._attempts.pop(key, None)
                self._locked_until.pop(key, None)
                self._last_seen.pop(key, None)

    def retry_after(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            remaining = self._locked_until.get(key, 0.0) - now
            if remaining > 0:
                self._last_seen[key] = now
                return max(1, math.ceil(remaining))
            return 0

    def record_failure(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            attempts = self._attempts.setdefault(key, deque())
            attempts.append(now)
            self._last_seen[key] = now
            if len(attempts) < self.max_attempts:
                return 0

            self._attempts.pop(key, None)
            self._locked_until[key] = now + self.lockout_seconds
            return self.lockout_seconds

    def reset(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)
            self._locked_until.pop(key, None)
            self._last_seen.pop(key, None)


def create_app(
    config: Config | None = None,
    *,
    state_factory: Callable[[Config], StateStore] | None = None,
) -> Flask:
    """Build the dashboard WSGI application.

    StateStore connections are made lazily per Flask request, rather than in
    the import/factory process. This is safe with Gunicorn workers and also
    avoids a preloaded master process handing a live PostgreSQL connection to
    forked workers.
    """

    config = config or Config.from_env(runtime_role="dashboard")
    if not config.dashboard_password:
        raise RuntimeError("DASHBOARD_PASSWORD is required to run the dashboard")
    if not config.dashboard_secret_key:
        raise RuntimeError("DASHBOARD_SECRET_KEY is required to run the dashboard")

    app = Flask(__name__)
    app.secret_key = config.dashboard_secret_key

    secure_cookies = bool(getattr(config, "dashboard_secure_cookies", False))
    hsts_enabled = (
        secure_cookies
        and getattr(config, "deployment_env", "development") == "production"
    )
    app.config.update(
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        SESSION_COOKIE_NAME=str(
            getattr(config, "dashboard_session_cookie_name", "trading_bot_dashboard")
        ),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure_cookies,
        SESSION_REFRESH_EACH_REQUEST=False,
        MAX_CONTENT_LENGTH=16 * 1024,
    )

    trusted_proxy_count = _config_int(
        config,
        ("trusted_proxy_count", "dashboard_trusted_proxy_count"),
        0,
        minimum=0,
    )
    if trusted_proxy_count:
        # Do not trust any forwarding headers unless the operator explicitly
        # says how many proxies sit in front of this process.
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=trusted_proxy_count,
            x_proto=trusted_proxy_count,
        )

    heartbeat_stale_seconds = _config_int(
        config,
        ("dashboard_heartbeat_stale_seconds", "heartbeat_stale_seconds"),
        HEARTBEAT_STALE_SECONDS,
        minimum=1,
    )
    default_price_stale_seconds = max(
        HEARTBEAT_STALE_SECONDS,
        max(1, _config_int(config, ("loop_seconds",), 60, minimum=1)) * 3,
    )
    price_stale_seconds = _config_int(
        config,
        ("dashboard_price_stale_seconds", "price_stale_seconds"),
        default_price_stale_seconds,
        minimum=1,
    )
    login_limiter = LoginRateLimiter(
        max_attempts=_config_int(
            config,
            ("dashboard_login_max_attempts", "login_max_attempts"),
            DEFAULT_LOGIN_MAX_ATTEMPTS,
            minimum=1,
        ),
        window_seconds=_config_int(
            config,
            ("dashboard_login_window_seconds", "login_window_seconds"),
            DEFAULT_LOGIN_WINDOW_SECONDS,
            minimum=1,
        ),
        lockout_seconds=_config_int(
            config,
            ("dashboard_login_lockout_seconds", "login_lockout_seconds"),
            DEFAULT_LOGIN_LOCKOUT_SECONDS,
            minimum=1,
        ),
        max_entries=_config_int(
            config,
            ("dashboard_login_rate_limit_entries",),
            DEFAULT_LOGIN_RATE_LIMIT_ENTRIES,
            minimum=100,
        ),
    )
    app.extensions["dashboard_rate_limiter"] = login_limiter

    def get_state() -> StateStore:
        state = getattr(g, "_dashboard_state", None)
        if state is None:
            if state_factory is not None:
                state = state_factory(config)
            else:
                try:
                    # The dashboard is a read-only state consumer.  This lets
                    # production use a PostgreSQL role without CREATE/ALTER
                    # privileges once the bot/migration has installed schema.
                    state = StateStore(config, initialize_schema=False)
                except TypeError as exc:
                    # Compatibility for an older StateStore during a rolling
                    # upgrade.  Do not swallow unrelated construction errors.
                    if "initialize_schema" not in str(exc):
                        raise
                    logger.warning(
                        "StateStore lacks initialize_schema; dashboard is using "
                        "the legacy constructor"
                    )
                    state = StateStore(config)
            g._dashboard_state = state
        return state

    @app.teardown_appcontext
    def close_state(_error: BaseException | None) -> None:
        state = g.pop("_dashboard_state", None)
        close = getattr(state, "close", None)
        if callable(close):
            close()

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy",
            "accelerometer=(), camera=(), geolocation=(), microphone=(), payment=()",
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault("Cache-Control", "no-store")
        if hsts_enabled:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    def csrf_token() -> str:
        token = session.get("csrf_token")
        if not isinstance(token, str) or not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    def client_ip() -> str:
        # ProxyFix normalizes this value only when trusted_proxy_count is set.
        return (request.remote_addr or "unknown")[:128]

    def validate_csrf() -> None:
        token = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not isinstance(expected, str) or not hmac.compare_digest(token, expected):
            logger.warning(
                "dashboard_csrf_rejected remote_addr=%s path=%s",
                client_ip(),
                request.path,
            )
            abort(400, "Invalid CSRF token")

    def login_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not session.get("authenticated"):
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)

        return wrapper

    def heartbeat_is_fresh(heartbeat_raw: Any) -> bool:
        if not heartbeat_raw:
            return False
        try:
            age_seconds = (
                datetime.now(timezone.utc) - _as_utc(heartbeat_raw)
            ).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return False
        # A small amount of clock skew is normal. A heartbeat substantially in
        # the future is not evidence that the bot is healthy.
        return -60 <= age_seconds <= heartbeat_stale_seconds

    def probe_state() -> tuple[bool, bool]:
        """Return database reachability and heartbeat freshness without leaks."""

        try:
            heartbeat = get_state().get_meta(HEARTBEAT_KEY, "")
        except Exception:
            logger.exception("Dashboard state probe failed")
            return False, False
        return True, heartbeat_is_fresh(heartbeat)

    def safe_next_url() -> str:
        target = (request.form.get("next") or request.args.get("next") or "").strip()
        parsed = urlparse(target)
        decoded_target = unquote(target)
        if (
            target.startswith("/")
            and not decoded_target.startswith(("//", "/\\"))
            and not parsed.scheme
            and not parsed.netloc
            and "\r" not in decoded_target
            and "\n" not in decoded_target
        ):
            return target
        return url_for("index")

    def login_response(
        error: str | None = None,
        *,
        status: int = 200,
        retry_after: int | None = None,
    ):
        response = make_response(
            render_template(
                "login.html",
                error=error,
                csrf_token=csrf_token(),
                next_url=request.form.get("next") or request.args.get("next") or "",
            ),
            status,
        )
        if retry_after:
            response.headers["Retry-After"] = str(retry_after)
        return response

    def persisted_price(state: StateStore, pair_id: str) -> dict[str, Any]:
        """Load the bot-written price envelope without ever contacting a venue."""

        raw = state.get_meta(f"{LAST_PRICE_PREFIX}{pair_id}", "")
        if not raw:
            return {
                "price": None,
                "status": "unavailable",
                "timestamp": None,
                "source": None,
                "message": "No persisted market price",
            }

        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise TypeError("price envelope is not an object")
            price = _finite_number(payload.get("price"))
            timestamp = _as_utc(payload["ts"])
            if price is None or price <= 0:
                raise ValueError("price is not a positive finite number")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, OverflowError):
            logger.warning("Ignoring malformed persisted price for %s", pair_id)
            return {
                "price": None,
                "status": "unavailable",
                "timestamp": None,
                "source": None,
                "message": "Persisted market price is invalid",
            }

        source = payload.get("source")
        source = str(source)[:80] if source else None
        age_seconds = (datetime.now(timezone.utc) - timestamp).total_seconds()
        if age_seconds < -60 or age_seconds > price_stale_seconds:
            return {
                "price": price,
                "status": "stale",
                "timestamp": timestamp.isoformat(),
                "source": source,
                "message": "Persisted market price is stale",
            }
        return {
            "price": price,
            "status": "fresh",
            "timestamp": timestamp.isoformat(),
            "source": source,
            "message": "Persisted market price",
        }

    @app.get("/healthz")
    def healthz():
        # This is deliberately process-only liveness.  A transient database or
        # bot outage belongs in /readyz; otherwise an orchestrator can create a
        # restart loop that hides the real incident.
        return jsonify(status="ok"), 200

    @app.get("/readyz")
    def readyz():
        # The dashboard is ready for operators only when it can reach durable
        # state and the trading process has written a recent heartbeat.
        database_ok, bot_online = probe_state()
        if not database_ok or not bot_online:
            return jsonify(status="not_ready"), 503
        return jsonify(status="ready"), 200

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            if session.get("authenticated"):
                return redirect(safe_next_url())
            return login_response()

        # Keep CSRF validation ahead of credential and rate-limit handling.
        validate_csrf()
        remote_addr = client_ip()
        retry_after = login_limiter.retry_after(remote_addr)
        if retry_after:
            logger.warning(
                "dashboard_auth_lockout remote_addr=%s retry_after=%s",
                remote_addr,
                retry_after,
            )
            return login_response(
                "Too many failed sign-in attempts. Please try again later.",
                status=429,
                retry_after=retry_after,
            )

        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        correct_username = hmac.compare_digest(username, config.dashboard_username)
        correct_password = hmac.compare_digest(password, config.dashboard_password)
        if correct_username and correct_password:
            login_limiter.reset(remote_addr)
            # Flask's signed cookie session has no server-side identifier to
            # rotate. Clearing it and adding fresh random material produces a
            # new signed session payload and invalidates pre-authentication
            # state, preventing fixation of the prior session contents.
            session.clear()
            session["authenticated"] = True
            session["session_nonce"] = secrets.token_urlsafe(32)
            session.permanent = True
            csrf_token()
            logger.info("dashboard_auth_success remote_addr=%s", remote_addr)
            return redirect(safe_next_url())

        retry_after = login_limiter.record_failure(remote_addr)
        logger.warning(
            "dashboard_auth_failure remote_addr=%s locked=%s",
            remote_addr,
            bool(retry_after),
        )
        if retry_after:
            return login_response(
                "Too many failed sign-in attempts. Please try again later.",
                status=429,
                retry_after=retry_after,
            )
        return login_response("Invalid username or password.")

    @app.post("/logout")
    @login_required
    def logout():
        validate_csrf()
        session.clear()
        logger.info("dashboard_auth_logout remote_addr=%s", client_ip())
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def index():
        state = get_state()
        try:
            positions = state.get_all_positions()
            enriched = []
            total_unrealized = 0.0
            total_unrealized_status = "fresh"
            for pos in positions:
                quote = persisted_price(state, pos.pair_id)
                price = quote["price"]
                unrealized: float | None = None
                if price is not None:
                    unrealized = (price - pos.entry_price) * pos.qty
                    total_unrealized += unrealized
                    if (
                        quote["status"] == "stale"
                        and total_unrealized_status != "unavailable"
                    ):
                        total_unrealized_status = "stale"
                else:
                    total_unrealized_status = "unavailable"

                enriched.append(
                    {
                        "pair_id": pos.pair_id,
                        "venue": pos.venue,
                        "symbol": pos.symbol,
                        "qty": pos.qty,
                        "entry_price": pos.entry_price,
                        "price": price,
                        "price_status": quote["status"],
                        "price_timestamp": quote["timestamp"],
                        "price_source": quote["source"],
                        "price_message": quote["message"],
                        "unrealized_pnl": unrealized,
                        "stop_loss": pos.stop_loss,
                        "take_profit": pos.take_profit,
                    }
                )

            if total_unrealized_status == "unavailable":
                total_unrealized = None

            equity_rows = state.get_equity_history(limit=2000)
            equity_series = []
            for row in equity_rows:
                value = _finite_number(row.get("equity"))
                if value is not None:
                    equity_series.append(value)
            current_equity = equity_series[-1] if equity_series else 0.0
            day_start = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            today = []
            for row in equity_rows:
                value = _finite_number(row.get("equity"))
                try:
                    timestamp = _as_utc(row["ts"])
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                if value is not None and timestamp >= day_start:
                    today.append(value)
            daily_base = (
                today[0] if today else (equity_series[0] if equity_series else 0.0)
            )
            daily_pnl = current_equity - daily_base if daily_base else 0.0
            daily_pnl_pct = (daily_pnl / daily_base * 100.0) if daily_base else 0.0
            peak_equity = max(equity_series) if equity_series else 0.0
            drawdown_pct = (
                (peak_equity - current_equity) / peak_equity * 100.0
                if peak_equity
                else 0.0
            )

            trades = []
            for trade in state.get_recent_trades(30):
                normalized = dict(trade)
                if isinstance(normalized.get("ts"), datetime):
                    normalized["ts"] = normalized["ts"].isoformat()
                trades.append(normalized)
            _database_ok, bot_online = probe_state()
            unresolved_orders = state.get_unresolved_orders()
        except Exception:
            logger.exception("Dashboard data read failed")
            return make_response("Dashboard data is temporarily unavailable.", 503)

        return render_template(
            "index.html",
            positions=enriched,
            trades=trades,
            equity_series=equity_series,
            current_equity=current_equity,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
            peak_equity=peak_equity,
            drawdown_pct=drawdown_pct,
            total_unrealized=total_unrealized,
            total_unrealized_status=total_unrealized_status,
            bot_online=bot_online,
            unresolved_orders=unresolved_orders,
            csrf_token=csrf_token(),
            price_stale_seconds=price_stale_seconds,
        )

    @app.errorhandler(400)
    def bad_request(_err):
        return make_response("Bad request.", 400)

    @app.errorhandler(404)
    def not_found(_err):
        return make_response("Not found.", 404)

    @app.errorhandler(413)
    def request_too_large(_err):
        return make_response("Request too large.", 413)

    return app
