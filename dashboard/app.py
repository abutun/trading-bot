from __future__ import annotations

import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlparse

from flask import Flask, abort, redirect, render_template, request, session, url_for

from config import Config
from state import StateStore


logger = logging.getLogger(__name__)

HEARTBEAT_KEY = "bot_heartbeat"
HEARTBEAT_STALE_SECONDS = 180


def _as_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def create_app(config: Config | None = None) -> Flask:
    config = config or Config.from_env()
    if not config.dashboard_password:
        raise RuntimeError("DASHBOARD_PASSWORD is required to run the dashboard")
    if not config.dashboard_secret_key:
        raise RuntimeError("DASHBOARD_SECRET_KEY is required to run the dashboard")

    app = Flask(__name__)
    app.secret_key = config.dashboard_secret_key
    app.config.update(
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.dashboard_secure_cookies,
    )
    state = StateStore(config)

    def csrf_token() -> str:
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    def validate_csrf() -> None:
        token = request.form.get("csrf_token", "")
        if not hmac.compare_digest(token, session.get("csrf_token", "")):
            abort(400, "Invalid CSRF token")

    def login_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not session.get("authenticated"):
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)

        return wrapper

    def _bot_online() -> bool:
        heartbeat_raw = state.get_meta(HEARTBEAT_KEY, "")
        try:
            return (datetime.now(timezone.utc) - _as_utc(heartbeat_raw)).total_seconds() <= HEARTBEAT_STALE_SECONDS
        except (ValueError, TypeError):
            return False

    def _safe_next_url() -> str:
        target = request.args.get("next", "")
        parsed = urlparse(target)
        if target.startswith("/") and not target.startswith("//") and not parsed.netloc:
            return target
        return url_for("index")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            validate_csrf()
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            correct_username = hmac.compare_digest(username, config.dashboard_username)
            correct_password = hmac.compare_digest(password, config.dashboard_password)
            if correct_username and correct_password:
                session.clear()
                session["authenticated"] = True
                session.permanent = True
                csrf_token()
                return redirect(_safe_next_url())
            error = "Invalid username or password."

        return render_template("login.html", error=error, csrf_token=csrf_token())

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        validate_csrf()
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def index():
        positions = state.get_all_positions()
        prices: dict[str, float] = {}
        md = None
        if positions:
            try:
                from market_data import MarketData

                md = MarketData(config)
                for pos in positions:
                    if pos.pair_id in prices:
                        continue
                    try:
                        df = md.fetch_ohlcv(pos.symbol, venue=pos.venue)
                        if not df.empty:
                            prices[pos.pair_id] = float(df["close"].iloc[-1])
                    except Exception as exc:
                        logger.warning(
                            "Dashboard price fetch failed for %s: %s", pos.pair_id, exc
                        )
            except Exception as exc:
                logger.warning("Dashboard market data init failed: %s", exc)
            finally:
                if md is not None:
                    md.close()

        enriched = []
        total_unrealized = 0.0
        for pos in positions:
            price = prices.get(pos.pair_id, pos.entry_price)
            unrealized = (price - pos.entry_price) * pos.qty
            total_unrealized += unrealized
            enriched.append(
                {
                    "pair_id": pos.pair_id,
                    "venue": pos.venue,
                    "symbol": pos.symbol,
                    "qty": pos.qty,
                    "entry_price": pos.entry_price,
                    "price": price,
                    "unrealized_pnl": unrealized,
                    "stop_loss": pos.stop_loss,
                    "take_profit": pos.take_profit,
                }
            )

        equity_rows = state.get_equity_history(limit=2000)
        equity_series = [float(row["equity"]) for row in equity_rows]
        current_equity = equity_series[-1] if equity_series else 0.0
        day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        today = [row for row in equity_rows if _as_utc(row["ts"]) >= day_start]
        daily_base = float(today[0]["equity"]) if today else (equity_series[0] if equity_series else 0.0)
        daily_pnl = current_equity - daily_base if daily_base else 0.0
        daily_pnl_pct = (daily_pnl / daily_base * 100.0) if daily_base else 0.0
        peak_equity = max(equity_series) if equity_series else 0.0
        drawdown_pct = ((peak_equity - current_equity) / peak_equity * 100.0) if peak_equity else 0.0

        trades = []
        for trade in state.get_recent_trades(30):
            normalized = dict(trade)
            if isinstance(normalized.get("ts"), datetime):
                normalized["ts"] = normalized["ts"].isoformat()
            trades.append(normalized)

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
            bot_online=_bot_online(),
            unresolved_orders=state.get_unresolved_orders(),
            csrf_token=csrf_token(),
        )

    @app.errorhandler(404)
    def not_found(_err):
        return render_template("login.html", error="Page not found.", csrf_token=csrf_token()), 404

    return app
