from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import ccxt

from models import ExecutionResult

from .base import Broker, BrokerOrderRejectedError, BrokerOrderUncertainError


logger = logging.getLogger(__name__)


TERMINAL_ORDER_STATUSES = {"closed", "canceled", "cancelled", "expired", "rejected"}


class BinanceBroker(Broker):
    """Binance Spot adapter with price-capped, client-idempotent IOC orders.

    A market order can execute substantially away from the strategy's reference
    price during a gap. Live configuration therefore requires ``ioc_limit``:
    Binance either fills immediately within the allowed limit or cancels the
    unfilled remainder. Any non-terminal / unqueryable result is deliberately
    surfaced as uncertain and blocks further automation until reconciled.
    """

    venue = "binance"

    def __init__(self, config, state):
        self.config = config
        self.state = state

        if not config.binance_api_key or not config.binance_api_secret:
            raise ValueError(
                "BINANCE_API_KEY and BINANCE_API_SECRET are required for live Binance trading"
            )

        self.ex = ccxt.binance(
            {
                "apiKey": config.binance_api_key,
                "secret": config.binance_api_secret,
                "enableRateLimit": True,
                "timeout": 15_000,
            }
        )
        if config.binance_testnet:
            self.ex.set_sandbox_mode(True)
        self.ex.load_markets()

    def _amount(self, symbol: str, qty: float) -> float:
        if not math.isfinite(qty) or qty <= 0:
            return 0.0
        return float(self.ex.amount_to_precision(symbol, qty))

    def _limit_price(self, symbol: str, side: str, price_hint: float) -> float:
        if side not in {"buy", "sell"} or not math.isfinite(price_hint) or price_hint <= 0:
            raise BrokerOrderRejectedError("Binance reference price is invalid")
        allowance = self.config.max_order_slippage_bps / 10_000
        raw_limit = price_hint * (1 + allowance if side == "buy" else 1 - allowance)
        if raw_limit <= 0:
            raise BrokerOrderRejectedError("Binance calculated a non-positive IOC limit")

        # CCXT normalizes the string for the exchange's precision / tick-size
        # rules. If normalizing a buy price rounds up by a tick, fail closed
        # rather than accidentally widening the user-approved ceiling.
        normalized = float(self.ex.price_to_precision(symbol, raw_limit))
        if normalized <= 0:
            raise BrokerOrderRejectedError("Binance calculated an invalid IOC limit")
        if side == "buy" and normalized > raw_limit:
            raise BrokerOrderRejectedError(
                "Binance price precision would exceed the configured buy slippage cap"
            )
        if side == "sell" and normalized < raw_limit:
            raise BrokerOrderRejectedError(
                "Binance price precision would exceed the configured sell slippage cap"
            )
        return normalized

    def _quote_total(self) -> float:
        balance = self.ex.fetch_balance()
        value = float(balance.get(self.config.quote_currency, {}).get("total", 0) or 0)
        if not math.isfinite(value) or value < 0:
            raise RuntimeError("Binance returned an invalid quote balance")
        return value

    def get_equity(self, prices: dict[str, float]) -> float:
        equity = self._quote_total()
        for pos in self.state.get_positions_by_venue(self.venue):
            price = prices.get(pos.pair_id)
            if price is None or not math.isfinite(price) or price <= 0:
                raise RuntimeError(f"No validated price for Binance position {pos.pair_id}")
            equity += pos.qty * price
        return equity

    @staticmethod
    def _is_definite_rejection(exc: Exception) -> bool:
        return isinstance(
            exc,
            (
                ccxt.InsufficientFunds,
                ccxt.InvalidOrder,
                ccxt.BadRequest,
                ccxt.AuthenticationError,
                ccxt.PermissionDenied,
            ),
        )

    def _recover_by_client_id(self, symbol: str, client_order_id: str | None) -> dict | None:
        """Best-effort lookup after a transport error without retrying the order."""
        if not client_order_id:
            return None
        method = getattr(self.ex, "fetch_order_by_client_order_id", None)
        if not callable(method):
            return None
        try:
            recovered = method(client_order_id, symbol)
            return recovered if isinstance(recovered, dict) else None
        except Exception as exc:
            logger.warning(
                "Could not recover Binance order by client id %s: %s",
                client_order_id,
                type(exc).__name__,
            )
            return None

    def _refresh_to_terminal(self, order: dict, symbol: str) -> dict:
        order_id = str(order.get("id") or "")
        if not order_id:
            raise BrokerOrderUncertainError(
                "Binance returned an order response without an exchange order ID"
            )
        try:
            refreshed = self.ex.fetch_order(order_id, symbol)
        except Exception as exc:
            raise BrokerOrderUncertainError(
                f"Binance accepted order {order_id} but final status could not be fetched",
                order_id,
            ) from exc
        if not isinstance(refreshed, dict):
            raise BrokerOrderUncertainError(
                f"Binance returned a malformed status for order {order_id}", order_id
            )
        status = str(refreshed.get("status") or "").lower()
        if status not in TERMINAL_ORDER_STATUSES:
            raise BrokerOrderUncertainError(
                f"Binance order {order_id} is non-terminal ({status or 'missing status'})",
                order_id,
            )
        return refreshed

    @staticmethod
    def _record_fee_items(record: Mapping) -> list[Mapping] | None:
        """Return explicit CCXT fee items, or ``None`` when omitted entirely."""
        raw_fees = record.get("fees")
        if raw_fees:
            if not isinstance(raw_fees, (list, tuple)):
                raise ValueError("fees is not a list")
            return list(raw_fees)

        single_fee = record.get("fee")
        if single_fee in (None, {}):
            return None
        if not isinstance(single_fee, Mapping):
            raise ValueError("fee is not a dictionary")
        return [single_fee]

    @staticmethod
    def _trade_order_id(trade: Mapping) -> str:
        """Read the unified order ID, with Binance's raw field as a fallback."""
        order_id = trade.get("order")
        if order_id not in (None, ""):
            return str(order_id)
        info = trade.get("info")
        if isinstance(info, Mapping):
            for field in ("orderId", "orderID", "order_id"):
                value = info.get(field)
                if value not in (None, ""):
                    return str(value)
        return ""

    @staticmethod
    def _valid_trade_number(value: Any, field: str, order_id: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise BrokerOrderUncertainError(
                f"Binance trade for order {order_id} has invalid {field}", order_id
            ) from exc
        if not math.isfinite(number) or number < 0:
            raise BrokerOrderUncertainError(
                f"Binance trade for order {order_id} has invalid {field}", order_id
            )
        return number

    @staticmethod
    def _matches_total(actual: float, expected: float) -> bool:
        return math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12)

    def _fetch_fee_items_from_trades(
        self,
        *,
        order_id: str,
        symbol: str,
        filled_qty: float,
        order_cost: float,
    ) -> list[Mapping]:
        """Reconcile fees from the exact fills for one terminal Binance order.

        CCXT documents ``fetch_order_trades`` for this purpose.  Some client
        versions or exchange configurations do not expose it, so use the
        Binance/CCXT ``fetch_my_trades(..., params={"orderId": ...})`` route as
        a fallback.  In both cases each returned trade must still carry the
        same order ID and aggregate to the terminal fill; trusting the endpoint
        filter alone would permit unrelated commissions to alter state.
        """
        lookup_errors: list[str] = []
        fetch_order_trades = getattr(self.ex, "fetch_order_trades", None)
        if callable(fetch_order_trades):
            try:
                trades = fetch_order_trades(order_id, symbol)
            except Exception as exc:
                lookup_errors.append(f"fetch_order_trades:{type(exc).__name__}")
            else:
                return self._validated_trade_fee_items(
                    trades, order_id, filled_qty, order_cost
                )

        fetch_my_trades = getattr(self.ex, "fetch_my_trades", None)
        if callable(fetch_my_trades):
            try:
                trades = fetch_my_trades(symbol, None, None, {"orderId": order_id})
            except Exception as exc:
                lookup_errors.append(f"fetch_my_trades:{type(exc).__name__}")
            else:
                return self._validated_trade_fee_items(
                    trades, order_id, filled_qty, order_cost
                )

        detail = ", ".join(lookup_errors) or "no trade-fee lookup method"
        raise BrokerOrderUncertainError(
            f"Binance omitted fee data for filled order {order_id}; trade reconciliation failed ({detail})",
            order_id,
        )

    def _validated_trade_fee_items(
        self, trades: Any, order_id: str, filled_qty: float, order_cost: float
    ) -> list[Mapping]:
        if not isinstance(trades, (list, tuple)) or not trades:
            raise BrokerOrderUncertainError(
                f"Binance trade reconciliation returned no fills for order {order_id}",
                order_id,
            )

        total_qty = 0.0
        total_cost = 0.0
        fee_items: list[Mapping] = []
        for trade in trades:
            if not isinstance(trade, Mapping):
                raise BrokerOrderUncertainError(
                    f"Binance trade reconciliation returned a malformed fill for order {order_id}",
                    order_id,
                )
            if self._trade_order_id(trade) != order_id:
                raise BrokerOrderUncertainError(
                    f"Binance trade reconciliation returned an unmatched fill for order {order_id}",
                    order_id,
                )
            amount = self._valid_trade_number(trade.get("amount"), "amount", order_id)
            cost = self._valid_trade_number(trade.get("cost"), "cost", order_id)
            if amount <= 0:
                raise BrokerOrderUncertainError(
                    f"Binance trade reconciliation returned a zero fill for order {order_id}",
                    order_id,
                )
            try:
                trade_fee_items = self._record_fee_items(trade)
            except ValueError as exc:
                raise BrokerOrderUncertainError(
                    f"Binance trade reconciliation returned malformed fee data for order {order_id}",
                    order_id,
                ) from exc
            if trade_fee_items is None:
                raise BrokerOrderUncertainError(
                    f"Binance trade reconciliation omitted fee data for order {order_id}",
                    order_id,
                )
            total_qty += amount
            total_cost += cost
            fee_items.extend(trade_fee_items)

        if not self._matches_total(total_qty, filled_qty):
            raise BrokerOrderUncertainError(
                f"Binance reconciled fills do not match terminal quantity for order {order_id}",
                order_id,
            )
        if order_cost > 0 and not self._matches_total(total_cost, order_cost):
            raise BrokerOrderUncertainError(
                f"Binance reconciled fills do not match terminal cost for order {order_id}",
                order_id,
            )
        return fee_items

    def _fee_breakdown(
        self, order: dict, symbol: str, filled_qty: float, order_cost: float
    ) -> tuple[float, float]:
        """Return quote-currency and base-token fees from a terminal order.

        A base-token commission changes the spendable inventory.  It therefore
        cannot be treated as a display-only fee: callers use the returned base
        amount to persist either the net acquired quantity (buy) or the full
        base balance debit (sell). A terminal order that omits fee data is
        reconciled against its exact private trades; if that cannot prove every
        fill and commission, the order remains unknown rather than guessing.
        """
        base_currency = symbol.split("/", 1)[0]
        order_id = str(order.get("id") or "")
        try:
            fee_items = self._record_fee_items(order)
        except ValueError as exc:
            raise BrokerOrderUncertainError(
                f"Binance returned malformed fees for order {order_id}", order_id
            ) from exc
        if fee_items is None:
            fee_items = self._fetch_fee_items_from_trades(
                order_id=order_id,
                symbol=symbol,
                filled_qty=filled_qty,
                order_cost=order_cost,
            )

        quote_total = 0.0
        base_total = 0.0
        unknown_currencies: set[str] = set()
        for fee in fee_items:
            if not isinstance(fee, Mapping):
                raise BrokerOrderUncertainError(
                    f"Binance returned a malformed fee item for order {order_id}", order_id
                )
            if fee.get("cost") is None:
                raise BrokerOrderUncertainError(
                    f"Binance omitted a fee cost for order {order_id}", order_id
                )
            try:
                cost = float(fee["cost"])
            except (TypeError, ValueError) as exc:
                raise BrokerOrderUncertainError(
                    f"Binance returned an invalid fee cost for order {order_id}", order_id
                ) from exc
            if not math.isfinite(cost) or cost < 0:
                raise BrokerOrderUncertainError(
                    f"Binance returned an invalid fee cost for order {order_id}", order_id
                )
            if cost == 0:
                continue
            currency = str(fee.get("currency") or "").strip()
            if not currency:
                raise BrokerOrderUncertainError(
                    f"Binance omitted a non-zero fee currency for order {order_id}", order_id
                )
            if currency == self.config.quote_currency:
                quote_total += cost
            elif currency == base_currency:
                base_total += cost
            else:
                unknown_currencies.add(currency)
        if unknown_currencies:
            logger.warning(
                "Binance order %s charged fee currency/currencies not converted to %s: %s",
                order.get("id"),
                self.config.quote_currency,
                ", ".join(sorted(unknown_currencies)),
            )
        return quote_total, base_total

    def _assert_buy_notional(self, qty: float, limit_price: float) -> None:
        """Refuse a buy whose IOC limit could exceed the absolute order cap."""
        try:
            amount = Decimal(str(qty))
            price = Decimal(str(limit_price))
            cap = Decimal(str(self.config.max_order_notional))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise BrokerOrderRejectedError(
                "Binance MAX_ORDER_NOTIONAL_USD configuration is invalid"
            ) from exc
        if (
            not amount.is_finite()
            or not price.is_finite()
            or not cap.is_finite()
            or amount <= 0
            or price <= 0
            or cap <= 0
        ):
            raise BrokerOrderRejectedError(
                "Binance MAX_ORDER_NOTIONAL_USD configuration is invalid"
            )
        worst_case_notional = amount * price
        if worst_case_notional > cap:
            raise BrokerOrderRejectedError(
                "Binance buy worst-case notional "
                f"{worst_case_notional} exceeds MAX_ORDER_NOTIONAL_USD={cap}"
            )

    def _result_from_terminal_order(
        self, order: dict, symbol: str, side: str, requested_qty: float
    ) -> ExecutionResult:
        order_id = str(order.get("id") or "")
        if side not in {"buy", "sell"}:
            raise BrokerOrderUncertainError("Binance returned an order with an invalid side", order_id)
        if not math.isfinite(requested_qty) or requested_qty <= 0:
            raise BrokerOrderUncertainError(
                f"Binance order {order_id} had an invalid requested quantity", order_id
            )
        try:
            filled_qty = float(order.get("filled") or 0.0)
            cost = float(order.get("cost") or 0.0)
            price = float(order.get("average") or order.get("price") or 0.0)
        except (TypeError, ValueError) as exc:
            raise BrokerOrderUncertainError(
                f"Binance returned malformed fill fields for order {order_id}", order_id
            ) from exc
        if not math.isfinite(filled_qty) or filled_qty < 0:
            raise BrokerOrderUncertainError(
                f"Binance returned an invalid fill quantity for order {order_id}", order_id
            )
        if filled_qty > requested_qty * (1 + 1e-9) + 1e-12:
            raise BrokerOrderUncertainError(
                f"Binance returned a fill larger than the submitted quantity for order {order_id}",
                order_id,
            )
        if not math.isfinite(cost) or cost < 0:
            raise BrokerOrderUncertainError(
                f"Binance returned an invalid fill cost for order {order_id}", order_id
            )
        if filled_qty <= 0:
            raise BrokerOrderRejectedError(
                f"Binance order {order_id} reached terminal state without a fill"
            )
        if (not math.isfinite(price) or price <= 0) and cost > 0:
            price = cost / filled_qty
        if not math.isfinite(price) or price <= 0:
            raise BrokerOrderUncertainError(
                f"Binance filled order {order_id} but did not provide a valid fill price",
                order_id,
            )
        quote_fee, base_fee = self._fee_breakdown(order, symbol, filled_qty, cost)
        if side == "buy":
            # Binance's ``filled`` amount is gross. When commission is taken
            # in the base asset, only this net amount is available to sell.
            inventory_qty = filled_qty - base_fee
            if inventory_qty <= 0:
                raise BrokerOrderUncertainError(
                    f"Binance buy {order_id} has no provable net base inventory after fees",
                    order_id,
                )
        else:
            # A base-token sell fee is an additional debit from the wallet.
            # Returning the full debit keeps the persisted residual position in
            # line with spendable inventory. If it would exceed the tracked
            # position, StateStore fails closed and requires reconciliation.
            inventory_qty = filled_qty + base_fee
        fee = quote_fee + (base_fee * price)
        return ExecutionResult(qty=inventory_qty, price=price, fee=fee, order_id=order_id)

    def _create_order(
        self, symbol: str, side: str, qty: float, price_hint: float, client_order_id: str | None
    ) -> ExecutionResult:
        qty = self._amount(symbol, qty)
        if qty <= 0:
            raise BrokerOrderRejectedError("Binance amount rounds below the exchange minimum")
        if self.config.binance_order_mode != "ioc_limit":
            raise BrokerOrderRejectedError("Live Binance execution requires IOC limit orders")
        limit_price = self._limit_price(symbol, side, price_hint)
        if side == "buy":
            self._assert_buy_notional(qty, limit_price)
        params: dict[str, Any] = {"timeInForce": "IOC"}
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        try:
            order = self.ex.create_order(
                symbol=symbol,
                type="limit",
                side=side,
                amount=qty,
                price=limit_price,
                params=params,
            )
        except Exception as exc:
            if self._is_definite_rejection(exc):
                raise BrokerOrderRejectedError(f"Binance rejected {side} order: {exc}") from exc
            recovered = self._recover_by_client_id(symbol, client_order_id)
            if recovered is not None:
                return self._result_from_terminal_order(
                    self._refresh_to_terminal(recovered, symbol),
                    symbol,
                    side,
                    qty,
                )
            raise BrokerOrderUncertainError(
                f"Binance {side} request failed after submission may have begun",
                client_order_id or "",
            ) from exc
        if not isinstance(order, dict):
            raise BrokerOrderUncertainError("Binance returned a malformed order response")
        return self._result_from_terminal_order(
            self._refresh_to_terminal(order, symbol), symbol, side, qty
        )

    def buy(
        self,
        symbol: str,
        qty: float,
        price_hint: float,
        client_order_id: str | None = None,
    ) -> ExecutionResult:
        return self._create_order(symbol, "buy", qty, price_hint, client_order_id)

    def sell(
        self,
        symbol: str,
        qty: float,
        price_hint: float,
        client_order_id: str | None = None,
    ) -> ExecutionResult:
        return self._create_order(symbol, "sell", qty, price_hint, client_order_id)
