from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from config import Config
from state import StateStore


logger = logging.getLogger(__name__)

HEARTBEAT_KEY = "bot_heartbeat"
HEARTBEAT_STALE_SECONDS = 180


def create_app(config: Config | None = None) -> Flask:
    config = config or Config.from_env()

    if not config.dashboard_password:
        raise RuntimeError("DASHBOARD_PASSWORD is required to run the dashboard")

    app = Flask(__name__)
    app.secret_key = config.dashboard_secret_key or "insecure-dev-key-change-me"
    app.config["PERMANENT_SESSION_LIFETIME"] = 8 * 3600

    state = StateStore(config)

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
            heartbeat_ts = datetime.fromisoformat(heartbeat_raw)
            age = (datetime.now(timezone.utc) - heartbeat_ts).total_seconds()
            return age <= HEARTBEAT_STALE_SECONDS
        except (ValueError, TypeError):
            return False

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None

        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""

            if (
                username == config.dashboard_username
                and password == config.dashboard_password
            ):
                session["authenticated"] = True
                session.permanent = True
                return redirect(request.args.get("next") or url_for("index"))

            error = "Invalid username or password."

        return render_template("login.html", error=error)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def index():
        positions = state.get_all_positions()

        # Fetch live prices for open positions (best effort).
        prices: dict[str, float] = {}

        if positions:
            try:
                from market_data import MarketData

                md = MarketData(config)

                for pos in positions:
                    if pos.symbol in prices:
                        continue

                    try:
                        df = md.fetch_ohlcv(pos.symbol)
                        if not df.empty:
                            prices[pos.symbol] = float(df["close"].iloc[-1])
                    except Exception as exc:
                        logger.warning(
                            "Dashboard price fetch failed for %s: %s", pos.symbol, exc
                        )
            except Exception as exc:
                logger.warning("Dashboard market data init failed: %s", exc)

        enriched = []
        total_unrealized = 0.0

        for pos in positions:
            price = prices.get(pos.symbol, pos.entry_price)
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
        trades = state.get_recent_trades(30)

        equity_series = [float(row["equity"]) for row in equity_rows]
        current_equity = equity_series[-1] if equity_series else 0.0

        day_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00")
        today = [row for row in equity_rows if str(row["ts"]) >= day_start]
        daily_base = (
            float(today[0]["equity"]) if today else (equity_series[0] if equity_series else 0.0)
        )
        daily_pnl = (
            current_equity - daily_base if (current_equity and daily_base) else 0.0
        )
        daily_pnl_pct = (daily_pnl / daily_base * 100.0) if daily_base else 0.0

        peak_equity = max(equity_series) if equity_series else 0.0
        drawdown_pct = (
            ((peak_equity - current_equity) / peak_equity * 100.0)
            if (peak_equity and current_equity)
            else 0.0
        )

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
        )

    @app.errorhandler(404)
    def not_found(_err):
        return render_template("login.html", error="Page not found."), 404

    return app
