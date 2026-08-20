from __future__ import annotations

import logging
import time

from eth_account import Account
from web3 import Web3

from models import ExecutionResult

from .base import Broker


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

        self.w3 = Web3(Web3.HTTPProvider(config.evm_rpc_url))

        if not self.w3.is_connected():
            raise ConnectionError("Cannot connect to EVM RPC")

        self.account = Account.from_key(config.evm_private_key)
        self.wallet = self.account.address

        self.router = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.evm_router_address),
            abi=ROUTER_ABI,
        )

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
            return Web3.to_wei(self.config.evm_gas_price_gwei, "gwei")

        return int(self.w3.eth.gas_price)

    def _send_transaction(self, tx: dict) -> str:
        tx["from"] = self.wallet
        tx["nonce"] = self._nonce
        self._nonce += 1

        if "chainId" not in tx:
            tx["chainId"] = self.config.evm_chain_id

        if "gasPrice" not in tx:
            tx["gasPrice"] = self._gas_price_wei()

        signed = self.account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")

        tx_hash = self.w3.eth.send_raw_transaction(raw)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)

        if receipt.status != 1:
            raise Exception(f"Transaction failed: {tx_hash.hex()}")

        return tx_hash.hex()

    def _ensure_approval(self, token_address: str, amount_wei: int) -> None:
        if amount_wei <= 0:
            return

        spender = Web3.to_checksum_address(self.config.evm_router_address)
        allowance = self._allowance_wei(token_address, spender)

        if allowance >= amount_wei:
            return

        tx = self._token(token_address).functions.approve(
            spender, 2**256 - 1
        ).build_transaction({"from": self.wallet})

        tx["gas"] = int(tx.get("gas", 60_000) * 1.3)
        self._send_transaction(tx)

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
            price = prices.get(pos.symbol, pos.entry_price)
            equity += pos.qty * price

        return equity

    def buy(self, symbol: str, qty: float, price_hint: float) -> ExecutionResult:
        mapping = self._mapping(symbol)
        base = mapping["base"]
        quote = mapping["quote"]

        base_decimals = self._decimals(base)
        quote_decimals = self._decimals(quote)

        if qty <= 0 or price_hint <= 0:
            return ExecutionResult(qty=0, price=price_hint)

        slippage = self.config.evm_slippage_bps / 10_000

        quote_in = int(qty * price_hint * (10**quote_decimals))
        if quote_in <= 0:
            return ExecutionResult(qty=0, price=price_hint)

        if self._balance_wei(quote) < quote_in:
            raise ValueError("Insufficient quote token balance")

        expected_out = self._get_amount_out(quote_in, quote, base)
        if expected_out <= 0:
            raise ValueError("No liquidity for buy")

        amount_out_min = int(expected_out * (1 - slippage))

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

        tx["gas"] = int(tx.get("gas", 250_000) * 1.3)
        tx_hash = self._send_transaction(tx)

        actual_qty = expected_out / (10**base_decimals)
        quote_spent = quote_in / (10**quote_decimals)

        actual_price = quote_spent / actual_qty if actual_qty > 0 else price_hint
        fee = max(0.0, abs(qty * price_hint - actual_qty * actual_price))

        return ExecutionResult(
            qty=actual_qty,
            price=actual_price,
            fee=fee,
            order_id=tx_hash,
        )

    def sell(self, symbol: str, qty: float, price_hint: float) -> ExecutionResult:
        mapping = self._mapping(symbol)
        base = mapping["base"]
        quote = mapping["quote"]

        base_decimals = self._decimals(base)
        quote_decimals = self._decimals(quote)

        if qty <= 0:
            return ExecutionResult(qty=0, price=price_hint)

        base_in = int(qty * (10**base_decimals))
        if self._balance_wei(base) < base_in:
            raise ValueError("Insufficient base token balance")

        expected_out = self._get_amount_out(base_in, base, quote)
        if expected_out <= 0:
            raise ValueError("No liquidity for sell")

        slippage = self.config.evm_slippage_bps / 10_000
        amount_out_min = int(expected_out * (1 - slippage))

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

        tx["gas"] = int(tx.get("gas", 250_000) * 1.3)
        tx_hash = self._send_transaction(tx)

        actual_quote = expected_out / (10**quote_decimals)
        actual_price = actual_quote / qty if qty > 0 else price_hint
        fee = max(0.0, abs(qty * price_hint - actual_quote))

        return ExecutionResult(
            qty=qty,
            price=actual_price,
            fee=fee,
            order_id=tx_hash,
        )
