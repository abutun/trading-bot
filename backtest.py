import logging

from market_data import MarketData
from strategy import EMARSI


logger = logging.getLogger(__name__)


def run_backtest(config) -> None:
    """Run one isolated, long-only backtest per configured pair.

    This deliberately does not share capital across pairs: it is a quick
    strategy sanity check, not a portfolio simulator. Prices are executed on
    the next bar to avoid making a decision on information from the same close.
    """
    market_data = MarketData(config)
    strategy = EMARSI(config)
    pairs = config.trading_pairs
    capital_per_pair = config.paper_initial_capital / len(pairs)
    total_final_equity = 0.0

    try:
        for pair in pairs:
            try:
                df = market_data.fetch_ohlcv(
                    pair.symbol,
                    limit=min(1000, max(config.candle_limit * 5, 300)),
                    venue=pair.venue,
                )
            except Exception as exc:
                logger.warning("Backtest fetch failed for %s: %s", pair.pair_id, exc)
                continue

            if df.empty:
                logger.warning("Backtest has no data for %s", pair.pair_id)
                continue

            out = strategy.compute(df)
            warmup = max(config.ema_slow, config.rsi_period, config.atr_period) + 1
            if len(out) <= warmup + 1:
                logger.warning("Backtest has insufficient history for %s", pair.pair_id)
                continue

            cash = capital_per_pair
            position: dict | None = None
            # Signals from row i execute at row i+1's open to avoid look-ahead.
            for i in range(warmup, len(out) - 1):
                signal_row = out.iloc[i]
                execution_price = float(out.iloc[i + 1]["open"])

                if position is not None:
                    if (
                        int(signal_row["action"]) == -1
                        or execution_price <= position["stop_loss"]
                        or execution_price >= position["take_profit"]
                    ):
                        fill_price = execution_price * (1 - config.slippage_bps / 10_000)
                        cash += position["qty"] * fill_price * (1 - config.fee_rate)
                        position = None
                    continue

                if int(signal_row["action"]) != 1 or execution_price <= 0:
                    continue

                qty = cash * (config.max_position_pct / 100) / execution_price
                fill_price = execution_price * (1 + config.slippage_bps / 10_000)
                cost = qty * fill_price * (1 + config.fee_rate)
                if qty * execution_price < config.min_notional or cost > cash:
                    continue

                cash -= cost
                position = {
                    "qty": qty,
                    "entry": fill_price,
                    "stop_loss": float(signal_row["stop_loss"]),
                    "take_profit": float(signal_row["take_profit"]),
                }

            final_close = float(out["close"].iloc[-1])
            final_equity = cash + (position["qty"] * final_close if position else 0.0)
            total_final_equity += final_equity
            return_pct = (
                (final_equity / capital_per_pair - 1) * 100 if capital_per_pair else 0.0
            )
            logger.info(
                "Backtest %s initial=%.2f final=%.2f return=%.2f%%",
                pair.pair_id,
                capital_per_pair,
                final_equity,
                return_pct,
            )
    finally:
        market_data.close()

    logger.info("Backtest total final equity=%.2f", total_final_equity)
