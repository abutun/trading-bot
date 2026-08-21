# Setup Guide

This guide explains how to configure, install, and run the trading bot.

Target folder:

```text
/Users/ahmet/Documents/Workspaces/Buhane/trading-bot
```

## 1. Prerequisites

You need:

- Python 3.10 or newer
- MySQL 8 (or MariaDB) for bot state — positions, trades, equity history
- A terminal
- Optional: Docker
- Optional: AWS CLI / boto3 if using AWS Secrets Manager

Check Python:

```bash
python3 --version
```

If you need to install Python on macOS using Homebrew:

```bash
brew install python@3.12
```

Install MySQL locally (macOS example):

```bash
brew install mysql
brew services start mysql
```

## 2. Project files

All files live in:

```text
/Users/ahmet/Documents/Workspaces/Buhane/trading-bot
```

Verify:

```bash
ls -la /Users/ahmet/Documents/Workspaces/Buhane/trading-bot
```

Expected files include:

```text
requirements.txt
.env.example
Dockerfile
main.py           # CLI entry point: run the bot or a backtest
dashboard.py      # monitoring dashboard entry point
backtest.py       # historical backtest of the strategy
config.py         # all settings, read from environment variables
secrets.py        # secret loading (env / .env / AWS Secrets Manager)
models.py         # data models (Signal, Position, Trade)
state.py          # MySQL-backed state store (auto-creates tables)
risk.py           # position sizing, drawdown kill-switch, daily loss limit
strategy.py       # EMA crossover + RSI strategy with ATR stops
market_data.py    # OHLCV data via ccxt
brokers/          # venue adapters: binance_broker.py (ccxt), evm_broker.py (web3.py / MetaMask key), paper.py
dashboard/        # Flask dashboard app + templates
README.md
SETUP.md
```

## 3. Install dependencies

Open the project folder:

```bash
cd /Users/ahmet/Documents/Workspaces/Buhane/trading-bot
```

Create a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

If you plan to use AWS Secrets Manager:

```bash
pip install boto3
```

## 4. Create the MySQL database

Tables are created automatically by `state.py` on first run, but the database and user must exist:

```bash
mysql -u root -p
```

```sql
CREATE DATABASE IF NOT EXISTS trading_bot;
CREATE USER 'tradingbot'@'localhost' IDENTIFIED BY 'change-me';
GRANT ALL PRIVILEGES ON trading_bot.* TO 'tradingbot'@'localhost';
FLUSH PRIVILEGES;
```

Then set `MYSQL_USER` / `MYSQL_PASSWORD` in `.env`.

## 5. Configure environment variables

Create your local `.env` file:

```bash
cp .env.example .env
nano .env
```

Do not commit `.env` to Git.

### Minimum paper trading configuration

For a safe first run:

```bash
BOT_MODE=paper
TRADING_PAIRS=binance:BTC/USDT,evm:ETH/USDT
PAPER_INITIAL_CAPITAL=1000
```

Then run:

```bash
python3 main.py
```

### Live Binance configuration

Example (testnet first):

```bash
BOT_MODE=live
TRADING_PAIRS=binance:BTC/USDT

BINANCE_API_KEY=your_binance_api_key
BINANCE_API_SECRET=your_binance_api_secret
BINANCE_TESTNET=true
```

For Binance mainnet:

```bash
BINANCE_TESTNET=false
```

Important:

- Use testnet keys when `BINANCE_TESTNET=true`.
- Use mainnet keys when `BINANCE_TESTNET=false`.
- Enable only the permissions you need (spot trading).
- Disable withdrawals on the API key.
- Use IP allowlisting if available.

### Live EVM / MetaMask wallet configuration

Example for Ethereum mainnet:

```bash
BOT_MODE=live
TRADING_PAIRS=evm:ETH/USDT

EVM_PRIVATE_KEY=0xyour_private_key
EVM_RPC_URL=https://eth.llamarpc.com
EVM_CHAIN_ID=1
EVM_ROUTER_ADDRESS=0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D
EVM_SLIPPAGE_BPS=50

EVM_SYMBOLS={"ETH/USDT":{"base":"0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2","quote":"0xA0b86991c6218b36c1d19D4a2e9Eb0C6E3606eB48"}}
```

Important:

- Use a **dedicated hot wallet** — never your main MetaMask wallet.
- Keep only trading capital plus gas in that wallet.
- Make sure the token addresses are correct for your chain.
- For native ETH, use WETH as the base token (the bot swaps ERC20 tokens via a DEX router).

## 6. Environment variable reference

### Core

| Variable | Required | Description |
|---|---:|---|
| `BOT_MODE` | Yes | `paper` or `live` |
| `TRADING_PAIRS` | Yes | Comma-separated pairs, e.g. `binance:BTC/USDT,evm:ETH/USDT` |
| `MARKET_DATA_EXCHANGE` | No | ccxt exchange id for market data, default `binance` |
| `INTERVAL` | No | Candle interval, e.g. `15m`, `1h`, `4h` |
| `LOOP_SECONDS` | No | Seconds between bot loops, default `60` |
| `CANDLE_LIMIT` | No | Number of candles to fetch, default `200` |
| `QUOTE_CURRENCY` | No | Quote currency used for Binance equity, default `USDT` |

