# Trading Bot — Binance + MetaMask/EVM

A production-grade Python trading bot that can trade:

- **Binance spot** using API keys.
- **EVM / MetaMask wallets** by using the wallet private key and executing DEX swaps with `web3.py`.

> **Important:** No trading bot can guarantee profit. This project provides a robust execution and risk-management framework plus an example strategy. You should backtest, paper trade, optimize, and monitor before using meaningful capital.

## Features

- Multi-venue trading:
  - Binance spot via `ccxt`
  - EVM/MetaMask DEX swaps via `web3.py`
- Secrets from environment variables or AWS Secrets Manager.
- Paper trading and live trading modes.
- Risk manager:
  - Max position size
  - Daily loss limit
  - Total drawdown kill switch
  - Stop-loss / take-profit
- SQLite state persistence:
  - Positions
  - Trades
  - Risk metadata
- Simple backtester.
- Docker support.

## Project structure

```text
trading-bot/
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
    __init__.py
    base.py
    paper.py
    binance_broker.py
    evm_broker.py
  README.md
  SETUP.md
```

## Quick start

```bash
cd /Users/ahmet/Documents/Workspaces/Buhane/trading-bot

python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt

cp .env.example .env
nano .env

python3 main.py --backtest
python3 main.py
```

## Configuration summary

The bot is configured through environment variables or `.env`.

Key variables:

| Variable | Description |
|---|---|
| `BOT_MODE` | `paper` or `live` |
| `TRADING_PAIRS` | Comma-separated venue:symbol pairs, e.g. `binance:BTC/USDT,evm:ETH/USDT` |
| `MARKET_DATA_EXCHANGE` | ccxt exchange id used for market data, e.g. `binance` |
| `INTERVAL` | Candle interval, e.g. `15m`, `1h`, `4h` |
| `LOOP_SECONDS` | Seconds between bot loops |
| `BINANCE_API_KEY` | Binance API key |
| `BINANCE_API_SECRET` | Binance API secret |
| `BINANCE_TESTNET` | Use Binance testnet when true |
| `EVM_PRIVATE_KEY` | Private key for the EVM/MetaMask wallet |
| `EVM_RPC_URL` | Ethereum-compatible RPC URL |
| `EVM_CHAIN_ID` | Chain ID, e.g. `1` for Ethereum mainnet, `56` for BSC |
| `EVM_ROUTER_ADDRESS` | DEX router address, e.g. Uniswap V2 or PancakeSwap |
| `EVM_SYMBOLS` | JSON mapping from symbol to ERC20 token addresses |

See `SETUP.md` for the full configuration guide.

## Security notes

- Do not commit `.env`.
- Use a dedicated Binance API key.
- Disable withdrawals on the Binance API key if possible.
- Use IP allowlisting for Binance API keys.
- For EVM/MetaMask trading, use a dedicated hot wallet.
- Do not put your main MetaMask wallet private key in the bot unless you fully understand the risk.
- For larger capital, consider AWS Secrets Manager, GCP Secret Manager, Vault, or an institutional signing solution.

## Running modes

### Backtest

```bash
python3 main.py --backtest
```

### Paper trading

Set:

```bash
BOT_MODE=paper
```

Then run:

```bash
python3 main.py
```

### Live trading

Set:

```bash
BOT_MODE=live
```

Then run:

```bash
python3 main.py
```

## EVM / MetaMask notes

The bot does not control the MetaMask browser extension. It uses the private key behind your MetaMask wallet directly.

The EVM broker currently assumes:

- ERC20 tokens.
- A Uniswap V2-style router.
- Wrapped base assets, e.g. WETH instead of native ETH.
- Stable quote tokens such as USDC or USDT valued approximately 1:1 in equity calculation.

For BSC, use PancakeSwap router and BSC token addresses.  
For Polygon, use a Polygon DEX router and Polygon token addresses.

## Profitability notes

The included strategy is an example EMA/RSI strategy with ATR-based stops. It is not guaranteed to be profitable in all markets.

To improve success:

- Backtest with fees and slippage.
- Use out-of-sample data.
- Paper trade for several weeks.
- Optimize strategy parameters carefully.
- Add better market data features.
- Reduce fees and slippage.
- Add monitoring, alerts, and reconciliation.
