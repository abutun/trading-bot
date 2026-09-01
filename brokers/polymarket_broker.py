from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from typing import Any

from models import ExecutionResult

from .base import Broker, BrokerOrderRejectedError, BrokerOrderUncertainError


logger = logging.getLogger(__name__)

# Both pUSD collateral and conditional outcome tokens use six decimal base units
# in the CLOB V2 signed-order payload.
_TOKEN_DECIMALS = Decimal("1000000")
_ONE = Decimal("1")
_ZERO = Decimal("0")
_SHARE_LOT = Decimal("0.01")


class PolymarketOrderUncertain(BrokerOrderUncertainError):
    """A CLOB order may have reached the venue but is not safely confirmed.

    The caller intentionally leaves its durable order intent unresolved when this
    is raised. Returning a zero fill here would permit an unsafe duplicate after
    a timeout, malformed response, or non-terminal FOK status.
    """

    def __init__(self, message: str, *, order_id: str | None = None):
        self.order_id = str(order_id or "").strip() or None
        if self.order_id and "order_id=" not in message:
            message = f"{message} (order_id={self.order_id})"
        super().__init__(message, external_order_id=self.order_id or "")


def _decimal(value: Any, label: str) -> Decimal:
    """Convert an external numeric value without accepting NaN or infinity."""
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label} is not a valid decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def _positive_decimal(value: Any, label: str) -> Decimal:
    result = _decimal(value, label)
    if result <= _ZERO:
        raise ValueError(f"{label} must be positive")
    return result


def _as_float(value: Decimal, label: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} is outside the supported numeric range")
    return result


def _short_address(address: str | None) -> str:
    if not address:
        return "signer"
    if len(address) <= 12:
        return address
    return f"{address[:6]}…{address[-4:]}"


