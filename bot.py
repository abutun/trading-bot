import logging
import time
from datetime import datetime, timezone

from market_data import MarketData
from models import Position
from risk import RiskManager
from state import StateStore, UnresolvedOrderError
from strategy import EMARSI

from brokers.binance_broker import BinanceBroker
from brokers.evm_broker import EVMBroker
from brokers.paper import PaperBroker
from brokers.polymarket_broker import PolymarketBroker


logger = logging.getLogger(__name__)

HEARTBEAT_KEY = "bot_heartbeat"


class TradingBot:
    def __init__(self, config):
        self.config = config
        self.state = StateStore(config)
        self.market_data = MarketData(config)
        self.strategy = EMARSI(config)
        self.risk = RiskManager(config, self.state)

        venues = sorted({pair.venue for pair in config.trading_pairs})
        self.brokers = {}
        initial_capital_per_venue = (
            config.paper_initial_capital / len(venues) if config.mode == "paper" else 0.0
        )

        for venue in venues:
            if config.mode == "paper":
                self.brokers[venue] = PaperBroker(
                    config=config,
                    state=self.state,
                    venue=venue,
                    initial_capital=initial_capital_per_venue,
                )
            elif venue == "binance":
                self.brokers[venue] = BinanceBroker(config=config, state=self.state)
            elif venue == "evm":
                self.brokers[venue] = EVMBroker(config=config, state=self.state)
            elif venue == "polymarket":
                self.brokers[venue] = PolymarketBroker(config=config, state=self.state)
            else:  # Config validates this before the bot is constructed.
                raise ValueError(f"Unknown venue: {venue}")

    def _total_equity(self, prices: dict[str, float]) -> float:
        total = 0.0
        for broker in self.brokers.values():
            try:
                total += broker.get_equity(prices)
            except Exception:
                logger.exception("Failed to calculate equity for venue %s", broker.venue)
        return total

    def _close_position(self, pos: Position, price: float, reason: str) -> None:
        if self.state.has_unresolved_order(pos.pair_id):
            raise UnresolvedOrderError(
                f"Cannot close {pos.pair_id}: its prior order requires reconciliation"
            )

        client_order_id = self.state.create_order_intent(
            pair_id=pos.pair_id,
            venue=pos.venue,
            symbol=pos.symbol,
            side="sell",
            qty=pos.qty,
            price=price,
        )

        try:
            result = self.brokers[pos.venue].sell(
                pos.symbol, pos.qty, price, client_order_id
            )
        except Exception as exc:
            # The request could have reached a venue before the client failed.
            # Preserve this state and never duplicate the order automatically.
            self.state.mark_order_unknown(client_order_id, str(exc))
            raise

        if result.qty <= 0:
            self.state.mark_order_rejected(client_order_id, "Venue returned a zero fill")
            raise RuntimeError(f"Venue returned a zero fill while closing {pos.pair_id}")

        remaining = self.state.complete_exit(
            client_order_id,
            pos,
            result,
            datetime.now(timezone.utc).isoformat(),
        )
        logger.info(
            "%s %s qty=%.8f price=%.8f reason=%s remaining=%.8f",
            "Closed" if remaining == 0 else "Partially closed",
            pos.pair_id,
            result.qty,
            result.price,
            reason,
            remaining,
        )

    def step(self) -> None:
        prices: dict[str, float] = {}
        signals = []

        for pair in self.config.trading_pairs:
            try:
                df = self.market_data.fetch_ohlcv(pair.symbol, venue=pair.venue)
            except Exception as exc:
                logger.warning("Failed to fetch %s: %s", pair.pair_id, exc)
                continue

            if df.empty:
                continue

            # Use the current price for risk management, but derive signals
            # from the most recently closed candle by default.
            price = float(df["close"].iloc[-1])
            prices[pair.pair_id] = price
            signal_df = df.iloc[:-1] if self.config.use_closed_candles and len(df) > 1 else df
            signals.append((pair, self.strategy.generate(signal_df), price))

        if not prices:
            return

        equity = self._total_equity(prices)
        self.risk.update(equity)
        try:
            self.state.record_equity(datetime.now(timezone.utc).isoformat(), equity)
        except Exception as exc:
            logger.warning("Failed to record equity: %s", exc)

        logger.info("Equity=%.2f halted=%s", equity, self.risk.halted())

        if self.risk.halted():
            for pos in list(self.state.get_all_positions()):
                price = prices.get(pos.pair_id, pos.entry_price)
                try:
                    self._close_position(pos, price, "risk_halt")
                except Exception:
                    logger.exception("Failed to close %s during risk halt", pos.pair_id)
            return

        for pair, signal, price in signals:
            pos = self.state.get_position(pair.pair_id)
            try:
                if self.state.has_unresolved_order(pair.pair_id):
                    logger.error(
                        "Skipping %s because an order is unresolved; reconcile the venue before resuming",
                        pair.pair_id,
                    )
                    continue

                if pos is not None:
                    exit_reason = None
                    if signal.action == -1:
                        exit_reason = "signal"
                    elif self.risk.check_exit(pos, price):
                        exit_reason = "stop_take_profit"
                    if exit_reason:
                        self._close_position(pos, price, exit_reason)
                    continue

                if signal.action != 1 or not self.risk.can_open(pair.pair_id, equity):
                    continue

                qty = self.risk.position_size(equity, price)
                if qty * price < self.config.min_notional:
                    continue

                client_order_id = self.state.create_order_intent(
                    pair_id=pair.pair_id,
                    venue=pair.venue,
                    symbol=pair.symbol,
                    side="buy",
                    qty=qty,
                    price=price,
                )
                try:
                    result = self.brokers[pair.venue].buy(
                        pair.symbol, qty, price, client_order_id
                    )
                except Exception as exc:
                    self.state.mark_order_unknown(client_order_id, str(exc))
                    raise

                if result.qty <= 0:
                    self.state.mark_order_rejected(
                        client_order_id, "Venue returned a zero fill"
                    )
                    continue

                new_pos = Position(
                    pair_id=pair.pair_id,
                    venue=pair.venue,
                    symbol=pair.symbol,
                    qty=result.qty,
                    entry_price=result.price,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit,
                )
                self.state.complete_entry(
                    client_order_id,
                    new_pos,
                    result,
                    datetime.now(timezone.utc).isoformat(),
                )
                logger.info(
                    "Opened %s qty=%.8f price=%.8f",
                    pair.pair_id,
                    result.qty,
                    result.price,
                )
            except Exception:
                logger.exception("Error processing %s", pair.pair_id)

    def run(self) -> None:
        logger.info(
            "Starting bot mode=%s pairs=%s",
            self.config.mode,
            [pair.pair_id for pair in self.config.trading_pairs],
        )
        try:
            while True:
                start = time.time()
                try:
                    self.step()
                except Exception:
                    logger.exception("Bot step failed")

                try:
                    self.state.set_meta(
                        HEARTBEAT_KEY, datetime.now(timezone.utc).isoformat()
                    )
                except Exception as exc:
                    logger.warning("Failed to write heartbeat: %s", exc)

                time.sleep(max(1, self.config.loop_seconds - (time.time() - start)))
        finally:
            self.market_data.close()
            for broker in self.brokers.values():
                close = getattr(broker, "close", None)
                if close:
                    close()
            self.state.close()
