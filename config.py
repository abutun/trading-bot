from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from dotenv import load_dotenv


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Pair:
    venue: str
    symbol: str

    @property
    def pair_id(self) -> str:
        return f"{self.venue}:{self.symbol}"


@dataclass
class Config:
    mode: str = "paper"
    trading_pairs: List[Pair] = field(default_factory=list)

    market_data_exchange: str = "binance"
    interval: str = "15m"
    loop_seconds: int = 60
    candle_limit: int = 200
    quote_currency: str = "USDT"

    # Strategy
    ema_fast: int = 12
    ema_slow: int = 26
    rsi_period: int = 14
    atr_period: int = 14
    stop_loss_atr_mult: float = 2.0
    take_profit_atr_mult: float = 3.0

    # Risk
    max_position_pct: float = 10.0
    max_daily_loss_pct: float = 2.0
    max_total_drawdown_pct: float = 10.0
    min_notional: float = 10.0
    max_open_positions: int = 3

    # Costs / paper trading
    fee_rate: float = 0.001
    slippage_bps: int = 5
    paper_initial_capital: float = 1000.0

    # Binance
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = True

    # EVM / MetaMask wallet
    evm_private_key: str = ""
    evm_rpc_url: str = ""
    evm_chain_id: int = 1
    evm_router_address: str = ""
    evm_gas_price_gwei: Optional[float] = None
    evm_slippage_bps: int = 50
    evm_symbols: Dict[str, Dict[str, str]] = field(default_factory=dict)

    log_level: str = "INFO"
    log_file: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        # Optional: load secrets from AWS Secrets Manager first.
        secret_manager = _str("SECRET_MANAGER", "").lower()
        if secret_manager == "aws":
            try:
                import boto3

                client = boto3.client("secretsmanager")
                secret_id = _str("SECRET_ID", "trading-bot/secrets")
                payload = client.get_secret_value(SecretId=secret_id)["SecretString"]

                for key, value in json.loads(payload).items():
                    os.environ.setdefault(key, str(value))

            except Exception as exc:
                raise RuntimeError(f"Failed to load AWS secret {secret_id}: {exc}") from exc

        # Local .env should not override real environment variables.
        load_dotenv()

        pairs_raw = _str("TRADING_PAIRS", "binance:BTC/USDT")
        pairs: List[Pair] = []

        for item in pairs_raw.split(","):
            item = item.strip()
            if not item:
                continue

            venue, symbol = [x.strip() for x in item.split(":", 1)]
            pairs.append(Pair(venue=venue, symbol=symbol))

        evm_symbols_raw = _str("EVM_SYMBOLS", "{}")
        try:
            evm_symbols = json.loads(evm_symbols_raw) if evm_symbols_raw else {}
        except json.JSONDecodeError:
            evm_symbols = {}

        gas_raw = _str("EVM_GAS_PRICE_GWEI", "")

        return cls(
            mode=_str("BOT_MODE", "paper").lower(),
            trading_pairs=pairs,

            market_data_exchange=_str("MARKET_DATA_EXCHANGE", "binance").lower(),
            interval=_str("INTERVAL", "15m"),
            loop_seconds=_int("LOOP_SECONDS", 60),
            candle_limit=_int("CANDLE_LIMIT", 200),
            quote_currency=_str("QUOTE_CURRENCY", "USDT"),

            ema_fast=_int("EMA_FAST", 12),
            ema_slow=_int("EMA_SLOW", 26),
            rsi_period=_int("RSI_PERIOD", 14),
            atr_period=_int("ATR_PERIOD", 14),
            stop_loss_atr_mult=_float("STOP_LOSS_ATR_MULT", 2.0),
            take_profit_atr_mult=_float("TAKE_PROFIT_ATR_MULT", 3.0),

            max_position_pct=_float("MAX_POSITION_PCT", 10.0),
            max_daily_loss_pct=_float("MAX_DAILY_LOSS_PCT", 2.0),
            max_total_drawdown_pct=_float("MAX_TOTAL_DRAWDOWN_PCT", 10.0),
            min_notional=_float("MIN_NOTIONAL_USD", 10.0),
            max_open_positions=_int("MAX_OPEN_POSITIONS", 3),

            fee_rate=_float("FEE_RATE", 0.001),
            slippage_bps=_int("SLIPPAGE_BPS", 5),
            paper_initial_capital=_float("PAPER_INITIAL_CAPITAL", 1000.0),

            binance_api_key=_str("BINANCE_API_KEY"),
            binance_api_secret=_str("BINANCE_API_SECRET"),
            binance_testnet=_bool("BINANCE_TESTNET", True),

            evm_private_key=_str("EVM_PRIVATE_KEY"),
            evm_rpc_url=_str("EVM_RPC_URL"),
            evm_chain_id=_int("EVM_CHAIN_ID", 1),
            evm_router_address=_str("EVM_ROUTER_ADDRESS"),
            evm_gas_price_gwei=float(gas_raw) if gas_raw else None,
            evm_slippage_bps=_int("EVM_SLIPPAGE_BPS", 50),
            evm_symbols=evm_symbols,

            log_level=_str("LOG_LEVEL", "INFO").upper(),
            log_file=_str("LOG_FILE"),
        )
