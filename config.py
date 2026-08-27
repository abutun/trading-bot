from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

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
    use_closed_candles: bool = True

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
    evm_approve_max: bool = False
    evm_receipt_timeout_seconds: int = 180
    evm_symbols: Dict[str, Dict[str, str]] = field(default_factory=dict)

    # Polymarket CLOB / account wallet. A MetaMask private key may be used here,
    # but it should be a dedicated trading signer, never a primary wallet.
    polymarket_private_key: str = ""
    polymarket_wallet_address: str = ""
    polymarket_relayer_api_key: str = ""
    polymarket_relayer_api_key_address: str = ""
    polymarket_markets: Dict[str, Dict[str, str]] = field(default_factory=dict)
    polymarket_history_interval: str = "1d"
    polymarket_fidelity_minutes: int = 15
    polymarket_slippage_bps: int = 100

    # PostgreSQL state database
    postgres_dsn: str = ""
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_user: str = ""
    postgres_password: str = ""
    postgres_database: str = "trading_bot"
    postgres_sslmode: str = "prefer"

    # Monitoring dashboard
    dashboard_username: str = "admin"
    dashboard_password: str = ""
    dashboard_secret_key: str = ""
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    dashboard_secure_cookies: bool = False

    log_level: str = "INFO"
    log_file: str = ""

    def resolve_polymarket_market(self, symbol: str) -> Dict[str, str]:
        """Return the configured CLOB token metadata for a strategy symbol."""
        try:
            market = self.polymarket_markets[symbol]
        except KeyError as exc:
            raise ValueError(
                f"POLYMARKET_MARKETS is missing a mapping for {symbol!r}"
            ) from exc

        token_id = market.get("token_id")
        if not token_id:
            raise ValueError(
                f"POLYMARKET_MARKETS[{symbol!r}] must include a non-empty token_id"
            )

        return market

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
        allowed_venues = {"binance", "evm", "polymarket"}

        for item in pairs_raw.split(","):
            item = item.strip()
            if not item:
                continue

            if ":" not in item:
                raise ValueError(
                    f"Invalid TRADING_PAIRS item {item!r}; expected venue:symbol"
                )

            venue, symbol = [x.strip() for x in item.split(":", 1)]
            venue = venue.lower()

            if venue not in allowed_venues:
                raise ValueError(
                    f"Unsupported venue {venue!r}; choose binance, evm, or polymarket"
                )
            if not symbol:
                raise ValueError(f"TRADING_PAIRS item {item!r} has an empty symbol")

            pairs.append(Pair(venue=venue, symbol=symbol))

        if not pairs:
            raise ValueError("TRADING_PAIRS must contain at least one venue:symbol pair")

        if len({pair.pair_id for pair in pairs}) != len(pairs):
            raise ValueError("TRADING_PAIRS contains a duplicate venue:symbol pair")

        evm_symbols = _json_object("EVM_SYMBOLS")
        polymarket_markets = _json_object("POLYMARKET_MARKETS")

        _validate_symbol_mapping("EVM_SYMBOLS", evm_symbols, {"base", "quote"})
        _validate_symbol_mapping("POLYMARKET_MARKETS", polymarket_markets, {"token_id"})

        gas_raw = _str("EVM_GAS_PRICE_GWEI", "")

        history_interval = _str("POLYMARKET_HISTORY_INTERVAL", "1d").lower()
        if history_interval not in {"1h", "6h", "1d", "1w", "max"}:
            raise ValueError(
                "POLYMARKET_HISTORY_INTERVAL must be one of 1h, 6h, 1d, 1w, max"
            )

        config = cls(
            mode=_str("BOT_MODE", "paper").lower(),
            trading_pairs=pairs,

            market_data_exchange=_str("MARKET_DATA_EXCHANGE", "binance").lower(),
            interval=_str("INTERVAL", "15m"),
            loop_seconds=_int("LOOP_SECONDS", 60),
            candle_limit=_int("CANDLE_LIMIT", 200),
            quote_currency=_str("QUOTE_CURRENCY", "USDT"),
            use_closed_candles=_bool("USE_CLOSED_CANDLES", True),

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
            evm_approve_max=_bool("EVM_APPROVE_MAX", False),
            evm_receipt_timeout_seconds=_int("EVM_RECEIPT_TIMEOUT_SECONDS", 180),
            evm_symbols=evm_symbols,

            polymarket_private_key=_str("POLYMARKET_PRIVATE_KEY"),
            polymarket_wallet_address=_str("POLYMARKET_WALLET_ADDRESS"),
            polymarket_relayer_api_key=_str("POLYMARKET_RELAYER_API_KEY"),
            polymarket_relayer_api_key_address=_str(
                "POLYMARKET_RELAYER_API_KEY_ADDRESS"
            ),
            polymarket_markets=polymarket_markets,
            polymarket_history_interval=history_interval,
            polymarket_fidelity_minutes=_int("POLYMARKET_FIDELITY_MINUTES", 15),
            polymarket_slippage_bps=_int("POLYMARKET_SLIPPAGE_BPS", 100),

            postgres_dsn=_str("POSTGRES_DSN"),
            postgres_host=_str("POSTGRES_HOST", "127.0.0.1"),
            postgres_port=_int("POSTGRES_PORT", 5432),
            postgres_user=_str("POSTGRES_USER"),
            postgres_password=_str("POSTGRES_PASSWORD"),
            postgres_database=_str("POSTGRES_DATABASE", "trading_bot"),
            postgres_sslmode=_str("POSTGRES_SSLMODE", "prefer"),

            dashboard_username=_str("DASHBOARD_USERNAME", "admin"),
            dashboard_password=_str("DASHBOARD_PASSWORD"),
            dashboard_secret_key=_str("DASHBOARD_SECRET_KEY"),
            dashboard_host=_str("DASHBOARD_HOST", "127.0.0.1"),
            dashboard_port=_int("DASHBOARD_PORT", 8080),
            dashboard_secure_cookies=_bool("DASHBOARD_SECURE_COOKIES", False),

            log_level=_str("LOG_LEVEL", "INFO").upper(),
            log_file=_str("LOG_FILE"),
        )

        config._validate()
        return config

    @property
    def database_dsn(self) -> str:
        if self.postgres_dsn:
            return self.postgres_dsn

        return (
            f"host={self.postgres_host} port={self.postgres_port} "
            f"user={self.postgres_user} password={self.postgres_password} "
            f"dbname={self.postgres_database} sslmode={self.postgres_sslmode}"
        )

    def _validate(self) -> None:
        if self.mode not in {"paper", "live"}:
            raise ValueError("BOT_MODE must be either 'paper' or 'live'")

        if self.ema_fast <= 0 or self.ema_slow <= self.ema_fast:
            raise ValueError("EMA_SLOW must be greater than EMA_FAST and both must be positive")
        if min(self.rsi_period, self.atr_period, self.candle_limit, self.loop_seconds) <= 0:
            raise ValueError("Indicator periods, CANDLE_LIMIT, and LOOP_SECONDS must be positive")
        if self.max_position_pct <= 0 or self.max_position_pct > 100:
            raise ValueError("MAX_POSITION_PCT must be greater than 0 and no more than 100")
        if not 0 <= self.max_daily_loss_pct < 100 or not 0 <= self.max_total_drawdown_pct < 100:
            raise ValueError("Loss and drawdown limits must be at least 0 and below 100")
        if self.max_open_positions <= 0 or self.min_notional <= 0:
            raise ValueError("MAX_OPEN_POSITIONS and MIN_NOTIONAL_USD must be positive")
        if self.paper_initial_capital <= 0:
            raise ValueError("PAPER_INITIAL_CAPITAL must be positive")
        if not 0 <= self.fee_rate < 1 or not 0 <= self.slippage_bps < 10_000:
            raise ValueError("FEE_RATE must be below 1 and SLIPPAGE_BPS below 10000")
        if not 0 < self.polymarket_fidelity_minutes or not 0 <= self.polymarket_slippage_bps < 10_000:
            raise ValueError("Polymarket fidelity must be positive and slippage below 10000")
        if not 0 <= self.evm_slippage_bps < 10_000 or self.evm_receipt_timeout_seconds <= 0:
            raise ValueError("EVM slippage must be below 10000 and receipt timeout positive")
        for pair in self.trading_pairs:
            if pair.venue == "evm" and pair.symbol not in self.evm_symbols:
                raise ValueError(f"EVM_SYMBOLS is missing a mapping for {pair.symbol!r}")
            if pair.venue == "polymarket":
                self.resolve_polymarket_market(pair.symbol)


def _json_object(name: str) -> Dict[str, Dict[str, str]]:
    raw = _str(name, "{}")
    try:
        value: Any = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON") from exc

    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")

    normalized: Dict[str, Dict[str, str]] = {}
    for symbol, mapping in value.items():
        if isinstance(mapping, str):
            normalized[str(symbol)] = {"token_id": mapping}
        elif isinstance(mapping, dict):
            normalized[str(symbol)] = {
                str(key): str(item) for key, item in mapping.items() if item is not None
            }
        else:
            raise ValueError(f"{name}[{symbol!r}] must be an object")

    return normalized


def _validate_symbol_mapping(
    name: str, mapping: Dict[str, Dict[str, str]], required_keys: set[str]
) -> None:
    for symbol, item in mapping.items():
        missing = required_keys - item.keys()
        if missing or any(not item[key].strip() for key in required_keys - missing):
            expected = ", ".join(sorted(required_keys))
            raise ValueError(f"{name}[{symbol!r}] must include non-empty {expected}")
