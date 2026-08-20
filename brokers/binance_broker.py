from __future__ import annotations

import logging

import ccxt

from models import ExecutionResult

from .base import Broker


logger = logging.getLogger(__name__)


class BinanceBroker(Broker):
    venue = "binance"

    def __init__(self, config, state):
        self.config = config
        self.state = state

        params = {
            "apiKey": config.binance_api_key,
            "secret": config.binance_api_secret,
            "enableRateLimit": True,
        }

        self.ex = ccxt.binance(params)

        if config.binance_testnet:
            self.ex.set_sandbox_mode(True)

        self.ex.load_markets()

    def _amount(self, symbol: str, qty: float) -> float:
        return float(self.ex.amount_to_precision(symbol, qty))

    def _quote_total(self) -> float:
        balance = self.ex.fetch_balance()
        return float(balance.get(self.config.quote_currency, {}).get("total", 0) or 0)

    def get_equity(self, prices: dict[str, float]) -> float:
        equity = self._quote_total()

        for pos in self.state.get_positions_by_venue("binance"):
            price = prices.get(pos.symbol, pos.entry_price)
            equity += pos.qty * price

        return equity

    def _create_order(self, symbol: str, side: str, qty: float):
        order = self.ex.create_order(symbol=symbol, type="market", side=side, amount=qty)

        try:
            if order.get("id"):
                order = self.ex.fetch_order(order["id"], symbol)
        except Exception as exc:
            logger.warning("Could not fetch order %s: %s", order.get("id"), exc)

        price = float(order.get("average") or order.get("price") or 0.0)
        return order, price

    def buy(self, symbol: str, qty: float, price_hint: float) -> ExecutionResult:
        if qty <= 0:
            return ExecutionResult(qty=0, price=price_hint)

        qty = self._amount(symbol, qty)
        if qty <= 0:
            return ExecutionResult(qty=0, price=price_hint)

        order, price = self._create_order(symbol, "buy", qty)
        if price <= 0:
            price = price_hint

        fee = qty * price * self.config.fee_rate
        return ExecutionResult(qty=qty, price=price, fee=fee, order_id=str(order.get("id", "")))

    def sell(self, symbol: str, qty: float, price_hint: float) -> ExecutionResult:
        if qty <= 0:
            return ExecutionResult(qty=0, price=price_hint)

        qty = self._amount(symbol, qty)
        if qty <= 0:
            return ExecutionResult(qty=0, price=price_hint)

        order, price = self._create_order(symbol, "sell", qty)
        if price <= 0:
            price = price_hint

        fee = qty * price * self.config.fee_rate
        return ExecutionResult(qty=qty, price=price, fee=fee, order_id=str(order.get("id", "")))