### Strategy

| Variable | Required | Description |
|---|---:|---|
| `EMA_FAST` | No | Fast EMA period, default `12` |
| `EMA_SLOW` | No | Slow EMA period, default `26` |
| `RSI_PERIOD` | No | RSI period, default `14` |
| `ATR_PERIOD` | No | ATR period, default `14` |
| `STOP_LOSS_ATR_MULT` | No | Stop-loss ATR multiplier, default `2.0` |
| `TAKE_PROFIT_ATR_MULT` | No | Take-profit ATR multiplier, default `3.0` |

### Risk

| Variable | Required | Description |
|---|---:|---|
| `MAX_POSITION_PCT` | No | Max percentage of equity per position, default `10` |
| `MAX_DAILY_LOSS_PCT` | No | Daily loss limit percentage, default `2` |
| `MAX_TOTAL_DRAWDOWN_PCT` | No | Total drawdown kill switch percentage, default `10` |
| `MIN_NOTIONAL_USD` | No | Minimum trade notional, default `10` |
| `MAX_OPEN_POSITIONS` | No | Maximum open positions, default `3` |

### Costs / paper trading

| Variable | Required | Description |
|---|---:|---|
| `FEE_RATE` | No | Assumed fee rate, default `0.001` |
| `SLIPPAGE_BPS` | No | Assumed slippage in basis points, default `5` |
| `PAPER_INITIAL_CAPITAL` | No | Paper trading starting capital, default `1000` |

### Binance

| Variable | Required | Description |
|---|---:|---|
| `BINANCE_API_KEY` | Conditional | Required for live Binance trading |
| `BINANCE_API_SECRET` | Conditional | Required for live Binance trading |
| `BINANCE_TESTNET` | No | Use Binance testnet, default `true` |

### EVM / MetaMask wallet

| Variable | Required | Description |
|---|---:|---|
| `EVM_PRIVATE_KEY` | Conditional | Required for live EVM trading (your MetaMask private key) |
| `EVM_RPC_URL` | Conditional | Required for live EVM trading (Infura/Alchemy/public RPC) |
| `EVM_CHAIN_ID` | No | Chain ID, default `1` (Ethereum mainnet) |
| `EVM_ROUTER_ADDRESS` | Conditional | DEX router address (Uniswap V2 / PancakeSwap) |
| `EVM_GAS_PRICE_GWEI` | No | Optional fixed gas price in Gwei; empty = use node suggestion |
| `EVM_SLIPPAGE_BPS` | No | DEX slippage tolerance in basis points, default `50` |
| `EVM_SYMBOLS` | Conditional | JSON mapping from symbol to ERC20 token addresses (base/quote) |

### MySQL state database

| Variable | Required | Description |
|---|---:|---|
| `MYSQL_HOST` | No | MySQL host, default `127.0.0.1` |
| `MYSQL_PORT` | No | MySQL port, default `3306` |
| `MYSQL_USER` | Yes | Database user (tables auto-created) |
| `MYSQL_PASSWORD` | Yes | Database password |
| `MYSQL_DATABASE` | No | Database name, default `trading_bot` |

### Monitoring dashboard

| Variable | Required | Description |
|---|---:|---|
| `DASHBOARD_USERNAME` | No | Login username, default `admin` |
| `DASHBOARD_PASSWORD` | Yes (for dashboard) | Login password |
| `DASHBOARD_SECRET_KEY` | Yes (for dashboard) | Long random string for session cookies (`openssl rand -hex 32`) |
| `DASHBOARD_HOST` | No | Bind address, default `127.0.0.1` |
| `DASHBOARD_PORT` | No | Port, default `8080` |

### Secrets manager

| Variable | Required | Description |
|---|---:|---|
| `SECRET_MANAGER` | No | Set to `aws` to load secrets from AWS Secrets Manager |
| `SECRET_ID` | Conditional | AWS Secret ID when using AWS Secrets Manager |

### Logging

| Variable | Required | Description |
|---|---:|---|
| `LOG_LEVEL` | No | Logging level, default `INFO` |
| `LOG_FILE` | No | Optional log file path |

## 7. Run the bot

Paper trading (safe first run):

```bash
BOT_MODE=paper python3 main.py
```

Live trading:

```bash
BOT_MODE=live python3 main.py
```

Backtest the strategy on recent candles:

```bash
python3 main.py --backtest
```

The bot logs every loop: equity, signals, orders, and risk state. State (positions, trades, equity history) is persisted in MySQL, so the bot survives restarts.

## 8. Run the monitoring dashboard

In a second terminal:

```bash
python3 dashboard.py
```

Then open `http://127.0.0.1:8080` and sign in with `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD`.

