# Local setup and migration

This guide is for development, paper trading, and migration rehearsal. Use
[PRODUCTION.md](PRODUCTION.md) for a server that can send real orders.

## Requirements

- Python 3.11 (the CI/container runtime version)
- Docker Compose or PostgreSQL 16+
- No exchange, wallet, or Polymarket account is needed for paper mode

## Install

```bash
cd /Users/ahmet/Documents/Workspaces/Buhane/trading-bot
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --require-hashes -r requirements-dev.lock
```

## Local PostgreSQL and isolated environments

The Compose file intentionally does not share the bot’s venue credentials with
the dashboard:

```bash
cp .env.example .env
cp .env.bot.example .env.bot
cp .env.dashboard.example .env.dashboard
```

Set one long `POSTGRES_PASSWORD` in all three files. For local Compose,
`.env.dashboard` may use `POSTGRES_USER=tradingbot`; production should use the
read-only dashboard role from [PRODUCTION.md](PRODUCTION.md).

```bash
docker compose up -d postgres
docker compose run --rm bot python main.py --preflight
docker compose up -d bot dashboard
```

This is the local-development stack and deliberately starts its local
PostgreSQL service. Do not use it unchanged on a server: the external TLS
database overlay and CA-certificate mount are documented in
[PRODUCTION.md](PRODUCTION.md).

Open `http://127.0.0.1:8080` only from the local machine. The dashboard needs
its own non-empty `DASHBOARD_PASSWORD` and random `DASHBOARD_SECRET_KEY`:

```bash
openssl rand -hex 32
```

To run directly instead of Compose, copy `.env.example` to `.env`, create the
PostgreSQL role/database, and use `POSTGRES_HOST=127.0.0.1`.

## Safe commands

```bash
python main.py --backtest     # historical strategy sanity check
python main.py --preflight    # no order; DB/data/equity validation
python main.py --once         # one guarded paper/live cycle
python main.py                # persistent loop
python dashboard.py           # development dashboard only
```

The default `BOT_MODE=paper` is intentional. `BOT_MODE=live` also needs the
exact `LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_TRADING_RISK` value.

## Venue examples

### Binance

```dotenv
TRADING_PAIRS=binance:BTC/USDT
BINANCE_TESTNET=true
BINANCE_ORDER_MODE=ioc_limit
```

Add API credentials only for live/testnet venue testing. The broker uses IOC
limit orders and reports non-terminal results as unresolved, not as a retry.

### EVM

```dotenv
TRADING_PAIRS=evm:ETH/USDT
EVM_RPC_URL=https://your-rpc.example
EVM_CHAIN_ID=1
EVM_ROUTER_ADDRESS=<verified-uniswap-v2-compatible-router>
EVM_SYMBOLS={"ETH/USDT":{"base":"<WETH>","quote":"<USDC>"}}
```

Only supply a dedicated exported MetaMask-compatible private key when testing
live execution. The mapping symbol must be valid for the configured market-data
exchange and must represent the same assets as the token addresses.

### Polymarket V2

```dotenv
TRADING_PAIRS=polymarket:example-yes
POLYMARKET_MARKETS={"example-yes":{"token_id":"<outcome-token-id>"}}
POLYMARKET_HISTORY_INTERVAL=1d
POLYMARKET_FIDELITY_MINUTES=15
```

The client is `py-clob-client-v2`; do not use legacy V1 relayer settings. For
live mode provide the V2 signer/API credentials and verify the selected token
through Polymarket’s official [trading documentation](https://docs.polymarket.com/trading/overview).

## MySQL → PostgreSQL migration

The bot never connects to MySQL at runtime. The optional migration tool is a
one-time copy operation.

1. Stop bot/dashboard and back up MySQL.
2. Copy `cp .env.migration.example .env.migration` and fill `MYSQL_*` source
   values plus `POSTGRES_*` target values.
3. Use an empty PostgreSQL target. The script takes the same database leader
   lock as the bot and refuses populated state tables.
4. Run:

   ```bash
   python -m pip install --require-hashes -r requirements-migration.lock
   python scripts/migrate_mysql_to_postgres.py --env-file .env.migration
   ```

5. The selected `--env-file` replaces ambient environment variables; it is the
   entire migration configuration. The script refuses any source
   non-terminal/unknown order and a carried position without finite
   `0 < stop-loss < entry < take-profit` protection. Reconcile either condition
   manually before retrying.
6. Compare source/target counts and dashboard data before retiring MySQL
   credentials. The script copies positions, trades, metadata, and equity
   history atomically and does not modify the source database.

## Validation

```bash
python -m pytest
python -m ruff check .
python -m pip_audit -r requirements.lock
python -m compileall -q .
```
