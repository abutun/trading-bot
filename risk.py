from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Mapping


class RiskManager:
    """Persistent, fail-closed portfolio risk controls."""

    def __init__(self, config, state):
        self.config = config
        self.state = state

    def _set_meta_many(self, entries: dict[str, str]) -> None:
        """Use the durable atomic API, with a small adapter for test doubles."""
        setter = getattr(self.state, "set_meta_many", None)
        if callable(setter):
            setter(entries)
            return
        for key, value in entries.items():
            self.state.set_meta(key, value)

    def _meta_bool(self, key: str) -> bool:
        raw = self.state.get_meta(key)
        if raw is None:
            return False
        normalized = str(raw).strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
        # Persisting malformed safety metadata must never silently re-enable
        # orders.  The next operator acknowledgement remains explicit.
        self._set_halt("halted_safety", f"invalid_risk_metadata:{key}")
        return True

    def _meta_float(self, key: str, default: float) -> float:
        raw = self.state.get_meta(key)
        if raw is None:
            return default
        try:
            value = float(raw)
        except (TypeError, ValueError):
            self._set_halt("halted_safety", f"invalid_risk_metadata:{key}")
            return default
        if not math.isfinite(value) or value < 0:
            self._set_halt("halted_safety", f"invalid_risk_metadata:{key}")
            return default
        return value

    def update(self, equity: float) -> None:
        if not math.isfinite(equity) or equity <= 0:
            raise ValueError("Equity must be finite and positive before updating risk state")

        now = datetime.now(timezone.utc)
        day = now.strftime("%Y-%m-%d")
        stored_day = self.state.get_meta("day")
        if stored_day != day:
            self._set_meta_many(
                {
                    "day": day,
                    "daily_start_equity": str(equity),
                    "halted_daily": "false",
                }
            )

        peak_missing = self.state.get_meta("peak_equity") is None
        peak = self._meta_float("peak_equity", equity)
        if peak_missing or equity > peak:
            peak = equity
            self._set_meta_many({"peak_equity": str(equity)})

        daily_start = self._meta_float("daily_start_equity", equity)
        if not self._meta_bool("halted_daily") and daily_start > 0:
            daily_limit = daily_start * (1 - self.config.max_daily_loss_pct / 100)
            if equity <= daily_limit:
                self._set_halt("halted_daily", "daily_loss_limit")

        if not self._meta_bool("halted_total") and peak > 0:
            total_limit = peak * (1 - self.config.max_total_drawdown_pct / 100)
            if equity <= total_limit:
                self._set_halt("halted_total", "total_drawdown_limit")

    def _set_halt(self, key: str, reason: str) -> None:
        self._set_meta_many(
            {
                key: "true",
                "halt_reason": reason,
                "halted_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def halt_safely(self, reason: str) -> None:
        """Stop all new orders after an invariant, data, or venue failure."""
        self._set_halt("halted_safety", reason[:500])

    def clear_safety_halt(self, reason: str) -> None:
        """Explicitly record a manual operator acknowledgement after reconciliation."""
        self._set_meta_many(
            {
                "halted_safety": "false",
                "halt_reason": f"manually_cleared:{reason[:400]}",
                "halted_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def record_cycle_failure(self, reason: str) -> None:
        failures = int(self._meta_float("bot_consecutive_failures", 0.0)) + 1
        entries = {
            "bot_consecutive_failures": str(failures),
            "bot_last_error": reason[:1000],
        }
        if failures >= self.config.max_consecutive_failures:
            entries.update(
                {
                    "halted_safety": "true",
                    "halt_reason": f"consecutive_failures:{failures}:{reason[:300]}",
                    "halted_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        self._set_meta_many(entries)

    def record_cycle_success(self) -> None:
        self._set_meta_many(
            {
                "bot_consecutive_failures": "0",
                "bot_last_success_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def halted(self) -> bool:
        return (
            self._meta_bool("halted_daily")
            or self._meta_bool("halted_total")
            or self._meta_bool("halted_safety")
        )

    def total_halted(self) -> bool:
        return self._meta_bool("halted_total")

    def liquidation_required(self) -> bool:
        """Whether a loss-limit halt calls for controlled position exits.

        A safety halt (unknown order, bad data, broken invariant) deliberately
        does *not* auto-submit a new exit order: doing so could duplicate an
        order whose outcome is exactly what remains unknown.
        """
        return not self._meta_bool("halted_safety") and (
            self._meta_bool("halted_daily") or self._meta_bool("halted_total")
        )

    def can_open(
        self,
        pair_id: str,
        equity: float,
        prices: Mapping[str, float] | None = None,
    ) -> bool:
        if self.halted() or not math.isfinite(equity) or equity <= 0:
            return False
        if self.state.get_position(pair_id) is not None:
            return False
        if self.state.has_unresolved_order(pair_id):
            return False
        has_global_unresolved = getattr(self.state, "has_any_unresolved_order", None)
        if has_global_unresolved is not None and has_global_unresolved():
            return False
        if len(self.state.get_all_positions()) >= self.config.max_open_positions:
            return False

        if prices is None:
            return True
        exposure = self.current_exposure(prices)
        if exposure is None:
            return False
        return exposure < equity * (self.config.max_total_exposure_pct / 100)

    def current_exposure(self, prices: Mapping[str, float]) -> float | None:
        exposure = 0.0
        for pos in self.state.get_all_positions():
            price = prices.get(pos.pair_id)
            if price is None or not math.isfinite(price) or price <= 0:
                return None
            exposure += pos.qty * price
        return exposure if math.isfinite(exposure) and exposure >= 0 else None

    def position_size(self, equity: float, price: float, current_exposure: float = 0.0) -> float:
        if (
            not math.isfinite(equity)
            or not math.isfinite(price)
            or not math.isfinite(current_exposure)
            or equity <= 0
            or price <= 0
            or current_exposure < 0
        ):
            return 0.0
        max_by_position = equity * (self.config.max_position_pct / 100)
        max_by_total = max(
            0.0, equity * (self.config.max_total_exposure_pct / 100) - current_exposure
        )
        notional = min(max_by_position, max_by_total, self.config.max_order_notional)
        # A buy can fill at the venue's price ceiling, not merely at the candle
        # close.  Size against that global worst-case price so the hard
        # MAX_ORDER_NOTIONAL_USD cap remains true for every adapter.
        worst_case_buy_price = price * (
            1 + self.config.max_order_slippage_bps / 10_000
        )
        if not math.isfinite(worst_case_buy_price) or worst_case_buy_price <= 0:
            return 0.0
        return max(0.0, notional / worst_case_buy_price)

    def check_exit(self, pos, price: float) -> bool:
        if not math.isfinite(price) or price <= 0:
            return False
        if pos.stop_loss > 0 and price <= pos.stop_loss:
            return True
        if pos.take_profit > 0 and price >= pos.take_profit:
            return True
        return False

    def fill_within_slippage(self, side: str, reference_price: float, fill_price: float) -> bool:
        if (
            side not in {"buy", "sell"}
            or not math.isfinite(reference_price)
            or not math.isfinite(fill_price)
            or reference_price <= 0
            or fill_price <= 0
        ):
            return False
        allowed = self.config.max_order_slippage_bps / 10_000
        if side == "buy":
            return fill_price <= reference_price * (1 + allowed)
        return fill_price >= reference_price * (1 - allowed)
