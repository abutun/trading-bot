# Trading Bot

Python trading bot with one strategy engine and three execution venues:

- **Binance Spot** through ccxt
- **EVM / MetaMask wallet** through web3.py and a configured DEX router
- **Polymarket CLOB** through the official `polymarket-client` Python SDK

The bot persists state in **PostgreSQL**. It supports paper trading, historical
backtests, a login-protected monitoring dashboard, ATR-based stops, a daily-loss
limit, and a total-drawdown kill switch.

> This software sends financial orders. It is not investment advice and it
> cannot guarantee a profit. Start in paper mode and use dedicated, low-balance
> trading wallets and least-privilege API keys.

## Important safety behaviour

- Every external order is first written to PostgreSQL as a durable **order
  intent**. A timeout or crash leaves it unresolved; the bot blocks only that
  pair rather than risking a duplicate order on restart.
- A zero-fill or partial sell never deletes the tracked position. A partial fill
  reduces the stored quantity; a failed fill keeps it unchanged.
- Signals use completed candles by default (`USE_CLOSED_CANDLES=true`). The
  live price still drives stop-loss/take-profit checks.
- EVM allowance defaults to the exact swap amount. Set `EVM_APPROVE_MAX=true`
  only if you consciously accept an unlimited ERC-20 allowance.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

cp .env.example .env
# Set POSTGRES_*, DASHBOARD_*, and the venue settings you will use.

python main.py              # paper mode by default
python dashboard.py         # in another terminal
```

Create a PostgreSQL database and restricted application user first:

```sql
CREATE USER tradingbot WITH PASSWORD 'use-a-long-random-password';
CREATE DATABASE trading_bot OWNER tradingbot;
```

For a local containerized PostgreSQL instance instead:

```bash
docker compose up -d postgres
```

The state schema is created automatically on the bot or dashboard’s first
connection. The reviewed schema is also available in
[`migrations/001_initial.sql`](migrations/001_initial.sql).

## Configuration

Copy `.env.example`; do not commit a filled `.env` file. `POSTGRES_DSN` takes
precedence over the individual `POSTGRES_*` variables.

| Group | Key settings |
|---|---|
| Core | `BOT_MODE`, `TRADING_PAIRS`, `INTERVAL`, `LOOP_SECONDS`, `USE_CLOSED_CANDLES` |
| Strategy | `EMA_FAST`, `EMA_SLOW`, `RSI_PERIOD`, `ATR_PERIOD`, ATR multipliers |
| Risk | `MAX_POSITION_PCT`, `MAX_DAILY_LOSS_PCT`, `MAX_TOTAL_DRAWDOWN_PCT`, `MIN_NOTIONAL_USD` |
| PostgreSQL | `POSTGRES_DSN` or `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DATABASE`, `POSTGRES_SSLMODE` |
| Dashboard | `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD`, `DASHBOARD_SECRET_KEY`, `DASHBOARD_SECURE_COOKIES` |

`TRADING_PAIRS` takes comma-separated `venue:symbol` values. Valid venues are
`binance`, `evm`, and `polymarket`. Pair IDs are venue-scoped, so the same
symbol can safely be used on multiple venues.

## Binance

```dotenv
BOT_MODE=live
TRADING_PAIRS=binance:BTC/USDT
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
BINANCE_TESTNET=true
```

Use testnet before mainnet. Create a Spot-only key, disable withdrawals, and
use IP allowlisting where Binance supports it.

## EVM / MetaMask

The EVM broker swaps ERC-20 tokens through a Uniswap V2-compatible router. Use
wrapped native tokens (for example WETH) rather than native ETH.

```dotenv
BOT_MODE=live
TRADING_PAIRS=evm:ETH/USDT
EVM_PRIVATE_KEY=0x...
EVM_RPC_URL=https://your-rpc.example
EVM_CHAIN_ID=1
EVM_ROUTER_ADDRESS=0x...
EVM_SYMBOLS={"ETH/USDT":{"base":"0xWETH","quote":"0xUSDC"}}
EVM_APPROVE_MAX=false
```

The configured private key is a MetaMask-compatible key. Use a dedicated hot
wallet that contains only the swap capital and native gas token required for
trading.

## Polymarket

Polymarket pairs trade a single **outcome token** (YES or NO), rather than a
crypto ticker. The public price-history API provides sampled prices; the bot
converts those to synthetic OHLC bars for the existing EMA/RSI/ATR strategy.
Synthetic bars have no real volume, so validate this strategy separately in
paper mode before going live.

```dotenv
BOT_MODE=paper
TRADING_PAIRS=polymarket:example-yes
POLYMARKET_MARKETS={"example-yes":{"token_id":"<YES_OUTCOME_TOKEN_ID>"}}
POLYMARKET_HISTORY_INTERVAL=1d
POLYMARKET_FIDELITY_MINUTES=15
```

Live trading needs a dedicated Polygon/MetaMask signer. `POLYMARKET_WALLET_ADDRESS`
is optional for a direct EOA, and required when the account wallet differs from
the signer. A Relayer key is optional but recommended for supported gasless
Deposit Wallet operations:

```dotenv
BOT_MODE=live
TRADING_PAIRS=polymarket:example-yes
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_WALLET_ADDRESS=0x...
POLYMARKET_RELAYER_API_KEY=...
POLYMARKET_RELAYER_API_KEY_ADDRESS=0x...
POLYMARKET_SLIPPAGE_BPS=100
POLYMARKET_MARKETS={"example-yes":{"token_id":"<YES_OUTCOME_TOKEN_ID>"}}
```

Buys and sells use fill-or-kill market orders: a requested order either fills
fully or is rejected, preventing the strategy from silently carrying a partial
outcome position. Before trading, fund the account with pUSD and complete the
Polymarket trading-approval setup. See the official [Python SDK guide](https://docs.polymarket.com/getting-started/python), [wallet/authentication guide](https://docs.polymarket.com/trading/wallets-auth), and [order guide](https://docs.polymarket.com/trading/place-orders).

## MySQL to PostgreSQL migration

1. Stop both the bot and dashboard to freeze writes.
2. Back up the MySQL source database.
3. Configure the target `POSTGRES_*` variables alongside the legacy source
   `MYSQL_*` variables in a local migration-only env file.
4. Install the migration dependency and run the script:

```bash
pip install -r requirements-migration.txt
python scripts/migrate_mysql_to_postgres.py --env-file .env.migration
```

The script refuses a non-empty PostgreSQL `positions` table, copies positions,
trades, metadata, and equity history in one transaction, and preserves the old
trade/equity IDs. It does not delete or alter the MySQL source.

After checking PostgreSQL row counts and dashboard values, remove the `MYSQL_*`
secrets and use the normal `.env` with only PostgreSQL settings.

## Running and monitoring

```bash
BOT_MODE=paper python main.py
python main.py --backtest
python dashboard.py
```

The dashboard binds to `127.0.0.1:8080` by default. It requires both
`DASHBOARD_PASSWORD` and a random `DASHBOARD_SECRET_KEY` (for example
`openssl rand -hex 32`). If TLS terminates upstream, keep
`DASHBOARD_SECURE_COOKIES=false` only for local HTTP development; set it to
`true` whenever users access the dashboard over HTTPS, including via a TLS
reverse proxy.

The dashboard lists unresolved orders prominently. Reconcile those manually in
the venue before any database change: inspect the venue order/transaction,
correct the position and trade history if necessary, then mark the corresponding
`orders` row with its verified terminal outcome. This conservative stop is
intentional—blind retries are unsafe for live trading.

## Tests

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
```
