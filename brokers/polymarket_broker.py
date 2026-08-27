from __future__ import annotations

import logging
from decimal import Decimal

from models import ExecutionResult

from .base import Broker


logger = logging.getLogger(__name__)
_USDC_DECIMALS = Decimal("1000000")


class PolymarketBroker(Broker):
    """Polymarket CLOB broker using the official ``polymarket-client`` SDK.

    Symbols are local aliases defined in ``POLYMARKET_MARKETS``. Each alias
    resolves to exactly one YES or NO outcome-token id. Market orders use FOK
    so a strategy never records a partially filled outcome position.
    """

    venue = "polymarket"

    def __init__(self, config, state):
        self.config = config
        self.state = state

        if not config.polymarket_private_key:
            raise ValueError(
                "POLYMARKET_PRIVATE_KEY is required for live Polymarket trading"
            )

        try:
            from polymarket import RelayerApiKey, SecureClient
        except ImportError as exc:  # pragma: no cover - dependency failure path
            raise RuntimeError(
                "polymarket-client is required for Polymarket support; run pip install -r requirements.txt"
            ) from exc

        client_args = {"private_key": config.polymarket_private_key}
        if config.polymarket_wallet_address:
            client_args["wallet"] = config.polymarket_wallet_address

        has_relayer_key = bool(config.polymarket_relayer_api_key)
        has_relayer_address = bool(config.polymarket_relayer_api_key_address)
        if has_relayer_key != has_relayer_address:
            raise ValueError(
                "POLYMARKET_RELAYER_API_KEY and POLYMARKET_RELAYER_API_KEY_ADDRESS must be set together"
            )
        if has_relayer_key:
            client_args["api_key"] = RelayerApiKey(
                key=config.polymarket_relayer_api_key,
                address=config.polymarket_relayer_api_key_address,
            )

        self.client = SecureClient.create(**client_args)
        logger.info(
            "Polymarket CLOB client initialized for wallet=%s type=%s",
            self.client.wallet,
            self.client.wallet_type,
        )

    def _token_id(self, symbol: str) -> str:
        return self.config.resolve_polymarket_market(symbol)["token_id"]

    def get_equity(self, prices: dict[str, float]) -> float:
        # CLOB collateral is pUSD, represented in 6 decimal base units.
        balance = self.client.get_balance_allowance(asset_type="COLLATERAL")
        equity = float(Decimal(balance.balance) / _USDC_DECIMALS)

        for pos in self.state.get_positions_by_venue(self.venue):
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

        spend = Decimal(str(qty)) * Decimal(str(price_hint))
        slippage = Decimal(self.config.polymarket_slippage_bps) / Decimal("10000")
        response = self.client.place_market_order(
            token_id=self._token_id(symbol),
            side="BUY",
            amount=str(spend),
            max_spend=str(spend * (Decimal("1") + slippage)),
            order_type="FOK",
        )

        if not response.ok:
            logger.warning("Polymarket buy rejected for %s: %s", symbol, response.message)
            return ExecutionResult(qty=0, price=price_hint)

        # A buy makes pUSD and takes outcome shares. FOK prevents partial fills.
        filled_qty = float(response.taking_amount)
        spent = float(response.making_amount)
        if filled_qty <= 0:
            return ExecutionResult(qty=0, price=price_hint)

        return ExecutionResult(
            qty=filled_qty,
            price=spent / filled_qty,
            # The CLOB response does not provide a standalone fee amount.
            fee=0.0,
            order_id=str(response.order_id),
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

        slippage = Decimal(self.config.polymarket_slippage_bps) / Decimal("10000")
        response = self.client.place_market_order(
            token_id=self._token_id(symbol),
            side="SELL",
            shares=str(qty),
            min_price=str(Decimal(str(price_hint)) * (Decimal("1") - slippage)),
            order_type="FOK",
        )

        if not response.ok:
            logger.warning("Polymarket sell rejected for %s: %s", symbol, response.message)
            return ExecutionResult(qty=0, price=price_hint)

        # A sell makes outcome shares and takes pUSD. FOK prevents partial fills.
        filled_qty = float(response.making_amount)
        proceeds = float(response.taking_amount)
        if filled_qty <= 0:
            return ExecutionResult(qty=0, price=price_hint)

        return ExecutionResult(
            qty=filled_qty,
            price=proceeds / filled_qty,
            fee=0.0,
            order_id=str(response.order_id),
        )

    def close(self) -> None:
        self.client.close()
