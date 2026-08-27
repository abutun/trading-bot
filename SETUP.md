# Setup and operations guide

## Prerequisites

- Python 3.10+ (3.11 recommended)
- PostgreSQL 14+ (16 recommended)
- A venue account only when using live mode

For macOS:

```bash
brew install postgresql@16
brew services start postgresql@16
```

Or use the included Compose service:

```bash
cp .env.example .env
# Set POSTGRES_PASSWORD in .env before starting the service.
docker compose up -d postgres
```

## Install the application

```bash
cd /Users/ahmet/Documents/Workspaces/Buhane/trading-bot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

Generate a dashboard signing secret and put it in `.env`:

```bash
openssl rand -hex 32
```

At minimum, set:

```dotenv
BOT_MODE=paper
TRADING_PAIRS=binance:BTC/USDT
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_USER=tradingbot
POSTGRES_PASSWORD=<long-random-password>
POSTGRES_DATABASE=trading_bot
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=<long-random-password>
DASHBOARD_SECRET_KEY=<openssl-output>
```

`state.py` creates all application tables automatically. The initial schema is
checked into [`migrations/001_initial.sql`](migrations/001_initial.sql) for
review and managed deployments.

## Safe first run

Start only in paper mode:

```bash
BOT_MODE=paper python main.py
```

In a second terminal, with the same `.env`:

```bash
python dashboard.py
```

Open `http://127.0.0.1:8080`. The dashboard login has CSRF protection,
HttpOnly/Lax session cookies, and no insecure fallback secret. For remote
access, place it behind a TLS reverse proxy. Do not expose Flask’s development
server directly to the Internet.

## Venue setup

### Binance

```dotenv
BOT_MODE=live
TRADING_PAIRS=binance:BTC/USDT
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
BINANCE_TESTNET=true
```

Use dedicated testnet keys first. Mainnet keys should be Spot-only,
withdrawal-disabled, and IP-allowlisted where possible. Switch to
`BINANCE_TESTNET=false` only after paper/testnet verification.

### EVM / MetaMask

```dotenv
BOT_MODE=live
TRADING_PAIRS=evm:ETH/USDT
EVM_PRIVATE_KEY=0x...
EVM_RPC_URL=https://your-rpc.example
EVM_CHAIN_ID=1
EVM_ROUTER_ADDRESS=0x...
EVM_SYMBOLS={"ETH/USDT":{"base":"<WETH-address>","quote":"<USDC-address>"}}
EVM_SLIPPAGE_BPS=50
EVM_APPROVE_MAX=false
```

This broker trades ERC-20 paths on a Uniswap V2-compatible router. Use WETH,
not native ETH. The default exact-amount approval is safer but can require a
new approval for future swaps. A dedicated hot wallet must hold both swap funds
and the native gas token.

### Polymarket

Polymarket symbols are your local aliases for a YES or NO outcome token:

```dotenv
BOT_MODE=paper
TRADING_PAIRS=polymarket:example-yes
POLYMARKET_MARKETS={"example-yes":{"token_id":"<outcome-token-id>"}}
POLYMARKET_HISTORY_INTERVAL=1d
POLYMARKET_FIDELITY_MINUTES=15
```

For live orders:

```dotenv
BOT_MODE=live
TRADING_PAIRS=polymarket:example-yes
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_WALLET_ADDRESS=0x...
POLYMARKET_RELAYER_API_KEY=...
POLYMARKET_RELAYER_API_KEY_ADDRESS=0x...
POLYMARKET_SLIPPAGE_BPS=100
POLYMARKET_MARKETS={"example-yes":{"token_id":"<outcome-token-id>"}}
```

`POLYMARKET_PRIVATE_KEY` is compatible with a MetaMask signer but must be a
dedicated trading key. Fund the account with pUSD and configure Polymarket
trading approvals before enabling live mode. The broker uses the official
`polymarket-client` SDK and fill-or-kill market orders; no partial strategy
position is accepted. Consult Polymarket’s official [wallet setup](https://docs.polymarket.com/trading/wallets-auth), [order placement](https://docs.polymarket.com/trading/place-orders), and [price history](https://docs.polymarket.com/market-data/prices-order-books) documentation before funding or trading.

## MySQL → PostgreSQL migration

The application no longer connects to MySQL at runtime. The optional migration
script is the only remaining MySQL consumer.

1. Stop `main.py` and `dashboard.py`.
2. Back up the source database, for example:

   ```bash
   mysqldump --single-transaction -u tradingbot -p trading_bot > trading_bot_backup.sql
   ```

3. Create `.env.migration` with the legacy `MYSQL_HOST`, `MYSQL_PORT`,
   `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE` values and target
   `POSTGRES_*` values.
4. Run:

   ```bash
   pip install -r requirements-migration.txt
   python scripts/migrate_mysql_to_postgres.py --env-file .env.migration
   ```

The target must have no positions. The script copies legacy positions, trades,
metadata, and equity history into PostgreSQL atomically; it never deletes data
from MySQL. Verify row counts and the dashboard, then retire MySQL credentials.

## Normal operations

```bash
python main.py --backtest
BOT_MODE=paper python main.py
python dashboard.py
```

Before every live deployment:

- Review open positions and external venue balances.
- Verify the dashboard is connected to the intended PostgreSQL database.
- Confirm the bot’s first heartbeat appears.
- Check the `orders` table/dashboard for unresolved `pending` or `unknown`
  rows. Do not clear an unresolved order until its venue result and resulting
  position have been reconciled manually.
- Back up PostgreSQL regularly, e.g. `pg_dump -Fc trading_bot > backup.dump`.

## Troubleshooting

| Symptom | Check |
|---|---|
| PostgreSQL connection failure | PostgreSQL is running, `POSTGRES_*` are correct, and the user can connect to the named database. |
| Polymarket has no data | Alias exists in `POLYMARKET_MARKETS`, token ID is the selected outcome token, and the outcome has price history. |
| Polymarket order rejected | Account has pUSD, trading approvals are set, wallet/signer pairing is correct, and the market accepts orders. |
| Bot skips a pair as unresolved | Inspect the venue order/transaction and database `orders` record; this is an intentional duplicate-order safeguard. |
| Dashboard refuses to start | Set a non-empty `DASHBOARD_PASSWORD` and a cryptographically random `DASHBOARD_SECRET_KEY`. |
