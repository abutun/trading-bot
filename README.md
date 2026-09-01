# Trading Bot

Production-oriented Python trading bot for three execution venues:

- Binance Spot through CCXT
- an EVM DEX router using a dedicated MetaMask-compatible signing key
- Polymarket CLOB V2 on Polygon

The bot keeps positions, trades, equity, heartbeats, price snapshots, and
durable order intents in PostgreSQL. It is deliberately **fail-closed**: it
will stop new automation rather than guess after stale data, a database fault,
an unknown order outcome, or an unsafe fill.

> This software can send real financial orders. It is not investment advice or
> a guarantee of profit. Start with paper trading and dedicated low-balance
> accounts. Do not use a primary exchange account or primary wallet.

## What is protected

| Risk | Runtime behaviour |
|---|---|
| Duplicate order after timeout/crash | A PostgreSQL order intent is committed before submission. Any ambiguous outcome becomes `unknown` and blocks the entire bot until reconciled. |
| Stale/malformed prices | All OHLCV input is finite/consistent and must be fresh before a decision. No entry-price fallback is used for live risk decisions. |
| Price gap / slippage | Binance uses IOC limit orders; EVM and Polymarket enforce executable price bounds; every reported fill is checked again against `MAX_ORDER_SLIPPAGE_BPS`. |
| Two bot instances | A PostgreSQL advisory lock permits exactly one active trading loop for the same database. |
| EVM misconfiguration | RPC chain ID, router bytecode, gas price/limit caps, confirmation count, and pre-trade router quote are verified. |
| Dashboard secret leakage | Docker uses separate bot and dashboard environment files. The dashboard does not receive venue or wallet credentials. |

Bot-managed stop/take-profit rules are **not native exchange stop orders**.
The bot must remain healthy and receive fresh data to execute them. Use venue-
native protection separately if your strategy or venue supports it.

## Quick start: paper mode

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --require-hashes -r requirements-dev.lock

cp .env.example .env
# Replace POSTGRES_PASSWORD before use.
python main.py --backtest
```

For the supported container stack, keep configuration separated:

```bash
cp .env.example .env                 # Compose variables, including POSTGRES_PASSWORD
cp .env.bot.example .env.bot         # bot-only credentials and limits
cp .env.dashboard.example .env.dashboard
# Make POSTGRES_PASSWORD identical in all three local files.

docker compose up -d postgres
docker compose run --rm bot python main.py --preflight
docker compose up -d bot dashboard
```

Those commands intentionally start the local Compose PostgreSQL service. A
server deployment uses the external-TLS
[`docker-compose.production.yml`](docker-compose.production.yml) overlay and
does not start local PostgreSQL; follow the exact certificate and DSN setup in
[PRODUCTION.md](PRODUCTION.md).

The dashboard binds only to `127.0.0.1` by default. Use a TLS reverse proxy
for remote access; do not expose Gunicorn directly to the Internet.

## Live trading gate

Live mode cannot start accidentally. It requires all of the following:

```dotenv
BOT_MODE=live
LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_TRADING_RISK
```

Then run the non-ordering validation command first:

```bash
python main.py --preflight
```

`--preflight` verifies PostgreSQL, the single-instance lock, configured
positions, fresh market data, and account equity. It never creates or submits
an order. Use `--once` only after paper/testnet validation to execute one
fully guarded cycle.

Detailed deployment, recovery, database-role, and rollback instructions are
in [PRODUCTION.md](PRODUCTION.md). Local setup and migration instructions are
in [SETUP.md](SETUP.md).

## Venue configuration

### Binance Spot

```dotenv
TRADING_PAIRS=binance:BTC/USDT
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
BINANCE_TESTNET=true
BINANCE_ORDER_MODE=ioc_limit
```

Live Binance orders are IOC limit orders, never unbounded market orders. Use a
Spot-only key with withdrawals disabled and IP allowlisting wherever Binance
supports it. Testnet must pass preflight and a paper soak before switching
`BINANCE_TESTNET=false`.

### EVM / MetaMask-compatible signer

```dotenv
TRADING_PAIRS=evm:ETH/USDT
EVM_PRIVATE_KEY=0x...                 # exported dedicated hot-wallet key
EVM_RPC_URL=https://your-rpc.example
EVM_CHAIN_ID=1
EVM_ROUTER_ADDRESS=0x...
EVM_SYMBOLS={"ETH/USDT":{"base":"0xWETH","quote":"0xUSDC"}}
EVM_MAX_GAS_PRICE_GWEI=100
EVM_MAX_GAS_LIMIT=600000
EVM_MIN_CONFIRMATIONS=1
EVM_SLIPPAGE_BPS=50
```

This is server-side signing with a key exported from a dedicated MetaMask
account; it does not control the MetaMask browser extension. Use ERC-20 tokens
(for example WETH, not native ETH), verify every router/token contract for the
target chain, and keep only trading capital plus gas in that wallet. The bot
refuses a router quote outside `MAX_ORDER_SLIPPAGE_BPS` before broadcast.

### Polymarket CLOB V2

`POLYMARKET_MARKETS` maps an app-local alias to one conditional-token outcome
ID (YES or NO), not a market slug:

```dotenv
TRADING_PAIRS=polymarket:example-yes
POLYMARKET_MARKETS={"example-yes":{"token_id":"<outcome-token-id>"}}
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...
POLYMARKET_SIGNATURE_TYPE=0
POLYMARKET_SLIPPAGE_BPS=100
```

The integration uses `py-clob-client-v2`, the current V2 API. It creates
price-bounded FOK orders and accepts only a terminal exact fill; delayed,
partial, malformed, or transport-ambiguous responses halt automation for
manual reconciliation. Set all three API credentials together, or deliberately
enable `POLYMARKET_DERIVE_API_CREDENTIALS=true`. For proxy/safe signatures,
also set `POLYMARKET_FUNDER_ADDRESS`.

Read Polymarket’s official [V2 migration guide](https://docs.polymarket.com/v2-migration), [trading overview](https://docs.polymarket.com/trading/overview), and [order management guide](https://docs.polymarket.com/trading/manage-orders) before funding an account. Price history is sampled outcome-token data converted to synthetic OHLC bars; it has no real volume and must be validated in paper mode.

## PostgreSQL and MySQL migration

PostgreSQL is the only runtime database. The reviewed schema is
[`migrations/001_initial.sql`](migrations/001_initial.sql); the bot initializes
it, while the dashboard intentionally uses no DDL privileges.

For a one-time legacy MySQL copy:

```bash
cp .env.migration.example .env.migration
# Fill MYSQL_* source and POSTGRES_* target values.
python -m pip install --require-hashes -r requirements-migration.lock
python scripts/migrate_mysql_to_postgres.py --env-file .env.migration
```

Stop bot and dashboard first, back up MySQL, and use an empty PostgreSQL target.
The selected `--env-file` replaces the process environment rather than merging
with it, so an ambient shell cannot redirect the copy. The copy is transactional
for positions, trades, metadata, and equity history; it never alters the MySQL
source. It refuses legacy non-terminal/unknown orders and carried positions
without finite `0 < stop-loss < entry < take-profit` protection; reconcile
those at the venue before migrating.

## Tests and quality checks

```bash
python -m pytest
python -m ruff check .
python -m pip_audit -r requirements.lock
python -m compileall -q .
```

GitHub Actions runs the same Python 3.11 checks on pushes and pull requests.
`requirements.txt` and `requirements-dev.txt` are reviewed input manifests;
the exact, hash-verified locks are regenerated deliberately during dependency
updates.
