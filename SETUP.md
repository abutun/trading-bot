# Setup Guide

This guide explains how to configure, install, and run the trading bot.

Target folder:

```text
/Users/ahmet/Documents/Workspaces/Buhane/trading-bot
```

## 1. Prerequisites

You need:

- Python 3.10 or newer
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

## 2. Create project files

If you used the provided creation script, all files should already exist in:

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
main.py
config.py
models.py
state.py
risk.py
strategy.py
market_data.py
backtest.py
bot.py
brokers/
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
```

Activate it:

```bash
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

## 4. Configure environment variables

Create your local `.env` file:

```bash
cp .env.example .env
```

Edit it:

```bash
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
python3 main.py --backtest
python3 main.py
```

### Live Binance configuration

Example:

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
- Enable only the permissions you need.
- Disable withdrawals if possible.
- Use IP allowlisting.

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

- Use a dedicated hot wallet.
- Keep only trading capital plus gas in that wallet.
- Make sure the token addresses are correct for your chain.
- For native ETH, use WETH as the base token unless you extend the broker to support native ETH swaps.

## 5. Environment variable reference

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
| `EVM_PRIVATE_KEY` | Conditional | Required for live EVM trading |
| `EVM_RPC_URL` | Conditional | Required for live EVM trading |
| `EVM_CHAIN_ID` | No | Chain ID, default `1` |
| `EVM_ROUTER_ADDRESS` | Conditional | Required for live EVM trading |
| `EVM_GAS_PRICE_GWEI` | No | Optional fixed gas price in Gwei |
| `EVM_SLIPPAGE_BPS` | No | DEX slippage tolerance in basis points, default `50` |
| `EVM_SYMBOLS` | Conditional | JSON mapping from symbol to ERC20 token addresses |

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

## 6. Optional: AWS Secrets Manager

If you want to store secrets in AWS Secrets Manager instead of `.env`:

1. Install boto3:

```bash
pip install boto3
```

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

The bot will load the secret into environment variables at startup.

You can use a similar approach with GCP Secret Manager, HashiCorp Vault, Azure Key Vault, Doppler, or Infisical.

## 7. Run backtest

Run a simple backtest:

```bash
python3 main.py --backtest
```

Example output will show final equity and return for each configured pair.

Important: this backtester is simple. For production decisions, use more rigorous walk-forward testing and out-of-sample validation.

## 8. Run paper trading

Make sure `.env` contains:

```bash
BOT_MODE=paper
```

Run:

```bash
python3 main.py
```

Paper trading uses simulated fills and stores state in `state.db`.

## 9. Run live trading

Make sure `.env` contains:

```bash
BOT_MODE=live
```

Run:

```bash
python3 main.py
```

Before live trading:

- Run paper trading first.
- Use small capital.
- Monitor logs.
- Verify balances and positions.
- Test restart behavior.

## 10. Docker usage

Build:

```bash
docker build -t trading-bot .
```

Run with environment file:

```bash
docker run --env-file .env trading-bot
```

For backtest:

```bash
docker run --env-file .env trading-bot python main.py --backtest
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

Use a Polygon DEX router and Polygon token addresses in `EVM_SYMBOLS`.

## 13. Troubleshooting

### `ModuleNotFoundError: No module named 'ccxt'`

Activate the virtual environment and install dependencies:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

### Binance testnet authentication error

Make sure you are using Binance testnet API keys when:

```bash
BINANCE_TESTNET=true
```

Use mainnet API keys only when:

```bash
BINANCE_TESTNET=false
```

### EVM `Insufficient quote token balance`

Make sure the wallet has enough of the quote token, for example USDC or USDT.

### EVM `Insufficient base token balance`

Make sure the wallet has enough of the base token, for example WETH.

### EVM transaction failed

Check:

- Gas balance
- RPC connectivity
- Token addresses
- Router address
- Chain ID
- Slippage settings

### No liquidity for buy/sell

Check:

- Token addresses
- Router address
- Pair liquidity
- Chain ID
- Whether the tokens are supported by that router

### State mismatch after restart

The bot stores positions in `state.db`. For production, periodically reconcile:

- Binance balances vs bot state
- EVM wallet token balances vs bot state

## 14. Go-live checklist

Before using real money:

- [ ] Paper trade for at least 1–4 weeks.
- [ ] Run Binance testnet first.
- [ ] Run EVM on a fork/testnet if possible.
- [ ] Use small capital initially.
- [ ] Use dedicated API keys and wallet.
- [ ] Enable IP allowlist on Binance.
- [ ] Disable withdrawals if possible.
- [ ] Monitor logs continuously.
- [ ] Back up `state.db`.
- [ ] Add alerts for errors, failed transactions, and drawdowns.
- [ ] Test restart behavior after crash.
- [ ] Verify nonce handling on EVM chain.
- [ ] Verify gas limits are sufficient for your tokens/DEX.
- [ ] Add reconciliation between bot state and exchange/wallet balances.
