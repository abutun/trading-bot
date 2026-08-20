from datetime import datetime, timezone


class RiskManager:
    def __init__(self, config, state):
        self.config = config
        self.state = state

    def _meta_bool(self, key: str) -> bool:
        return self.state.get_meta(key, "false").lower() == "true"

    def update(self, equity: float) -> None:
        now = datetime.now(timezone.utc)
        day = now.strftime("%Y-%m-%d")

        stored_day = self.state.get_meta("day", day)
        if stored_day != day:
            self.state.set_meta("day", day)
            self.state.set_meta("daily_start_equity", str(equity))
            self.state.set_meta("halted_daily", "false")

        peak = float(self.state.get_meta("peak_equity", str(equity)))
        if equity > peak:
            self.state.set_meta("peak_equity", str(equity))

        daily_start = float(self.state.get_meta("daily_start_equity", str(equity)))

        if not self._meta_bool("halted_daily") and daily_start > 0:
            daily_limit = daily_start * (1 - self.config.max_daily_loss_pct / 100)
            if equity <= daily_limit:
                self.state.set_meta("halted_daily", "true")

        if not self._meta_bool("halted_total") and peak > 0:
            total_limit = peak * (1 - self.config.max_total_drawdown_pct / 100)
            if equity <= total_limit:
                self.state.set_meta("halted_total", "true")

    def halted(self) -> bool:
        return self._meta_bool("halted_daily") or self._meta_bool("halted_total")

    def total_halted(self) -> bool:
        return self._meta_bool("halted_total")

    def can_open(self, pair_id: str, equity: float) -> bool:
        if self.halted():
            return False

        if self.state.get_position(pair_id) is not None:
            return False

        open_positions = len(self.state.get_all_positions())
        if open_positions >= self.config.max_open_positions:
            return False

        return True

    def position_size(self, equity: float, price: float) -> float:
        if price <= 0:
            return 0.0

        notional = equity * (self.config.max_position_pct / 100)
        return max(0.0, notional / price)

    def check_exit(self, pos, price: float) -> bool:
        if pos.stop_loss > 0 and price <= pos.stop_loss:
            return True

        if pos.take_profit > 0 and price >= pos.take_profit:
            return True

        return False
