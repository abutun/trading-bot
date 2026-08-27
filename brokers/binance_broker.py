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

        if not config.binance_api_key or not config.binance_api_secret:
            raise ValueError(
                "BINANCE_API_KEY and BINANCE_API_SECRET are required for live Binance trading"
            )

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
            price = prices.get(pos.pair_id, pos.entry_price)
            equity += pos.qty * price

        return equity

    def _create_order(
        self, symbol: str, side: str, qty: float, client_order_id: str | None = None
    ):
        params = {"newClientOrderId": client_order_id} if client_order_id else {}
        order = self.ex.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=qty,
            params=params,
        )

        try:
            if order.get("id"):
                order = self.ex.fetch_order(order["id"], symbol)
        except Exception as exc:
            # A request may have reached Binance even though confirmation failed.
            # Escalate this to the bot's durable "unknown" order state instead
            # of reporting a zero fill and allowing a duplicate retry.
            if not float(order.get("filled") or 0.0):
                raise RuntimeError(
                    f"Binance accepted order {order.get('id')} but its fill could not be confirmed"
                ) from exc
            logger.warning("Could not refresh Binance order %s: %s", order.get("id"), exc)

        price = float(order.get("average") or order.get("price") or 0.0)
        fee_cost = self._fee_in_quote_currency(order, symbol, price)
        filled_qty = float(order.get("filled") or 0.0)
        return order, price, fee_cost, filled_qty

    def _fee_in_quote_currency(self, order: dict, symbol: str, price: float) -> float:
        """Return a known quote-currency fee, without mislabelling BNB fees as USD."""
        base_currency = symbol.split("/", 1)[0]
        single_fee = order.get("fee") or {}
        fee_items = [single_fee] if single_fee.get("cost") else (order.get("fees") or [])
        total = 0.0
        found = False
        for fee in fee_items:
            cost = float(fee.get("cost") or 0.0)
            currency = fee.get("currency")
            if not cost:
                continue
            if currency == self.config.quote_currency:
                total += cost
                found = True
            elif currency == base_currency and price > 0:
                total += cost * price
                found = True
        return total if found else 0.0

    def buy(
        self,
        symbol: str,
        qty: float,
        price_hint: float,
        client_order_id: str | None = None,
    ) -> ExecutionResult:
        if qty <= 0:
            return ExecutionResult(qty=0, price=price_hint)

        qty = self._amount(symbol, qty)
        if qty <= 0:
            return ExecutionResult(qty=0, price=price_hint)

        order, price, fee, filled_qty = self._create_order(
            symbol, "buy", qty, client_order_id
        )
        if filled_qty <= 0:
            return ExecutionResult(qty=0, price=price_hint, order_id=str(order.get("id", "")))
        if price <= 0:
            price = price_hint

        if fee <= 0:
            fee = filled_qty * price * self.config.fee_rate
        return ExecutionResult(qty=filled_qty, price=price, fee=fee, order_id=str(order.get("id", "")))

    def sell(
        self,
        symbol: str,
        qty: float,
        price_hint: float,
        client_order_id: str | None = None,
    ) -> ExecutionResult:
        if qty <= 0:
            return ExecutionResult(qty=0, price=price_hint)

        qty = self._amount(symbol, qty)
        if qty <= 0:
            return ExecutionResult(qty=0, price=price_hint)

        order, price, fee, filled_qty = self._create_order(
            symbol, "sell", qty, client_order_id
        )
        if filled_qty <= 0:
            return ExecutionResult(qty=0, price=price_hint, order_id=str(order.get("id", "")))
        if price <= 0:
            price = price_hint

        if fee <= 0:
            fee = filled_qty * price * self.config.fee_rate
        return ExecutionResult(qty=filled_qty, price=price, fee=fee, order_id=str(order.get("id", "")))
