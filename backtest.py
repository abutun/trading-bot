import logging

from market_data import MarketData
from strategy import EMARSI


logger = logging.getLogger(__name__)


def run_backtest(config) -> None:
    market_data = MarketData(config)
    strategy = EMARSI(config)

    pairs = config.trading_pairs
    if not pairs:
        logger.warning("No trading pairs configured for backtest")
        return

    capital_per_pair = config.paper_initial_capital / len(pairs)
    total_final_equity = 0.0

    for pair in pairs:
        try:
            df = market_data.fetch_ohlcv(
                pair.symbol,
                limit=min(1000, max(config.candle_limit * 5, 300)),
            )
        except Exception as exc:
            logger.warning("Backtest fetch failed for %s: %s", pair.symbol, exc)
            continue

        if df.empty:
            continue

        out = strategy.compute(df)

        cash = capital_per_pair
        positions: dict[str, dict] = {}
        last_prices: dict[str, float] = {}

        warmup = max(config.ema_slow, config.rsi_period, config.atr_period) + 1

        for i in range(warmup, len(out)):
            row = out.iloc[i]

            price = float(row["close"])
            action = int(row["action"])

            last_prices[pair.symbol] = price
            key = pair.pair_id

            if key in positions:
                pos = positions[key]

                if action == -1 or price <= pos["stop_loss"] or price >= pos["take_profit"]:
                    fill_price = price * (1 - config.slippage_bps / 10_000)
                    proceeds = pos["qty"] * fill_price * (1 - config.fee_rate)

                    cash += proceeds
                    del positions[key]

            else:
                if action == 1:
                    equity = cash + sum(
                        p["qty"] * last_prices.get(sym, p["entry"])
                        for sym, p in positions.items()
                    )

                    qty = equity * (config.max_position_pct / 100) / price if price > 0 else 0
                    fill_price = price * (1 + config.slippage_bps / 10_000)
                    cost = qty * fill_price * (1 + config.fee_rate)

                    if qty * price >= config.min_notional and cost <= cash:
                        cash -= cost

                        positions[key] = {
                            "qty": qty,
                            "entry": fill_price,
                            "stop_loss": float(row["stop_loss"]),
                            "take_profit": float(row["take_profit"]),
                        }

        final_equity = cash + sum(
            p["qty"] * last_prices.get(sym, p["entry"])
            for sym, p in positions.items()
        )

        total_final_equity += final_equity

        return_pct = (final_equity / capital_per_pair - 1) * 100 if capital_per_pair > 0 else 0.0

        logger.info(
            "Backtest %s initial=%.2f final=%.2f return=%.2f%%",
            pair.pair_id,
            capital_per_pair,
            final_equity,
            return_pct,
        )

    logger.info("Backtest total final equity=%.2f", total_final_equity)
