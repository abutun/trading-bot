from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from psycopg.conninfo import conninfo_to_dict


LIVE_TRADING_CONFIRMATION = "I_UNDERSTAND_LIVE_TRADING_RISK"
SECURE_POSTGRES_SSLMODES = {"verify-full"}
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}
MAX_META_KEY_LENGTH = 128
MAX_PAIR_ID_LENGTH = MAX_META_KEY_LENGTH - len("last_order_at:")

# Public templates use deliberately obvious values.  A production process must
# never accept one merely because it happens to meet a minimum length check.
PLACEHOLDER_MARKERS = (
    "replace",
    "changeme",
    "change-me",
    "example",
    "placeholder",
    "your-",
    "<",
    ">",
)


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
    normalized = raw.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ValueError(
        f"{name} must be one of true/false, yes/no, on/off, or 1/0"
    )


@dataclass(frozen=True)
class Pair:
    venue: str
    symbol: str

    @property
    def pair_id(self) -> str:
        return f"{self.venue}:{self.symbol}"


@dataclass
class Config:
    """Runtime configuration with intentionally fail-closed live-mode defaults."""

    runtime_role: str = "bot"
    deployment_env: str = "development"
    mode: str = "paper"
    live_trading_confirmation: str = ""
    trading_pairs: List[Pair] = field(default_factory=list)

    market_data_exchange: str = "binance"
    interval: str = "15m"
    loop_seconds: int = 60
    candle_limit: int = 200
    quote_currency: str = "USDT"
    use_closed_candles: bool = True
    market_data_max_age_seconds: int = 180
    max_consecutive_failures: int = 3
    order_cooldown_seconds: int = 30

    # Strategy
    ema_fast: int = 12
    ema_slow: int = 26
    rsi_period: int = 14
    atr_period: int = 14
    stop_loss_atr_mult: float = 2.0
    take_profit_atr_mult: float = 3.0

    # Risk
    max_position_pct: float = 10.0
    max_total_exposure_pct: float = 30.0
    max_order_notional: float = 100.0
    max_order_slippage_bps: int = 100
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
    # IOC limits provide an enforceable price ceiling/floor for live spot orders.
    binance_order_mode: str = "ioc_limit"

    # EVM / MetaMask wallet
    evm_private_key: str = ""
    evm_rpc_url: str = ""
    evm_rpc_timeout_seconds: int = 15
    evm_chain_id: int = 1
    evm_router_address: str = ""
    evm_gas_price_gwei: Optional[float] = None
    evm_max_gas_price_gwei: float = 100.0
    evm_max_gas_limit: int = 600_000
    evm_min_confirmations: int = 1
    evm_slippage_bps: int = 50
    evm_approve_max: bool = False
    evm_receipt_timeout_seconds: int = 180
    evm_symbols: Dict[str, Dict[str, str]] = field(default_factory=dict)

    # Polymarket CLOB V2. The legacy polymarket-client package is not accepted.
    polymarket_private_key: str = ""
    polymarket_clob_host: str = "https://clob.polymarket.com"
    polymarket_chain_id: int = 137
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    polymarket_signature_type: int = 0
    polymarket_funder_address: str = ""
    polymarket_derive_api_credentials: bool = False
    polymarket_markets: Dict[str, Dict[str, str]] = field(default_factory=dict)
    polymarket_history_interval: str = "1d"
    polymarket_fidelity_minutes: int = 15
    polymarket_slippage_bps: int = 100

    # Kept only to provide a clear error for old environment files.
    polymarket_relayer_api_key: str = ""
    polymarket_relayer_api_key_address: str = ""

    # PostgreSQL state database
    postgres_dsn: str = ""
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_user: str = ""
    postgres_password: str = ""
    postgres_database: str = "trading_bot"
    postgres_sslmode: str = "prefer"
    postgres_sslrootcert: str = ""
    postgres_connect_timeout_seconds: int = 10

    # Monitoring dashboard
    dashboard_username: str = "admin"
    dashboard_password: str = ""
    dashboard_secret_key: str = ""
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    dashboard_secure_cookies: bool = False
    dashboard_heartbeat_stale_seconds: int = 180
    dashboard_login_max_attempts: int = 5
    dashboard_login_window_seconds: int = 300
    dashboard_login_lockout_seconds: int = 900
    trusted_proxy_count: int = 0

    log_level: str = "INFO"
    log_file: str = ""
    json_logs: bool = True

    def resolve_polymarket_market(self, symbol: str) -> Dict[str, str]:
        """Return the configured CLOB outcome-token metadata for a local alias."""
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
    def from_env(
        cls,
        *,
        load_dotenv_file: bool = True,
        runtime_role: str | None = None,
    ) -> "Config":
        """Build config from the environment.

        ``load_dotenv_file=False`` makes tests and controlled container launches
        deterministic by preventing a local ``.env`` from filling missing values.
        """
        resolved_runtime_role = (runtime_role or _str("APP_ROLE", "bot")).lower()
        if resolved_runtime_role not in {"bot", "dashboard", "all"}:
            raise ValueError("APP_ROLE must be bot, dashboard, or all")
        secret_manager = _str("SECRET_MANAGER", "").lower()
        if resolved_runtime_role == "dashboard" and secret_manager:
            raise ValueError(
                "Dashboard must not load a global SECRET_MANAGER; inject only its "
                "isolated database/session secrets through the dashboard environment"
            )
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

        if load_dotenv_file:
            load_dotenv(override=False)

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
            runtime_role=resolved_runtime_role,
            deployment_env=_str("DEPLOYMENT_ENV", "development").lower(),
            mode=_str("BOT_MODE", "paper").lower(),
            live_trading_confirmation=_str("LIVE_TRADING_CONFIRMATION"),
            trading_pairs=pairs,
            market_data_exchange=_str("MARKET_DATA_EXCHANGE", "binance").lower(),
            interval=_str("INTERVAL", "15m"),
            loop_seconds=_int("LOOP_SECONDS", 60),
            candle_limit=_int("CANDLE_LIMIT", 200),
            quote_currency=_str("QUOTE_CURRENCY", "USDT"),
            use_closed_candles=_bool("USE_CLOSED_CANDLES", True),
            market_data_max_age_seconds=_int("MARKET_DATA_MAX_AGE_SECONDS", 180),
            max_consecutive_failures=_int("MAX_CONSECUTIVE_FAILURES", 3),
            order_cooldown_seconds=_int("ORDER_COOLDOWN_SECONDS", 30),
            ema_fast=_int("EMA_FAST", 12),
            ema_slow=_int("EMA_SLOW", 26),
            rsi_period=_int("RSI_PERIOD", 14),
            atr_period=_int("ATR_PERIOD", 14),
            stop_loss_atr_mult=_float("STOP_LOSS_ATR_MULT", 2.0),
            take_profit_atr_mult=_float("TAKE_PROFIT_ATR_MULT", 3.0),
            max_position_pct=_float("MAX_POSITION_PCT", 10.0),
            max_total_exposure_pct=_float("MAX_TOTAL_EXPOSURE_PCT", 30.0),
            max_order_notional=_float("MAX_ORDER_NOTIONAL_USD", 100.0),
            max_order_slippage_bps=_int("MAX_ORDER_SLIPPAGE_BPS", 100),
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
            binance_order_mode=_str("BINANCE_ORDER_MODE", "ioc_limit").lower(),
            evm_private_key=_str("EVM_PRIVATE_KEY"),
            evm_rpc_url=_str("EVM_RPC_URL"),
            evm_rpc_timeout_seconds=_int("EVM_RPC_TIMEOUT_SECONDS", 15),
            evm_chain_id=_int("EVM_CHAIN_ID", 1),
            evm_router_address=_str("EVM_ROUTER_ADDRESS"),
            evm_gas_price_gwei=float(gas_raw) if gas_raw else None,
            evm_max_gas_price_gwei=_float("EVM_MAX_GAS_PRICE_GWEI", 100.0),
            evm_max_gas_limit=_int("EVM_MAX_GAS_LIMIT", 600_000),
            evm_min_confirmations=_int("EVM_MIN_CONFIRMATIONS", 1),
            evm_slippage_bps=_int("EVM_SLIPPAGE_BPS", 50),
            evm_approve_max=_bool("EVM_APPROVE_MAX", False),
            evm_receipt_timeout_seconds=_int("EVM_RECEIPT_TIMEOUT_SECONDS", 180),
            evm_symbols=evm_symbols,
            polymarket_private_key=_str("POLYMARKET_PRIVATE_KEY"),
            polymarket_clob_host=_str("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com"),
            polymarket_chain_id=_int("POLYMARKET_CHAIN_ID", 137),
            polymarket_api_key=_str("POLYMARKET_API_KEY"),
            polymarket_api_secret=_str("POLYMARKET_API_SECRET"),
            polymarket_api_passphrase=_str("POLYMARKET_API_PASSPHRASE"),
            polymarket_signature_type=_int("POLYMARKET_SIGNATURE_TYPE", 0),
            polymarket_funder_address=(
                _str("POLYMARKET_FUNDER_ADDRESS") or _str("POLYMARKET_WALLET_ADDRESS")
            ),
            polymarket_derive_api_credentials=_bool(
                "POLYMARKET_DERIVE_API_CREDENTIALS", False
            ),
            polymarket_markets=polymarket_markets,
            polymarket_history_interval=history_interval,
            polymarket_fidelity_minutes=_int("POLYMARKET_FIDELITY_MINUTES", 15),
            polymarket_slippage_bps=_int("POLYMARKET_SLIPPAGE_BPS", 100),
            polymarket_relayer_api_key=_str("POLYMARKET_RELAYER_API_KEY"),
            polymarket_relayer_api_key_address=_str(
                "POLYMARKET_RELAYER_API_KEY_ADDRESS"
            ),
            postgres_dsn=_str("POSTGRES_DSN"),
            postgres_host=_str("POSTGRES_HOST", "127.0.0.1"),
            postgres_port=_int("POSTGRES_PORT", 5432),
            postgres_user=_str("POSTGRES_USER"),
            postgres_password=_str("POSTGRES_PASSWORD"),
            postgres_database=_str("POSTGRES_DATABASE", "trading_bot"),
            postgres_sslmode=_str("POSTGRES_SSLMODE", "prefer").lower(),
            postgres_sslrootcert=_str("POSTGRES_SSLROOTCERT"),
            postgres_connect_timeout_seconds=_int("POSTGRES_CONNECT_TIMEOUT_SECONDS", 10),
            dashboard_username=_str("DASHBOARD_USERNAME", "admin"),
            dashboard_password=_str("DASHBOARD_PASSWORD"),
            dashboard_secret_key=_str("DASHBOARD_SECRET_KEY"),
            dashboard_host=_str("DASHBOARD_HOST", "127.0.0.1"),
            dashboard_port=_int("DASHBOARD_PORT", 8080),
            dashboard_secure_cookies=_bool("DASHBOARD_SECURE_COOKIES", False),
            dashboard_heartbeat_stale_seconds=_int(
                "DASHBOARD_HEARTBEAT_STALE_SECONDS", 180
            ),
            dashboard_login_max_attempts=_int("DASHBOARD_LOGIN_MAX_ATTEMPTS", 5),
            dashboard_login_window_seconds=_int("DASHBOARD_LOGIN_WINDOW_SECONDS", 300),
            dashboard_login_lockout_seconds=_int("DASHBOARD_LOGIN_LOCKOUT_SECONDS", 900),
            trusted_proxy_count=_int("TRUSTED_PROXY_COUNT", 0),
            log_level=_str("LOG_LEVEL", "INFO").upper(),
            log_file=_str("LOG_FILE"),
            json_logs=_bool("JSON_LOGS", True),
        )
        config._validate()
        return config

    @property
    def database_dsn(self) -> str:
        if self.postgres_dsn:
            return self.postgres_dsn
        parts = [
            f"host={self.postgres_host}",
            f"port={self.postgres_port}",
            f"user={self.postgres_user}",
            f"password={self.postgres_password}",
            f"dbname={self.postgres_database}",
            f"sslmode={self.postgres_sslmode}",
            f"connect_timeout={self.postgres_connect_timeout_seconds}",
        ]
        if self.postgres_sslrootcert:
            parts.append(f"sslrootcert={self.postgres_sslrootcert}")
        return " ".join(parts)

    def _validate(self) -> None:
        if self.mode not in {"paper", "live"}:
            raise ValueError("BOT_MODE must be either 'paper' or 'live'")
        if self.deployment_env not in {"development", "staging", "production"}:
            raise ValueError("DEPLOYMENT_ENV must be development, staging, or production")
        if self.runtime_role not in {"bot", "dashboard", "all"}:
            raise ValueError("APP_ROLE must be bot, dashboard, or all")
        if self.ema_fast <= 0 or self.ema_slow <= self.ema_fast:
            raise ValueError("EMA_SLOW must be greater than EMA_FAST and both must be positive")
        if min(
            self.rsi_period,
            self.atr_period,
            self.candle_limit,
            self.loop_seconds,
            self.market_data_max_age_seconds,
            self.max_consecutive_failures,
            self.postgres_connect_timeout_seconds,
            self.dashboard_heartbeat_stale_seconds,
            self.dashboard_login_max_attempts,
            self.dashboard_login_window_seconds,
            self.dashboard_login_lockout_seconds,
            self.evm_rpc_timeout_seconds,
        ) <= 0:
            raise ValueError("Configured intervals, limits, and timeouts must be positive")
        if self.order_cooldown_seconds < 0 or self.trusted_proxy_count < 0:
            raise ValueError("ORDER_COOLDOWN_SECONDS and TRUSTED_PROXY_COUNT cannot be negative")
        _validate_finite_positive(
            "strategy and risk values",
            self.stop_loss_atr_mult,
            self.take_profit_atr_mult,
            self.max_position_pct,
            self.max_total_exposure_pct,
            self.max_order_notional,
            self.min_notional,
            self.paper_initial_capital,
            self.evm_max_gas_price_gwei,
        )
        if not 0 < self.max_position_pct <= self.max_total_exposure_pct <= 100:
            raise ValueError(
                "MAX_POSITION_PCT must be positive and no greater than MAX_TOTAL_EXPOSURE_PCT (max 100)"
            )
        if not 0 <= self.max_daily_loss_pct < 100 or not 0 <= self.max_total_drawdown_pct < 100:
            raise ValueError("Loss and drawdown limits must be at least 0 and below 100")
        if not 0 <= self.fee_rate < 1:
            raise ValueError("FEE_RATE must be at least 0 and below 1")
        if any(
            not 0 <= value < 10_000
            for value in (
                self.slippage_bps,
                self.max_order_slippage_bps,
                self.evm_slippage_bps,
                self.polymarket_slippage_bps,
            )
        ):
            raise ValueError("All slippage values must be at least 0 and below 10000 bps")
        if self.evm_slippage_bps > self.max_order_slippage_bps:
            raise ValueError(
                "EVM_SLIPPAGE_BPS cannot exceed MAX_ORDER_SLIPPAGE_BPS"
            )
        if self.polymarket_slippage_bps > self.max_order_slippage_bps:
            raise ValueError(
                "POLYMARKET_SLIPPAGE_BPS cannot exceed MAX_ORDER_SLIPPAGE_BPS"
            )
        if self.max_open_positions <= 0:
            raise ValueError("MAX_OPEN_POSITIONS must be positive")
        if self.binance_order_mode not in {"ioc_limit", "market"}:
            raise ValueError("BINANCE_ORDER_MODE must be ioc_limit or market")
        if self.evm_receipt_timeout_seconds <= 0 or self.evm_max_gas_limit <= 21_000:
            raise ValueError("EVM receipt timeout and maximum gas limit are invalid")
        if (
            self.evm_gas_price_gwei is not None
            and (
                not math.isfinite(self.evm_gas_price_gwei)
                or self.evm_gas_price_gwei <= 0
                or self.evm_gas_price_gwei > self.evm_max_gas_price_gwei
            )
        ):
            raise ValueError(
                "EVM_GAS_PRICE_GWEI must be positive and no greater than EVM_MAX_GAS_PRICE_GWEI"
            )
        if self.evm_min_confirmations <= 0 or self.evm_chain_id <= 0:
            raise ValueError("EVM chain ID and confirmation count must be positive")
        if self.polymarket_chain_id != 137:
            raise ValueError("POLYMARKET_CHAIN_ID must be 137 for the production CLOB")
        if self.polymarket_signature_type not in {0, 1, 2, 3}:
            raise ValueError("POLYMARKET_SIGNATURE_TYPE must be 0, 1, 2, or 3")
        if not self.polymarket_clob_host.startswith("https://"):
            raise ValueError("POLYMARKET_CLOB_HOST must use https://")
        if self.polymarket_fidelity_minutes <= 0:
            raise ValueError("POLYMARKET_FIDELITY_MINUTES must be positive")
        if self.postgres_port <= 0 or self.dashboard_port <= 0:
            raise ValueError("POSTGRES_PORT and DASHBOARD_PORT must be positive")
        for pair in self.trading_pairs:
            if (
                len(pair.venue) > 32
                or len(pair.symbol) > 255
                or len(pair.pair_id) > MAX_PAIR_ID_LENGTH
            ):
                raise ValueError("TRADING_PAIRS venue/symbol values exceed database limits")
            if pair.venue == "evm" and pair.symbol not in self.evm_symbols:
                raise ValueError(f"EVM_SYMBOLS is missing a mapping for {pair.symbol!r}")
            if pair.venue == "polymarket":
                self.resolve_polymarket_market(pair.symbol)

        if self.runtime_role == "dashboard":
            self._validate_dashboard_credential_isolation()

        if self.mode == "live" and self.runtime_role != "dashboard":
            if self.deployment_env != "production":
                raise ValueError(
                    "BOT_MODE=live requires DEPLOYMENT_ENV=production so TLS and "
                    "production safety controls cannot be bypassed"
                )
            if self.live_trading_confirmation != LIVE_TRADING_CONFIRMATION:
                raise ValueError(
                    "BOT_MODE=live requires LIVE_TRADING_CONFIRMATION="
                    f"{LIVE_TRADING_CONFIRMATION}"
                )
            if self.binance_order_mode != "ioc_limit" and any(
                pair.venue == "binance" for pair in self.trading_pairs
            ):
                raise ValueError("Live Binance trading requires BINANCE_ORDER_MODE=ioc_limit")
            if any(pair.venue == "binance" for pair in self.trading_pairs):
                _require_non_placeholder("BINANCE_API_KEY", self.binance_api_key)
                _require_non_placeholder("BINANCE_API_SECRET", self.binance_api_secret)
            if any(pair.venue == "evm" for pair in self.trading_pairs):
                _require_non_placeholder("EVM_PRIVATE_KEY", self.evm_private_key)
                _require_non_placeholder("EVM_RPC_URL", self.evm_rpc_url)
                _require_non_placeholder("EVM_ROUTER_ADDRESS", self.evm_router_address)
            if any(pair.venue == "polymarket" for pair in self.trading_pairs):
                self._validate_polymarket_live()
            if self.polymarket_relayer_api_key or self.polymarket_relayer_api_key_address:
                raise ValueError(
                    "POLYMARKET_RELAYER_* belongs to the retired V1 SDK. Use the V2 "
                    "POLYMARKET_API_KEY, POLYMARKET_API_SECRET, and "
                    "POLYMARKET_API_PASSPHRASE settings instead."
                )

        if self.deployment_env == "production":
            # The bot and dashboard deliberately load different environment
            # files. Only the dashboard process needs its login/session values.
            if self.runtime_role != "bot":
                if not self.dashboard_secure_cookies:
                    raise ValueError("Production requires DASHBOARD_SECURE_COOKIES=true")
                if self.dashboard_username.lower() == "admin":
                    raise ValueError("Production requires a non-default DASHBOARD_USERNAME")
                _require_non_placeholder("DASHBOARD_USERNAME", self.dashboard_username)
                if len(self.dashboard_password) < 16 or len(self.dashboard_secret_key) < 32:
                    raise ValueError(
                        "Production requires a 16+ character dashboard password and 32+ character signing secret"
                    )
                _require_non_placeholder("DASHBOARD_PASSWORD", self.dashboard_password)
                _require_non_placeholder("DASHBOARD_SECRET_KEY", self.dashboard_secret_key)
            if self.postgres_dsn:
                _require_non_placeholder("POSTGRES_DSN", self.postgres_dsn)
            else:
                _require_non_placeholder("POSTGRES_USER", self.postgres_user)
                _require_non_placeholder("POSTGRES_PASSWORD", self.postgres_password)
                _require_non_placeholder("POSTGRES_DATABASE", self.postgres_database)
            # psycopg receives POSTGRES_DSN verbatim. A secure individual
            # POSTGRES_SSLMODE must never be allowed to mask a DSN that omits
            # it (psycopg/libpq would then fall back to its own default).
            sslmode = (
                _dsn_sslmode(self.postgres_dsn)
                if self.postgres_dsn
                else self.postgres_sslmode
            )
            if sslmode not in SECURE_POSTGRES_SSLMODES:
                raise ValueError(
                    "Production PostgreSQL requires sslmode=verify-full in "
                    "POSTGRES_DSN or POSTGRES_SSLMODE"
                )
            if not (
                self.postgres_sslrootcert
                or _dsn_param(self.postgres_dsn, "sslrootcert")
            ):
                raise ValueError(
                    "POSTGRES_SSLROOTCERT (or sslrootcert in POSTGRES_DSN) is required with verify-full"
                )
            if self.postgres_sslrootcert:
                _require_non_placeholder(
                    "POSTGRES_SSLROOTCERT", self.postgres_sslrootcert
                )

    def _validate_polymarket_live(self) -> None:
        _require_non_placeholder("POLYMARKET_PRIVATE_KEY", self.polymarket_private_key)
        credential_values = (
            self.polymarket_api_key,
            self.polymarket_api_secret,
            self.polymarket_api_passphrase,
        )
        supplied = sum(bool(value) for value in credential_values)
        if supplied not in {0, 3}:
            raise ValueError(
                "Set all of POLYMARKET_API_KEY, POLYMARKET_API_SECRET, and "
                "POLYMARKET_API_PASSPHRASE together"
            )
        if supplied == 0 and not self.polymarket_derive_api_credentials:
            raise ValueError(
                "Set Polymarket V2 API credentials or explicitly set "
                "POLYMARKET_DERIVE_API_CREDENTIALS=true"
            )
        if supplied:
            _require_non_placeholder("POLYMARKET_API_KEY", self.polymarket_api_key)
            _require_non_placeholder("POLYMARKET_API_SECRET", self.polymarket_api_secret)
            _require_non_placeholder(
                "POLYMARKET_API_PASSPHRASE", self.polymarket_api_passphrase
            )
        if self.polymarket_signature_type in {1, 2, 3} and not self.polymarket_funder_address:
            raise ValueError(
                "POLYMARKET_FUNDER_ADDRESS is required for non-EOA Polymarket signatures"
            )

    def _validate_dashboard_credential_isolation(self) -> None:
        """Refuse a dashboard process configured with transaction credentials."""
        leaked = [
            name
            for name, value in (
                ("BINANCE_API_KEY", self.binance_api_key),
                ("BINANCE_API_SECRET", self.binance_api_secret),
                ("EVM_PRIVATE_KEY", self.evm_private_key),
                ("POLYMARKET_PRIVATE_KEY", self.polymarket_private_key),
                ("POLYMARKET_API_KEY", self.polymarket_api_key),
                ("POLYMARKET_API_SECRET", self.polymarket_api_secret),
                ("POLYMARKET_API_PASSPHRASE", self.polymarket_api_passphrase),
            )
            if value
        ]
        if leaked:
            raise ValueError(
                "Dashboard environment must not contain trading credentials: "
                + ", ".join(leaked)
            )


def _require_non_placeholder(name: str, value: str) -> None:
    normalized = str(value or "").strip()
    lowered = normalized.lower()
    if not normalized or any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        raise ValueError(f"{name} must be set to a non-placeholder production value")


def _validate_finite_positive(label: str, *values: float) -> None:
    if any(not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError(f"{label} must be finite and positive")


def _dsn_sslmode(dsn: str) -> str:
    return _dsn_param(dsn, "sslmode")


def _dsn_param(dsn: str, name: str) -> str:
    if not dsn:
        return ""
    try:
        # Let psycopg/libpq parse the exact same DSN syntax that StateStore
        # will connect with. In particular, duplicate URI options resolve to
        # their effective final value; hand-parsing the first occurrence would
        # make production TLS validation bypassable.
        value = conninfo_to_dict(dsn).get(name)
    except Exception as exc:
        raise ValueError("POSTGRES_DSN is not a valid psycopg/libpq connection string") from exc
    return str(value or "").strip().lower()


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