class PolymarketBroker(Broker):
    """Fail-closed broker for the official Polymarket CLOB V2 client.

    Symbols are local aliases configured in ``POLYMARKET_MARKETS``. The V2
    protocol has no client-order-id field or venue-side idempotency guarantee.
    When supplied, the durable caller intent is hashed into the signed V2
    ``metadata`` bytes32 field for correlation only; it is never presented as a
    protocol client-order ID.
    """

    venue = "polymarket"

    def __init__(self, config, state):
        self.config = config
        self.state = state

        private_key = str(getattr(config, "polymarket_private_key", "") or "").strip()
        if not private_key:
            raise ValueError(
                "POLYMARKET_PRIVATE_KEY is required for live Polymarket trading"
            )

        host = str(getattr(config, "polymarket_clob_host", "") or "").strip().rstrip("/")
        if not host.startswith("https://"):
            raise ValueError("POLYMARKET_CLOB_HOST must be an https URL")

        chain_id = self._positive_int(
            getattr(config, "polymarket_chain_id", None), "POLYMARKET_CHAIN_ID"
        )
        signature_type = self._signature_type(
            getattr(config, "polymarket_signature_type", None)
        )
        funder = str(
            getattr(config, "polymarket_funder_address", "") or ""
        ).strip() or None
        if signature_type != 0 and not funder:
            raise ValueError(
                "POLYMARKET_FUNDER_ADDRESS is required for non-EOA Polymarket signatures"
            )

        try:
            from py_clob_client_v2.client import ClobClient
            from py_clob_client_v2.clob_types import (
                ApiCreds,
                AssetType,
                BalanceAllowanceParams,
                MarketOrderArgsV2,
                OrderType,
            )
        except ImportError as exc:  # pragma: no cover - dependency failure path
            raise RuntimeError(
                "py-clob-client-v2 is required for Polymarket CLOB V2 support; "
                "run pip install -r requirements.txt"
            ) from exc

        self._asset_type = AssetType
        self._balance_allowance_params_cls = BalanceAllowanceParams
        self._market_order_args_cls = MarketOrderArgsV2
        self._order_type_fok = OrderType.FOK

        credential_values = tuple(
            str(getattr(config, field, "") or "").strip()
            for field in (
                "polymarket_api_key",
                "polymarket_api_secret",
                "polymarket_api_passphrase",
            )
        )
        credential_count = sum(bool(value) for value in credential_values)
        if credential_count not in {0, 3}:
            # Never silently derive after a partial configuration. That could bind
            # an unintended account while hiding an operator configuration error.
            raise ValueError(
                "POLYMARKET_API_KEY, POLYMARKET_API_SECRET, and "
                "POLYMARKET_API_PASSPHRASE must be set together"
            )

        derive_credentials = (
            getattr(config, "polymarket_derive_api_credentials", False) is True
        )
        if credential_count == 0 and not derive_credentials:
            raise ValueError(
                "Set all Polymarket V2 API credentials or explicitly set "
                "POLYMARKET_DERIVE_API_CREDENTIALS=true"
            )

        creds = (
            ApiCreds(
                api_key=credential_values[0],
                api_secret=credential_values[1],
                api_passphrase=credential_values[2],
            )
            if credential_count == 3
            else None
        )
        client = ClobClient(
            host=host,
            chain_id=chain_id,
            key=private_key,
            creds=creds,
            signature_type=signature_type,
            funder=funder,
            # Retrying an unactionably uncertain POST can duplicate a trade.
            retry_on_error=False,
        )

        credential_source = "configured"
        if creds is None:
            try:
                derived = client.derive_api_key()
            except Exception as exc:
                raise RuntimeError(
                    "Could not explicitly derive Polymarket V2 API credentials"
                ) from exc
            if not self._valid_api_creds(derived):
                raise RuntimeError("Polymarket returned incomplete derived API credentials")
            client.set_api_creds(derived)
            credential_source = "derived"

        self.client = client
        logger.info(
            "Polymarket CLOB V2 client initialized host=%s chain_id=%s "
            "signature_type=%s funder=%s credentials=%s",
            host,
            chain_id,
            signature_type,
            _short_address(funder),
            credential_source,
        )

    @staticmethod
    def _positive_int(value: Any, label: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{label} must be a positive integer")
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be a positive integer") from exc
        if result <= 0:
            raise ValueError(f"{label} must be a positive integer")
        return result

    @staticmethod
    def _signature_type(value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("POLYMARKET_SIGNATURE_TYPE must be 0, 1, 2, or 3")
        try:
            signature_type = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "POLYMARKET_SIGNATURE_TYPE must be 0, 1, 2, or 3"
            ) from exc
        if signature_type not in {0, 1, 2, 3}:
            raise ValueError("POLYMARKET_SIGNATURE_TYPE must be 0, 1, 2, or 3")
        return signature_type

    @staticmethod
    def _valid_api_creds(creds: Any) -> bool:
        return all(
            bool(str(getattr(creds, field, "") or "").strip())
            for field in ("api_key", "api_secret", "api_passphrase")
        )

    def _token_id(self, symbol: str) -> str:
        token_id = str(
            self.config.resolve_polymarket_market(symbol).get("token_id", "")
        ).strip()
        if not token_id:
            raise ValueError(f"Polymarket market mapping for {symbol!r} has no token_id")
        return token_id

    def get_equity(self, prices: dict[str, float]) -> float:
        """Return pUSD collateral plus locally tracked outcome-token positions.

        CLOB V2's current ``get_balance_allowance`` returns a dictionary whose
        ``balance`` is an integer-like string in six-decimal collateral units.
        Malformed account responses are surfaced rather than interpreted as zero,
        because a false zero balance can bypass risk controls.
        """
        params = self._balance_allowance_params_cls(
            asset_type=self._asset_type.COLLATERAL
        )
        response = self.client.get_balance_allowance(params=params)
        if not isinstance(response, Mapping):
            raise RuntimeError("Polymarket balance response was not a V2 dictionary")
        if "balance" not in response:
            raise RuntimeError("Polymarket balance response omitted balance")

        balance_units = _decimal(response["balance"], "Polymarket collateral balance")
        if balance_units < _ZERO:
            raise RuntimeError("Polymarket collateral balance cannot be negative")
        equity = balance_units / _TOKEN_DECIMALS

        for pos in self.state.get_positions_by_venue(self.venue):
            price = _positive_decimal(
                prices.get(pos.pair_id, pos.entry_price),
                f"price for {pos.pair_id}",
            )
            quantity = _positive_decimal(pos.qty, f"quantity for {pos.pair_id}")
            equity += quantity * price

        return _as_float(equity, "Polymarket equity")

    def buy(
        self,
        symbol: str,
        qty: float,
        price_hint: float,
        client_order_id: str | None = None,
    ) -> ExecutionResult:
        return self._execute_fok(
            symbol=symbol,
            qty=qty,
            price_hint=price_hint,
            side="BUY",
            client_order_id=client_order_id,
        )

    def sell(
        self,
        symbol: str,
        qty: float,
        price_hint: float,
        client_order_id: str | None = None,
    ) -> ExecutionResult:
        return self._execute_fok(
            symbol=symbol,
            qty=qty,
            price_hint=price_hint,
            side="SELL",
            client_order_id=client_order_id,
        )

    def _execute_fok(
        self,
        *,
        symbol: str,
        qty: float,
        price_hint: float,
        side: str,
        client_order_id: str | None,
    ) -> ExecutionResult:
        try:
            token_id = self._token_id(symbol)
            requested_shares = self._order_quantity(qty, side)
            price_bound = self._price_bound(token_id, price_hint, side)
            amount = (
                (requested_shares * price_bound).quantize(
                    _SHARE_LOT, rounding=ROUND_DOWN
                )
                if side == "BUY"
                else requested_shares
            )
            if amount <= _ZERO:
                raise ValueError("Polymarket order amount rounds to zero")
            if side == "BUY":
                self._assert_buy_notional(amount)
        except Exception as exc:
            # All work above is local or read-only metadata lookup; no signed
            # order has been posted, so this is a conclusive rejection rather
            # than an order outcome requiring reconciliation.
            raise BrokerOrderRejectedError(
                f"Polymarket order cannot be created for {symbol}: {exc}"
            ) from exc

        correlation_id = str(client_order_id or "").strip() or None
        correlation_label = "none"
        args: dict[str, Any] = {
            "token_id": token_id,
            "amount": _as_float(amount, "Polymarket order amount"),
            "side": side,
            "price": _as_float(price_bound, "Polymarket price bound"),
            "order_type": self._order_type_fok,
        }
        if correlation_id:
            # V2 has no client_order_id argument. ``metadata`` is a signed
            # bytes32 field, so retain only a domain-separated correlation hash.
            args["metadata"] = self._correlation_metadata(correlation_id)
            correlation_label = args["metadata"][2:18]

        order_args = self._market_order_args_cls(**args)
        logger.info(
            "Submitting Polymarket V2 FOK side=%s symbol=%s amount=%s price_bound=%s correlation=%s",
            side,
            symbol,
            amount,
            price_bound,
            correlation_label,
        )

        try:
            signed_order = self.client.create_market_order(order_args)
            expected_shares, expected_quote = self._signed_order_amounts(
                signed_order, side
            )
            if side == "BUY":
                # The SDK's signed payload, rather than its local order
                # arguments, is the last source of truth before the POST.
                # Never submit if it would spend above the absolute cap.
                self._assert_buy_notional(expected_quote)
            self._validate_signed_price(
                expected_shares, expected_quote, price_bound, side
            )
        except Exception as exc:
            # create_market_order performs local signing / read-only market
            # metadata work. The POST below is the first side-effecting call.
            raise BrokerOrderRejectedError(
                f"Polymarket V2 FOK could not be created for {symbol}: {exc}"
            ) from exc

        try:
            response = self.client.post_order(
                signed_order, order_type=self._order_type_fok
            )
        except Exception as exc:
            # The signed order may have reached CLOB even if its response did
            # not. Do not let the bot place a replacement automatically.
            raise PolymarketOrderUncertain(
                f"Polymarket V2 FOK confirmation failed for {symbol}"
            ) from exc

        return self._verified_execution(
            response=response,
            expected_shares=expected_shares,
            expected_quote=expected_quote,
            price_bound=price_bound,
            side=side,
            symbol=symbol,
            correlation_label=correlation_label,
        )

    @staticmethod
    def _correlation_metadata(client_order_id: str) -> str:
        digest = hashlib.sha256(
            f"trading-bot:polymarket-v2:{client_order_id}".encode("utf-8")
        ).hexdigest()
        return f"0x{digest}"

    @staticmethod
    def _order_quantity(qty: float, side: str) -> Decimal:
        raw = _positive_decimal(qty, "Polymarket quantity")
        normalized = raw.quantize(_SHARE_LOT, rounding=ROUND_DOWN)
        if normalized <= _ZERO:
            raise ValueError("Polymarket quantity is below the 0.01-share lot size")
        if side == "SELL" and normalized != raw:
            # The V2 market-order builder rounds sell sizes down to two decimals.
            # Refusing is safer than returning a full exit that silently leaves
            # an untracked residual outcome-token balance.
            raise ValueError(
                "Polymarket sell quantity must be an exact 0.01-share multiple"
            )
        return normalized

    def _price_bound(self, token_id: str, price_hint: float, side: str) -> Decimal:
        reference = _positive_decimal(price_hint, "Polymarket price hint")
        if reference >= _ONE:
            raise ValueError("Polymarket price hint must be below 1")

        slippage_bps = _decimal(
            getattr(self.config, "polymarket_slippage_bps", None),
            "POLYMARKET_SLIPPAGE_BPS",
        )
        global_slippage_bps = _decimal(
            getattr(self.config, "max_order_slippage_bps", None),
            "MAX_ORDER_SLIPPAGE_BPS",
        )
        if slippage_bps < _ZERO or slippage_bps >= Decimal("10000"):
            raise ValueError("POLYMARKET_SLIPPAGE_BPS must be in [0, 10000)")
        if global_slippage_bps < _ZERO or global_slippage_bps >= Decimal("10000"):
            raise ValueError("MAX_ORDER_SLIPPAGE_BPS must be in [0, 10000)")
        if slippage_bps > global_slippage_bps:
            raise ValueError(
                "POLYMARKET_SLIPPAGE_BPS cannot exceed MAX_ORDER_SLIPPAGE_BPS"
            )

        try:
            tick_size = _positive_decimal(
                self.client.get_tick_size(token_id), "Polymarket tick size"
            )
        except Exception as exc:
            raise PolymarketOrderUncertain(
                f"Could not obtain Polymarket tick size for {token_id}"
            ) from exc
        if tick_size >= Decimal("0.5"):
            raise ValueError("Polymarket tick size is invalid")

        upper = _ONE - tick_size
        if upper <= tick_size:
            raise ValueError("Polymarket price range is invalid")
        slippage = slippage_bps / Decimal("10000")
        precision = max(0, -tick_size.as_tuple().exponent)
        quantum = Decimal(1).scaleb(-precision)

        if side == "BUY":
            raw_bound = min(reference * (_ONE + slippage), upper)
            price_bound = raw_bound.quantize(quantum, rounding=ROUND_DOWN)
            if price_bound < tick_size:
                raise ValueError("Polymarket buy price bound is below the market tick")
        else:
            raw_bound = max(reference * (_ONE - slippage), tick_size)
            price_bound = raw_bound.quantize(quantum, rounding=ROUND_UP)
            if price_bound > upper:
                raise ValueError("Polymarket sell price bound is above the market range")

        if not tick_size <= price_bound <= upper:
            raise ValueError("Polymarket computed an invalid price bound")
        return price_bound

    def _assert_buy_notional(self, quote_amount: Decimal) -> None:
        """Enforce the absolute spend cap on the exact V2 signed quote amount."""
        max_notional = _positive_decimal(
            getattr(self.config, "max_order_notional", None),
            "MAX_ORDER_NOTIONAL_USD",
        )
        if quote_amount > max_notional:
            raise ValueError(
                "Polymarket buy worst-case notional "
                f"{quote_amount} exceeds MAX_ORDER_NOTIONAL_USD={max_notional}"
            )

    @staticmethod
    def _signed_order_amounts(signed_order: Any, side: str) -> tuple[Decimal, Decimal]:
        """Return (outcome shares, pUSD) exactly as signed in the V2 order."""
        try:
            maker_raw = _positive_decimal(
                getattr(signed_order, "makerAmount"), "V2 signed makerAmount"
            )
            taker_raw = _positive_decimal(
                getattr(signed_order, "takerAmount"), "V2 signed takerAmount"
            )
        except AttributeError as exc:
            raise PolymarketOrderUncertain(
                "Polymarket V2 returned a signed order without fixed-point amounts"
            ) from exc

        if (
            maker_raw != maker_raw.to_integral_value()
            or taker_raw != taker_raw.to_integral_value()
        ):
            raise PolymarketOrderUncertain(
                "Polymarket V2 signed order amounts were not base-unit integers"
            )
        maker = maker_raw / _TOKEN_DECIMALS
        taker = taker_raw / _TOKEN_DECIMALS
        if side == "BUY":
            return taker, maker
        return maker, taker

    @staticmethod
    def _validate_signed_price(
        shares: Decimal, quote: Decimal, price_bound: Decimal, side: str
    ) -> None:
        if shares <= _ZERO or quote <= _ZERO:
            raise PolymarketOrderUncertain("Polymarket V2 signed a zero-value order")
        signed_price = quote / shares
        if not signed_price.is_finite() or signed_price <= _ZERO:
            raise PolymarketOrderUncertain("Polymarket V2 signed an invalid price")
        if side == "BUY" and signed_price > price_bound:
            raise PolymarketOrderUncertain(
                "Polymarket V2 signed a buy above its configured price bound"
            )
        if side == "SELL" and signed_price < price_bound:
            raise PolymarketOrderUncertain(
                "Polymarket V2 signed a sell below its configured price bound"
            )

    def _verified_execution(
        self,
        *,
        response: Any,
        expected_shares: Decimal,
        expected_quote: Decimal,
        price_bound: Decimal,
        side: str,
        symbol: str,
        correlation_label: str,
    ) -> ExecutionResult:
        if not isinstance(response, Mapping):
            raise PolymarketOrderUncertain(
                f"Polymarket V2 returned a non-dictionary FOK response for {symbol}"
            )

        order_id = str(response.get("orderID") or "").strip()
        status = str(response.get("status") or "").strip().casefold()
        message = self._response_message(response)
        if response.get("success") is not True:
            raise PolymarketOrderUncertain(
                f"Polymarket V2 FOK was not confirmed as successful for {symbol}"
                f"{self._message_suffix(message)}",
                order_id=order_id,
            )
        if not order_id:
            raise PolymarketOrderUncertain(
                f"Polymarket V2 FOK success response omitted orderID for {symbol}"
            )
        if status != "matched":
            # A FOK must be terminally matched. In particular, V2 `delayed`
            # cannot prove no order hit the matching engine, so a zero result
            # would invite a duplicate submission.
            raise PolymarketOrderUncertain(
                f"Polymarket V2 FOK is not terminally matched "
                f"(status={status or 'missing'}){self._message_suffix(message)}",
                order_id=order_id,
            )
        if message:
            raise PolymarketOrderUncertain(
                "Polymarket V2 FOK matched with an unexpected error message",
                order_id=order_id,
            )

        share_field, quote_field = (
            ("takingAmount", "makingAmount")
            if side == "BUY"
            else ("makingAmount", "takingAmount")
        )
        try:
            actual_shares, scale = self._response_amount(
                response.get(share_field), expected_shares, share_field
            )
            quote_raw = _positive_decimal(response.get(quote_field), quote_field)
            actual_quote = quote_raw / scale
        except (TypeError, ValueError) as exc:
            raise PolymarketOrderUncertain(
                "Polymarket V2 FOK returned invalid fill amounts",
                order_id=order_id,
            ) from exc

        if actual_quote <= _ZERO or not actual_quote.is_finite():
            raise PolymarketOrderUncertain(
                "Polymarket V2 FOK returned a non-positive quote fill",
                order_id=order_id,
            )
        if actual_shares != expected_shares or actual_quote != expected_quote:
            raise PolymarketOrderUncertain(
                "Polymarket V2 FOK was not an exact signed fill", order_id=order_id
            )

        price = actual_quote / actual_shares
        if not price.is_finite() or price <= _ZERO:
            raise PolymarketOrderUncertain(
                "Polymarket V2 FOK returned an invalid average price",
                order_id=order_id,
            )
        if side == "BUY" and price > price_bound:
            raise PolymarketOrderUncertain(
                "Polymarket V2 FOK exceeded its buy price bound", order_id=order_id
            )
        if side == "SELL" and price < price_bound:
            raise PolymarketOrderUncertain(
                "Polymarket V2 FOK breached its sell price bound", order_id=order_id
            )

        logger.info(
            "Verified Polymarket V2 FOK order_id=%s side=%s symbol=%s shares=%s "
            "price=%s correlation=%s",
            order_id,
            side,
            symbol,
            actual_shares,
            price,
            correlation_label,
        )
        return ExecutionResult(
            qty=_as_float(actual_shares, "Polymarket filled quantity"),
            price=_as_float(price, "Polymarket filled price"),
            fee=0.0,
            order_id=order_id,
        )

    @staticmethod
    def _response_amount(
        value: Any, expected: Decimal, field: str
    ) -> tuple[Decimal, Decimal]:
        """Decode decimal strings and fixed-point strings only if exact.

        Current CLOB examples use decimal amounts (``"20"``), while generated
        API schemas also describe six-decimal fixed-point integers. Both are
        accepted only when they equal the signed amount exactly.
        """
        raw = _positive_decimal(value, field)
        if raw == expected:
            return raw, _ONE
        fixed_point = raw / _TOKEN_DECIMALS
        if fixed_point == expected:
            return fixed_point, _TOKEN_DECIMALS
        raise ValueError(f"{field} does not equal the signed FOK amount")

    @staticmethod
    def _response_message(response: Mapping[str, Any]) -> str:
        for field in ("errorMsg", "error", "message"):
            value = response.get(field)
            if value:
                return str(value)[:500]
        return ""

    @staticmethod
    def _message_suffix(message: str) -> str:
        return f": {message}" if message else ""

    def close(self) -> None:
        # py-clob-client-v2 currently exposes no public close method, while test
        # doubles and future SDK releases may. Do not invoke retired V1 APIs.
        close = getattr(self.client, "close", None)
        if callable(close):
            close()
