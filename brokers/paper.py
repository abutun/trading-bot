from __future__ import annotations

from models import ExecutionResult

from .base import Broker


class PaperBroker(Broker):
    def __init__(self, config, state, venue: str, initial_capital: float):
        self.config = config
        self.state = state
        self.venue = venue

        self.cash_key = f"paper_cash_{venue}"

        if state.get_meta(self.cash_key) is None:
            state.set_meta(self.cash_key, str(initial_capital))

    def _cash(self) -> float:
        return float(self.state.get_meta(self.cash_key, "0"))

    def _set_cash(self, value: float) -> None:
        self.state.set_meta(self.cash_key, str(value))

    def get_equity(self, prices: dict[str, float]) -> float:
        equity = self._cash()

        for pos in self.state.get_positions_by_venue(self.venue):
            # MarketData and all live brokers key prices by the durable pair
            # identifier (``venue:symbol``), not just the local symbol.
            # Looking up `pos.symbol` silently valued every paper position at
            # its entry price and could hide a risk-limit breach.
            price = prices.get(pos.pair_id, pos.entry_price)
            equity += pos.qty * price

        return equity

    def buy(
        self,
        symbol: str,
        qty: float,
        price_hint: float,
        client_order_id: str | None = None,
    ) -> ExecutionResult:
        if qty <= 0 or price_hint <= 0:
            return ExecutionResult(qty=0, price=price_hint)

        fill_price = price_hint * (1 + self.config.slippage_bps / 10_000)
        cost = qty * fill_price * (1 + self.config.fee_rate)

        cash = self._cash()

        if cost > cash:
            qty = cash / (fill_price * (1 + self.config.fee_rate)) if fill_price > 0 else 0
            cost = qty * fill_price * (1 + self.config.fee_rate)

        if qty <= 0:
            return ExecutionResult(qty=0, price=fill_price)

        self._set_cash(cash - cost)

        fee = qty * fill_price * self.config.fee_rate
        return ExecutionResult(
            qty=qty,
            price=fill_price,
            fee=fee,
            order_id=f"paper:{client_order_id}" if client_order_id else "",
        )

    def sell(
        self,
        symbol: str,
        qty: float,
        price_hint: float,
        client_order_id: str | None = None,
    ) -> ExecutionResult:
        if qty <= 0 or price_hint <= 0:
            return ExecutionResult(qty=0, price=price_hint)

        fill_price = price_hint * (1 - self.config.slippage_bps / 10_000)
        proceeds = qty * fill_price * (1 - self.config.fee_rate)

        self._set_cash(self._cash() + proceeds)

        fee = qty * fill_price * self.config.fee_rate
        return ExecutionResult(
            qty=qty,
            price=fill_price,
            fee=fee,
            order_id=f"paper:{client_order_id}" if client_order_id else "",
        )
