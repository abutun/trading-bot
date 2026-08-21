# Trading Bot

A production-grade Python trading bot that runs the same strategy across two venues:

- **Binance** (spot, via ccxt)
- **MetaMask / EVM chains** (via web3.py — the bot signs and sends transactions with your MetaMask private key, so it works on Ethereum mainnet or any EVM chain you configure)

## Architecture

```
config.py        All settings, read from environment variables (or AWS Secrets Manager)
secrets.py       Secret loading: env vars, .env file, or AWS Secrets Manager
market_data.py   OHLCV data via ccxt (works for both venues' symbols)
strategy.py      EMA + RSI momentum strategy with trend filter and ATR-based stops
risk.py          Position sizing, max drawdown kill-switch, daily loss limit
brokers/         Venue adapters: binance_broker.py (ccxt), evm_broker.py (web3.py / MetaMask key), paper.py
state.py         MySQL-backed state: positions, trades, equity history (auto-created tables)
bot.py           Main loop: fetch data -> signal -> risk check -> execute -> manage exits
dashboard/       Flask monitoring dashboard (login-protected)
main.py          CLI entry point: `python main.py` (bot) or `python main.py --backtest`
dashboard.py     Dashboard entry point: `python dashboard.py`
```

## Strategy (EMA crossover + RSI filter)

- **Long entry**: fast EMA above slow EMA (uptrend) and RSI below 70 (not overbought).
- **Exit**: fast EMA below slow EMA and RSI above 30, or the ATR-based stop loss / take profit is hit.
- **Position sizing**: fixed fraction of equity per trade, capped by `MAX_POSITION_PCT`.
- **Risk management**: max drawdown kill-switch (closes all, halts trading), daily loss limit.

All parameters (`EMA_FAST`, `EMA_SLOW`, `RSI_PERIOD`, ATR multipliers, risk limits) are configurable via environment variables.

## Secrets & configuration

All secrets are read from **environment variables** (or a `.env` file for local dev, or AWS Secrets Manager in production). See `.env.example` for the full list.

| Variable | Description |
|---|---|
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | Binance API credentials (create with spot trading only, no withdrawals) |
| `EVM_PRIVATE_KEY` | Your MetaMask wallet private key (0x-prefixed hex) — the bot signs EVM transactions with it |
| `EVM_RPC_URL` | RPC endpoint (e.g. Infura/Alchemy URL) for the EVM chain |
| `EVM_CHAIN_ID` | Chain ID (1 = Ethereum mainnet, 10 = Optimism, ...) |
| `EVM_GAS_PRICE_GWEI` | Optional fixed gas price; if empty the bot uses the node's suggested fee |
| `EVM_SYMBOLS` | JSON mapping ccxt symbol -> ERC20 token addresses (base/quote) for swaps |
| `MYSQL_HOST` / `MYSQL_PORT` / `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DATABASE` | MySQL state database (tables auto-created) |
| `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` / `DASHBOARD_SECRET_KEY` | Dashboard login and session signing key |
| `SECRET_MANAGER=aws` + `SECRET_ID` | Optional: load all secrets from AWS Secrets Manager instead of env vars |

### Security notes

- The MetaMask private key gives full control of that wallet. Use a **dedicated hot wallet** with only the funds you want the bot to trade — never your main MetaMask wallet.
- For Binance, create an API key with **spot trading enabled and withdrawals disabled**.
- In production prefer AWS Secrets Manager (`SECRET_MANAGER=aws`) over a `.env` file.
- The dashboard binds to `127.0.0.1` by default — put it behind a reverse proxy (nginx + TLS) if you need remote access.

## Running

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure secrets
cp .env.example .env   # then fill in real values

# 3. Create the MySQL database (tables are created automatically)
mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS trading_bot; CREATE USER 'tradingbot'@'localhost' IDENTIFIED BY 'change-me'; GRANT ALL ON trading_bot.* TO 'tradingbot'@'localhost';"

# 4. Run the bot
python main.py

# 5. In another terminal, run the monitoring dashboard
python dashboard.py    # then open http://127.0.0.1:8080
```

The dashboard shows live equity, daily PnL, drawdown, open positions (with live prices), recent trades, and an equity chart. It refreshes every 30 seconds.

## Backtesting / paper trading

- Set `BOT_MODE=paper` to run the full loop with simulated fills — no real orders are sent anywhere, and no API keys or wallet key are needed.
- Run a historical backtest of the strategy on recent candles:

```bash
python main.py --backtest
```

- The strategy parameters in `.env` (`EMA_FAST`, `EMA_SLOW`, `RSI_PERIOD`, etc.) can be tuned; validate changes on paper before going live.

## Honest expectations

This bot is engineered to be robust and low-risk (trend filter, ATR stops, drawdown kill-switch), but **no trading bot can guarantee profits** — returns depend on market conditions, fees and slippage. Start with a small amount in paper mode or testnet, verify behavior over weeks, then scale up.
