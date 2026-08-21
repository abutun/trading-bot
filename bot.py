import logging
import time
from datetime import datetime, timezone

from market_data import MarketData
from models import Position
from risk import RiskManager
from state import StateStore
from strategy import EMARSI

from brokers.binance_broker import BinanceBroker
from brokers.evm_broker import EVMBroker
from brokers.paper import PaperBroker


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
        if not venues:
            raise ValueError("No trading pairs configured")

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

            else:
                raise ValueError(f"Unknown venue: {venue}")

    def _total_equity(self, prices: dict[str, float]) -> float:
        total = 0.0

        for broker in self.brokers.values():
            try:
                total += broker.get_equity(prices)
            except Exception as exc:
                logger.exception("Failed to calculate equity for venue %s", broker.venue)

        return total

    def _close_position(self, pos: Position, price: float, reason: str) -> None:
        broker = self.brokers[pos.venue]

        result = broker.sell(pos.symbol, pos.qty, price)

        self.state.delete_position(pos.pair_id)
        self.state.add_trade(
            ts=datetime.now(timezone.utc).isoformat(),
            pair_id=pos.pair_id,
            venue=pos.venue,
            symbol=pos.symbol,
            side="sell",
            qty=result.qty,
            price=result.price,
            fee=result.fee,
            order_id=result.order_id,
        )

        logger.info(
            "Closed %s qty=%.8f price=%.8f reason=%s",
            pos.pair_id,
            result.qty,
            result.price,
            reason,
        )

    def step(self) -> None:
        prices: dict[str, float] = {}
        signals = []

        for pair in self.config.trading_pairs:
            try:
                df = self.market_data.fetch_ohlcv(pair.symbol)
            except Exception as exc:
                logger.warning("Failed to fetch %s: %s", pair.symbol, exc)
                continue

            if df.empty:
                continue

            price = float(df["close"].iloc[-1])
            prices[pair.symbol] = price

            signal = self.strategy.generate(df)
            signals.append((pair, signal, price))

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
                price = prices.get(pos.symbol, pos.entry_price)

                try:
                    self._close_position(pos, price, "risk_halt")
                except Exception as exc:
                    logger.exception("Failed to close %s during risk halt", pos.pair_id)

            return

        for pair, signal, price in signals:
            broker = self.brokers[pair.venue]
            pos = self.state.get_position(pair.pair_id)

            try:
                if pos is not None:
                    exit_reason = None

                    if signal.action == -1:
                        exit_reason = "signal"
                    elif self.risk.check_exit(pos, price):
                        exit_reason = "stop_take_profit"

                    if exit_reason:
                        self._close_position(pos, price, exit_reason)

                else:
                    if signal.action == 1 and self.risk.can_open(pair.pair_id, equity):
                        qty = self.risk.position_size(equity, price)

                        if qty * price < self.config.min_notional:
                            continue

                        result = broker.buy(pair.symbol, qty, price)

                        if result.qty <= 0:
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

                        self.state.upsert_position(new_pos)
                        self.state.add_trade(
                            ts=datetime.now(timezone.utc).isoformat(),
                            pair_id=pair.pair_id,
                            venue=pair.venue,
                            symbol=pair.symbol,
                            side="buy",
                            qty=result.qty,
                            price=result.price,
                            fee=result.fee,
                            order_id=result.order_id,
                        )

                        logger.info(
                            "Opened %s qty=%.8f price=%.8f",
                            pair.pair_id,
                            result.qty,
                            result.price,
                        )

            except Exception as exc:
                logger.exception("Error processing %s", pair.pair_id)

    def run(self) -> None:
        logger.info(
            "Starting bot mode=%s pairs=%s",
            self.config.mode,
            [pair.pair_id for pair in self.config.trading_pairs],
        )

        while True:
            start = time.time()

            try:
                self.step()
            except Exception as exc:
                logger.exception("Bot step failed")

            try:
                self.state.set_meta(
                    HEARTBEAT_KEY, datetime.now(timezone.utc).isoformat()
                )
            except Exception as exc:
                logger.warning("Failed to write heartbeat: %s", exc)

            elapsed = time.time() - start
            sleep_time = max(1, self.config.loop_seconds - elapsed)
            time.sleep(sleep_time)