The dashboard shows:

- Live equity, daily PnL, peak equity and drawdown
- Equity history chart (refreshes every 30 seconds)
- Open positions with live prices and unrealized PnL
- Recent trades (side, qty, price, fees)
- Bot online/offline status (heartbeat-based)

For remote access put it behind a reverse proxy with TLS, e.g. nginx:

```nginx
server {
    listen 443 ssl;
    server_name bot.example.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

## 9. Optional: AWS Secrets Manager

If you want to store secrets in AWS Secrets Manager instead of `.env`:

1. Install boto3: `pip install boto3`
2. Configure AWS credentials on the machine running the bot.
3. Create a JSON secret, for example:

```json
{
  "BINANCE_API_KEY": "...",
  "BINANCE_API_SECRET": "...",
  "EVM_PRIVATE_KEY": "0x..."
}
```

4. Set in `.env`:

```bash
SECRET_MANAGER=aws
SECRET_ID=trading-bot/secrets
```

The bot loads the secret into environment variables at startup. You can use a similar approach with GCP Secret Manager, HashiCorp Vault, Azure Key Vault, Doppler, or Infisical.

## 10. Docker usage

Build:

```bash
docker build -t trading-bot .
```

Run with environment file (bot):

```bash
docker run --env-file .env trading-bot
```

Run the dashboard:

```bash
docker run -p 8080:8080 --env-file .env trading-bot python dashboard.py
```

## 11. BSC / PancakeSwap example

For Binance Smart Chain:

```bash
EVM_RPC_URL=https://bsc-dataseed.binance.org
EVM_CHAIN_ID=56
EVM_ROUTER_ADDRESS=0x10ED43C718714eb63d5aA57B78B54704E256024E
```

Example token mapping:

- WBNB: `0xbb4CdB9CBd36B01bD1cBaEF60aF814a3f6F0Ee75`
- USDT on BSC: `0x55d398326f99059fF775485246999027B3197955`

Example:

```bash
EVM_SYMBOLS={"BNB/USDT":{"base":"0xbb4CdB9CBd36B01bD1cBaEF60aF814a3f6F0Ee75","quote":"0x55d398326f99059fF775485246999027B3197955"}}
TRADING_PAIRS=binance:BNB/USDT,evm:BNB/USDT
```

## 12. Polygon example

For Polygon:

```bash
EVM_RPC_URL=https://polygon-rpc.com
EVM_CHAIN_ID=137
```

Use a Polygon DEX router (e.g. Uniswap V2 on Polygon: `0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D`) and Polygon token addresses in `EVM_SYMBOLS`.

## 13. Troubleshooting

### `ModuleNotFoundError: No module named 'ccxt'` (or web3, flask)

Activate the virtual environment and install dependencies:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

### MySQL connection error on startup

- Is MySQL running? `brew services start mysql` (macOS) or `systemctl status mysql`.
- Do the user/database exist? See section 4.
- Are `MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASSWORD` correct in `.env`?

### Binance testnet authentication error

Make sure you are using Binance **testnet** API keys when `BINANCE_TESTNET=true`, and mainnet keys only when `BINANCE_TESTNET=false`.

### EVM `Insufficient quote token balance`

Make sure the wallet has enough of the quote token (e.g. USDC or USDT) plus gas.

### EVM `Insufficient base token balance`

Make sure the wallet has enough of the base token (e.g. WETH) to sell.

### EVM transaction failed / stuck

Check:

- Gas balance (native coin) in the wallet
- RPC connectivity and rate limits
- Token addresses for your chain
- Router address supports the pair
- Slippage settings (`EVM_SLIPPAGE_BPS`)

### No liquidity for buy/sell on DEX

Check token addresses, router address, pair liquidity, chain ID, and whether the tokens are supported by that router.

### Dashboard says "Bot offline"

The dashboard shows online only if the bot wrote a heartbeat within 3 minutes. Make sure the bot process (`python3 main.py`) is running and both processes share the same MySQL database.

### State mismatch after restart

The bot stores positions in MySQL (`positions` table). For production, periodically reconcile:

- Binance balances vs bot state
- EVM wallet token balances vs bot state

## 14. Go-live checklist

Before using real money:

- [ ] Paper trade for at least 1–4 weeks.
- [ ] Run Binance testnet first (`BINANCE_TESTNET=true`).
- [ ] Run EVM on a fork/testnet if possible.
- [ ] Use small capital initially.
- [ ] Use dedicated API keys and a dedicated hot wallet.
- [ ] Enable IP allowlist on Binance; disable withdrawals.
- [ ] Monitor logs and the dashboard continuously.
- [ ] Back up the MySQL database regularly (`mysqldump trading_bot > backup.sql`).
- [ ] Add alerts for errors, failed transactions, and drawdowns.
- [ ] Test restart behavior after crash (state is in MySQL).
- [ ] Verify nonce handling and gas limits on the EVM chain.
- [ ] Add reconciliation between bot state and exchange/wallet balances.
