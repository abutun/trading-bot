from __future__ import annotations

import logging
import math
import time
from decimal import Decimal, ROUND_DOWN

from eth_account import Account
from web3 import Web3
from web3.exceptions import TimeExhausted

from models import ExecutionResult

from .base import Broker, BrokerOrderRejectedError, BrokerOrderUncertainError


logger = logging.getLogger(__name__)


TOKEN_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "spender", "type": "address"}],
        "name": "allowance",
        "outputs": [{"name": "remaining", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "success", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "decimals", "type": "uint8"}],
        "type": "function",
    },
]


ROUTER_ABI = [
    {
        "constant": True,
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "tokenIn", "type": "address"},
            {"name": "tokenOut", "type": "address"},
        ],
        "name": "getAmountsOut",
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMin", "type": "uint256"},
            {"name": "path", "type": "address[]"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "name": "swapExactTokensForTokens",
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
        "type": "function",
    },
]


class EVMBroker(Broker):
    venue = "evm"

    def __init__(self, config, state):
        self.config = config
        self.state = state

        if not config.evm_private_key:
            raise ValueError("EVM_PRIVATE_KEY is required")

        if not config.evm_rpc_url:
            raise ValueError("EVM_RPC_URL is required")

        if not config.evm_router_address:
            raise ValueError("EVM_ROUTER_ADDRESS is required")

        self.w3 = Web3(
            Web3.HTTPProvider(
                config.evm_rpc_url,
                request_kwargs={"timeout": config.evm_rpc_timeout_seconds},
            )
        )

        if not self.w3.is_connected():
            raise ConnectionError("Cannot connect to EVM RPC")
        connected_chain_id = int(self.w3.eth.chain_id)
        if connected_chain_id != config.evm_chain_id:
            raise ValueError(
                f"EVM RPC chain ID {connected_chain_id} does not match EVM_CHAIN_ID "
                f"{config.evm_chain_id}"
            )

        self.account = Account.from_key(config.evm_private_key)
        self.wallet = self.account.address

        self.router = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.evm_router_address),
            abi=ROUTER_ABI,
        )
        router_code = self.w3.eth.get_code(
            Web3.to_checksum_address(config.evm_router_address)
        )
        if not router_code:
            raise ValueError("EVM_ROUTER_ADDRESS has no deployed contract code")

        self._nonce = self.w3.eth.get_transaction_count(self.wallet, "pending")
        self._decimals_cache: dict[str, int] = {}

    def _token(self, address: str):
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(address),
            abi=TOKEN_ABI,
        )

    def _decimals(self, address: str) -> int:
        checksum = Web3.to_checksum_address(address)

        if checksum not in self._decimals_cache:
            self._decimals_cache[checksum] = int(
                self._token(checksum).functions.decimals().call()
            )

        return self._decimals_cache[checksum]

    def _balance_wei(self, address: str) -> int:
        return int(
            self._token(address).functions.balanceOf(self.wallet).call()
        )

    def _allowance_wei(self, token_address: str, spender: str) -> int:
        return int(
            self._token(token_address).functions.allowance(
                self.wallet, Web3.to_checksum_address(spender)
            ).call()
        )

    def _gas_price_wei(self) -> int:
        if self.config.evm_gas_price_gwei is not None:
            gas_price = int(Web3.to_wei(self.config.evm_gas_price_gwei, "gwei"))
        else:
            gas_price = int(self.w3.eth.gas_price)

        max_gas_price = int(
            Web3.to_wei(self.config.evm_max_gas_price_gwei, "gwei")
        )
        if gas_price <= 0 or gas_price > max_gas_price:
            raise BrokerOrderRejectedError(
                "EVM gas price is outside the configured safety cap "
                f"({gas_price} wei > {max_gas_price} wei)"
            )
        return gas_price

    def _set_bounded_gas(self, tx: dict, fallback: int) -> None:
        """Apply a modest gas buffer without permitting an unbounded spend."""
        estimated = int(tx.get("gas") or fallback)
        if estimated <= 21_000:
            estimated = fallback
        gas_limit = int(math.ceil(estimated * 1.2))
        if gas_limit > self.config.evm_max_gas_limit:
            raise BrokerOrderRejectedError(
                f"EVM transaction requires gas limit {gas_limit}, above configured "
                f"EVM_MAX_GAS_LIMIT={self.config.evm_max_gas_limit}"
            )
        tx["gas"] = gas_limit

    def _wait_for_confirmations(self, tx_hash, tx_hash_hex: str):
        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=self.config.evm_receipt_timeout_seconds
            )
        except (TimeExhausted, TimeoutError) as exc:
            raise BrokerOrderUncertainError(
                f"EVM transaction {tx_hash_hex} was broadcast but receipt timed out",
                tx_hash_hex,
            ) from exc
        except Exception as exc:
            raise BrokerOrderUncertainError(
                f"EVM transaction {tx_hash_hex} was broadcast but receipt lookup failed",
                tx_hash_hex,
            ) from exc

        status = int(receipt.get("status", getattr(receipt, "status", 0)))
        if status != 1:
            # The transaction was accepted by the chain and consumed nonce/gas,
            # even though its state transition reverted. Treat it as requiring
            # durable reconciliation rather than a pre-submission rejection;
            # automatic retry can repeat approvals/swaps and burn more gas.
            raise BrokerOrderUncertainError(
                f"EVM transaction reverted after broadcast: {tx_hash_hex}", tx_hash_hex
            )

        receipt_block = int(
            receipt.get("blockNumber", getattr(receipt, "blockNumber", 0))
        )
        required_block = receipt_block + self.config.evm_min_confirmations - 1
        deadline = time.monotonic() + self.config.evm_receipt_timeout_seconds
        try:
            while int(self.w3.eth.block_number) < required_block:
                if time.monotonic() >= deadline:
                    raise BrokerOrderUncertainError(
                        f"EVM transaction {tx_hash_hex} is mined but did not reach "
                        f"{self.config.evm_min_confirmations} confirmations in time",
                        tx_hash_hex,
                    )
                time.sleep(1)
        except BrokerOrderUncertainError:
            raise
        except Exception as exc:
            raise BrokerOrderUncertainError(
                f"Cannot verify confirmations for EVM transaction {tx_hash_hex}",
                tx_hash_hex,
            ) from exc
        return receipt

    def _send_transaction(self, tx: dict) -> str:
        tx["from"] = self.wallet
        tx["nonce"] = self._nonce

        if "chainId" not in tx:
            tx["chainId"] = self.config.evm_chain_id

        # The generic router ABI produces legacy-compatible transactions.  A
        # single bounded gasPrice is intentional here: accepting an RPC-supplied
        # dynamic maxFeePerGas would evade the configured cost cap.
        tx.pop("maxFeePerGas", None)
        tx.pop("maxPriorityFeePerGas", None)
        tx["gasPrice"] = self._gas_price_wei()

        signed = self.account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")

        try:
            tx_hash = self.w3.eth.send_raw_transaction(raw)
        except Exception:
            self._nonce = self.w3.eth.get_transaction_count(self.wallet, "pending")
            raise

        # The node accepted the raw transaction. Advance the local nonce even
        # if waiting for the receipt times out; retrying may otherwise replace
        # an already-broadcast transaction.
        self._nonce += 1
        tx_hash_hex = tx_hash.hex()
        self._wait_for_confirmations(tx_hash, tx_hash_hex)
        return tx_hash_hex

    def _ensure_approval(self, token_address: str, amount_wei: int) -> None:
        if amount_wei <= 0:
            return

        spender = Web3.to_checksum_address(self.config.evm_router_address)
        allowance = self._allowance_wei(token_address, spender)

        if allowance >= amount_wei:
            return

        # Some ERC-20s (notably USDT variants) reject changing a non-zero
        # allowance directly to another non-zero value.
        if allowance > 0:
            reset_tx = self._token(token_address).functions.approve(
                spender, 0
            ).build_transaction({"from": self.wallet})
            self._set_bounded_gas(reset_tx, 60_000)
            self._send_transaction(reset_tx)

        approval_amount = 2**256 - 1 if self.config.evm_approve_max else amount_wei
        approval_tx = self._token(token_address).functions.approve(
            spender, approval_amount
        ).build_transaction({"from": self.wallet})
        self._set_bounded_gas(approval_tx, 60_000)
        self._send_transaction(approval_tx)

    def _get_amount_out(self, amount_in_wei: int, token_in: str, token_out: str) -> int:
        amounts = self.router.functions.getAmountsOut(
            amount_in_wei,
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
        ).call()

        return int(amounts[-1])

    def _mapping(self, symbol: str) -> dict[str, str]:
        if symbol not in self.config.evm_symbols:
            raise ValueError(f"EVM_SYMBOLS is missing mapping for {symbol}")

        return self.config.evm_symbols[symbol]

    @staticmethod
    def _to_units(value: float, decimals: int) -> int:
        if not math.isfinite(value) or value <= 0:
            return 0
        scaled = Decimal(str(value)) * (Decimal(10) ** decimals)
        return int(scaled.to_integral_value(rounding=ROUND_DOWN))

    @staticmethod
    def _quote_in_units(qty: float, price_hint: float, quote_decimals: int) -> int:
        """Calculate the exact token input without a binary-float product."""
        if (
            not math.isfinite(qty)
            or not math.isfinite(price_hint)
            or qty <= 0
            or price_hint <= 0
            or quote_decimals < 0
        ):
            return 0
        value = (
            Decimal(str(qty))
            * Decimal(str(price_hint))
            * (Decimal(10) ** quote_decimals)
        )
        return int(value.to_integral_value(rounding=ROUND_DOWN))

    def _assert_buy_notional(self, quote_in_wei: int, quote_decimals: int) -> None:
        """Enforce the absolute cap against the actual rounded token input."""
        if quote_in_wei <= 0 or quote_decimals < 0:
            raise BrokerOrderRejectedError("EVM buy quote amount is invalid")
        try:
            max_notional = Decimal(str(self.config.max_order_notional))
        except Exception as exc:
            raise BrokerOrderRejectedError(
                "EVM MAX_ORDER_NOTIONAL_USD configuration is invalid"
            ) from exc
        if not max_notional.is_finite() or max_notional <= 0:
            raise BrokerOrderRejectedError(
                "EVM MAX_ORDER_NOTIONAL_USD configuration is invalid"
            )
        actual_notional = Decimal(quote_in_wei) / (Decimal(10) ** quote_decimals)
        if actual_notional > max_notional:
            raise BrokerOrderRejectedError(
                "EVM buy quote notional "
                f"{actual_notional} exceeds MAX_ORDER_NOTIONAL_USD={max_notional}"
            )

    def _assert_expected_price(
        self, side: str, expected_price: float, price_hint: float
    ) -> None:
        if (
            side not in {"buy", "sell"}
            or not math.isfinite(expected_price)
            or not math.isfinite(price_hint)
            or expected_price <= 0
            or price_hint <= 0
        ):
            raise BrokerOrderRejectedError("EVM quote or reference price is invalid")
        allowed = self.config.max_order_slippage_bps / 10_000
        too_expensive = side == "buy" and expected_price > price_hint * (1 + allowed)
        too_cheap = side == "sell" and expected_price < price_hint * (1 - allowed)
        if too_expensive or too_cheap:
            raise BrokerOrderRejectedError(
                f"EVM executable quote {expected_price:.12g} exceeds the permitted "
                f"{self.config.max_order_slippage_bps} bps deviation from reference "
                f"{price_hint:.12g}"
            )

    def _amount_out_minimum(
        self,
        *,
        side: str,
        amount_in_wei: int,
        expected_out_wei: int,
        input_decimals: int,
        output_decimals: int,
        price_hint: float,
    ) -> int:
        """Return a router ``amountOutMin`` that cannot widen the fill bound.

        The router quote has already been checked against the global reference
        price.  Applying a second percentage loss directly to that quote would
        compound the two tolerances: a quote at the global edge could then fill
        beyond it.  Instead, calculate two independent output floors and use
        the stricter one:

        * the globally approved price bound relative to ``price_hint``; and
        * the venue-local deterioration allowed from the executable quote.

        ``amountOutMin`` is an integer token amount, so all floors are rounded
        *up*.  Rounding down even one unit can violate a strict price ceiling
        or floor on small trades.
        """
        if side not in {"buy", "sell"}:
            raise BrokerOrderRejectedError("EVM order side is invalid")
        if (
            amount_in_wei <= 0
            or expected_out_wei <= 0
            or input_decimals < 0
            or output_decimals < 0
            or not math.isfinite(price_hint)
            or price_hint <= 0
        ):
            raise BrokerOrderRejectedError("EVM amount-out safety inputs are invalid")

        try:
            global_slippage = Decimal(str(self.config.max_order_slippage_bps)) / Decimal(
                "10000"
            )
            local_slippage = Decimal(str(self.config.evm_slippage_bps)) / Decimal(
                "10000"
            )
        except Exception as exc:
            raise BrokerOrderRejectedError("EVM slippage configuration is invalid") from exc
        if (
            not global_slippage.is_finite()
            or not local_slippage.is_finite()
            or global_slippage < 0
            or local_slippage < 0
            or global_slippage >= 1
            or local_slippage >= 1
            or local_slippage > global_slippage
        ):
            raise BrokerOrderRejectedError("EVM slippage configuration is outside safety caps")

        reference_price = Decimal(str(price_hint))
        token_scale = Decimal(10) ** (output_decimals - input_decimals)
        amount_in = Decimal(amount_in_wei)
        expected_out = Decimal(expected_out_wei)

        if side == "buy":
            # quote/base price: receiving less base makes a buy more expensive.
            approved_price = reference_price * (Decimal(1) + global_slippage)
            reference_floor = amount_in * token_scale / approved_price
        else:
            # quote/base price: receiving less quote makes a sell cheaper.
            approved_price = reference_price * (Decimal(1) - global_slippage)
            if approved_price <= 0:
                raise BrokerOrderRejectedError("EVM sell price floor is non-positive")
            reference_floor = amount_in * token_scale * approved_price

        local_floor = expected_out * (Decimal(1) - local_slippage)
        minimum = max(reference_floor, local_floor)
        amount_out_min = int(minimum.to_integral_value(rounding=ROUND_DOWN))
        if Decimal(amount_out_min) < minimum:
            amount_out_min += 1
        if amount_out_min <= 0 or amount_out_min > expected_out_wei:
            raise BrokerOrderRejectedError(
                "EVM quote cannot satisfy the configured reference and local slippage bounds"
            )
        return amount_out_min

    def get_equity(self, prices: dict[str, float]) -> float:
        equity = 0.0
        seen_quote_tokens: set[str] = set()

        # Sum quote token balances once per unique quote token.
        # This assumes quote tokens are stablecoins like USDC/USDT and valued 1:1.
        for symbol, mapping in self.config.evm_symbols.items():
            quote = mapping["quote"]

            if quote not in seen_quote_tokens:
                decimals = self._decimals(quote)
                equity += self._balance_wei(quote) / (10**decimals)
                seen_quote_tokens.add(quote)

        # Add tracked base token positions.
        for pos in self.state.get_positions_by_venue("evm"):
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
        mapping = self._mapping(symbol)
        base = mapping["base"]
        quote = mapping["quote"]

        base_decimals = self._decimals(base)
        quote_decimals = self._decimals(quote)

        if qty <= 0 or price_hint <= 0:
            return ExecutionResult(qty=0, price=price_hint)

        quote_in = self._quote_in_units(qty, price_hint, quote_decimals)
        if quote_in <= 0:
            return ExecutionResult(qty=0, price=price_hint)
        self._assert_buy_notional(quote_in, quote_decimals)

        quote_before = self._balance_wei(quote)
        base_before = self._balance_wei(base)

        if quote_before < quote_in:
            raise BrokerOrderRejectedError("Insufficient quote token balance")

        expected_out = self._get_amount_out(quote_in, quote, base)
        if expected_out <= 0:
            raise BrokerOrderRejectedError("No EVM liquidity for buy")

        expected_qty = expected_out / (10**base_decimals)
        expected_price = (quote_in / (10**quote_decimals)) / expected_qty
        self._assert_expected_price("buy", expected_price, price_hint)

        amount_out_min = self._amount_out_minimum(
            side="buy",
            amount_in_wei=quote_in,
            expected_out_wei=expected_out,
            input_decimals=quote_decimals,
            output_decimals=base_decimals,
            price_hint=price_hint,
        )

        self._ensure_approval(quote, quote_in)

        deadline = int(time.time()) + 120

        tx = self.router.functions.swapExactTokensForTokens(
            quote_in,
            amount_out_min,
            [
                Web3.to_checksum_address(quote),
                Web3.to_checksum_address(base),
            ],
            self.wallet,
            deadline,
        ).build_transaction({"from": self.wallet})

        self._set_bounded_gas(tx, 250_000)
        tx_hash = self._send_transaction(tx)

        try:
            actual_qty = (self._balance_wei(base) - base_before) / (10**base_decimals)
            quote_spent = (quote_before - self._balance_wei(quote)) / (10**quote_decimals)
        except Exception as exc:
            raise BrokerOrderUncertainError(
                f"EVM buy {tx_hash} is confirmed but balance reconciliation failed",
                tx_hash,
            ) from exc

        if actual_qty <= 0:
            raise BrokerOrderUncertainError(
                f"EVM buy {tx_hash} is confirmed but no base-token balance increase was observed",
                tx_hash,
            )

        actual_price = quote_spent / actual_qty if actual_qty > 0 else price_hint
        return ExecutionResult(
            qty=actual_qty,
            price=actual_price,
            fee=0.0,
            order_id=tx_hash,
        )

    def sell(
        self,
        symbol: str,
        qty: float,
        price_hint: float,
        client_order_id: str | None = None,
    ) -> ExecutionResult:
        mapping = self._mapping(symbol)
        base = mapping["base"]
        quote = mapping["quote"]

        base_decimals = self._decimals(base)
        quote_decimals = self._decimals(quote)

        if qty <= 0:
            return ExecutionResult(qty=0, price=price_hint)

        base_in = self._to_units(qty, base_decimals)
        base_before = self._balance_wei(base)
        quote_before = self._balance_wei(quote)

        if base_before < base_in:
            raise BrokerOrderRejectedError("Insufficient base token balance")

        expected_out = self._get_amount_out(base_in, base, quote)
        if expected_out <= 0:
            raise BrokerOrderRejectedError("No EVM liquidity for sell")

        expected_price = (expected_out / (10**quote_decimals)) / (
            base_in / (10**base_decimals)
        )
        self._assert_expected_price("sell", expected_price, price_hint)

        amount_out_min = self._amount_out_minimum(
            side="sell",
            amount_in_wei=base_in,
            expected_out_wei=expected_out,
            input_decimals=base_decimals,
            output_decimals=quote_decimals,
            price_hint=price_hint,
        )

        self._ensure_approval(base, base_in)

        deadline = int(time.time()) + 120

        tx = self.router.functions.swapExactTokensForTokens(
            base_in,
            amount_out_min,
            [
                Web3.to_checksum_address(base),
                Web3.to_checksum_address(quote),
            ],
            self.wallet,
            deadline,
        ).build_transaction({"from": self.wallet})

        self._set_bounded_gas(tx, 250_000)
        tx_hash = self._send_transaction(tx)

        try:
            actual_base_sold = (base_before - self._balance_wei(base)) / (10**base_decimals)
            actual_quote = (self._balance_wei(quote) - quote_before) / (10**quote_decimals)
        except Exception as exc:
            raise BrokerOrderUncertainError(
                f"EVM sell {tx_hash} is confirmed but balance reconciliation failed",
                tx_hash,
            ) from exc
        if actual_base_sold <= 0:
            raise BrokerOrderUncertainError(
                f"EVM sell {tx_hash} is confirmed but no base-token balance decrease was observed",
                tx_hash,
            )

        actual_price = actual_quote / actual_base_sold

        return ExecutionResult(
            qty=actual_base_sold,
            price=actual_price,
            fee=0.0,
            order_id=tx_hash,
        )
