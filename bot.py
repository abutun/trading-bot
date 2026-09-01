from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any

from brokers.base import BrokerOrderRejectedError, BrokerOrderUncertainError
from brokers.binance_broker import BinanceBroker
from brokers.evm_broker import EVMBroker
from brokers.paper import PaperBroker
from brokers.polymarket_broker import PolymarketBroker
from market_data import MarketData
from models import ExecutionResult, Position, Signal
from risk import RiskManager
from state import BotInstanceLockError, StateStore, UnresolvedOrderError
from strategy import EMARSI


logger = logging.getLogger(__name__)

HEARTBEAT_KEY = "bot_heartbeat"
LAST_ORDER_PREFIX = "last_order_at:"


class TradingBot:
    """Durable, fail-closed trading coordinator.

    External orders are always preceded by a PostgreSQL intent.  Any state,
    data, or venue ambiguity becomes a durable safety halt; the process never
    retries a potentially accepted order on its own.
    """

    def __init__(
        self,
        config,
        *,
        state: StateStore | None = None,
        market_data: MarketData | None = None,
        strategy: EMARSI | None = None,
        brokers: dict[str, Any] | None = None,
    ):
        self.config = config
        self.state = state or StateStore(config)
        self.market_data = market_data or MarketData(config)
        self.strategy = strategy or EMARSI(config)
        self.risk = RiskManager(config, self.state)
        self._stop_event = threading.Event()
        self._closed = False

        if brokers is not None:
            self.brokers = brokers
            return

        venues = sorted({pair.venue for pair in config.trading_pairs})
        self.brokers: dict[str, Any] = {}
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

    def request_stop(self) -> None:
        """Ask the run loop to finish its current operation and shut down."""
        self._stop_event.set()

    def _total_equity(self, prices: dict[str, float]) -> float:
        total = 0.0
        for venue, broker in self.brokers.items():
            value = float(broker.get_equity(prices))
            if not math.isfinite(value) or value < 0:
                raise RuntimeError(f"{venue} broker returned invalid equity {value!r}")
            total += value
        if not math.isfinite(total) or total <= 0:
            raise RuntimeError("Total equity must be finite and positive")
        return total

    def _order_cooldown_active(self, pair_id: str, now: datetime) -> bool:
        if self.config.order_cooldown_seconds <= 0:
            return False
        raw = self.state.get_meta(f"{LAST_ORDER_PREFIX}{pair_id}")
        if not raw:
            return False
        try:
            previous = datetime.fromisoformat(raw)
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            # A corrupt cooldown must not cause an implicit rapid retry.
            self.risk.halt_safely(f"invalid_order_cooldown:{pair_id}")
            return True
        return (now - previous).total_seconds() < self.config.order_cooldown_seconds

    def _create_order_intent(
        self, pair, side: str, qty: float, price: float
    ) -> str | None:
        now = datetime.now(timezone.utc)
        if self._order_cooldown_active(pair.pair_id, now):
            logger.info("Skipping %s: order cooldown is active", pair.pair_id)
            return None
        # Write this before the intent. A database failure therefore prevents
        # submission; a rare intent conflict safely creates only a cooldown.
        self.state.set_meta(f"{LAST_ORDER_PREFIX}{pair.pair_id}", now.isoformat())
        return self.state.create_order_intent(
            pair_id=pair.pair_id,
            venue=pair.venue,
            symbol=pair.symbol,
            side=side,
            qty=qty,
            price=price,
        )

    def _mark_unknown(
        self, client_order_id: str, error: Exception | str, external_order_id: str = ""
    ) -> None:
        if external_order_id:
            try:
                self.state.mark_order_submitted(client_order_id, external_order_id)
            except Exception:
                # The intent itself remains pending if PostgreSQL cannot record
                # the ID. It still blocks automated retries after recovery.
                logger.exception(
                    "Failed to persist external ID for uncertain order %s", client_order_id
                )
        self.state.mark_order_unknown(client_order_id, str(error))
        self.risk.halt_safely(f"uncertain_order:{client_order_id}:{str(error)[:250]}")

    def _submit_order(
        self, pair, side: str, qty: float, price: float
    ) -> tuple[str, ExecutionResult, bool] | None:
        """Submit once and return ``(intent_id, result, slippage_ok)``.

        A normal venue rejection resolves the intent as rejected. Everything
        else that happens after an intent is created is considered ambiguous
        unless the broker explicitly proves no external order could fill.
        """
        client_order_id = self._create_order_intent(pair, side, qty, price)
        if client_order_id is None:
            return None
        action = self.brokers[pair.venue].buy if side == "buy" else self.brokers[pair.venue].sell
        try:
            result = action(pair.symbol, qty, price, client_order_id)
        except BrokerOrderRejectedError as exc:
            self.state.mark_order_rejected(client_order_id, str(exc))
            logger.warning("Order rejected pair=%s side=%s: %s", pair.pair_id, side, exc)
            return None
        except BrokerOrderUncertainError as exc:
            self._mark_unknown(
                client_order_id,
                exc,
                getattr(exc, "external_order_id", "") or getattr(exc, "order_id", ""),
            )
            raise
        except Exception as exc:
            self._mark_unknown(client_order_id, exc)
            raise

        if not isinstance(result, ExecutionResult):
            error = RuntimeError("Broker returned an invalid execution result type")
            self._mark_unknown(client_order_id, error)
            raise error
        if (
            not math.isfinite(result.qty)
            or not math.isfinite(result.price)
            or not math.isfinite(result.fee)
            or result.qty < 0
            or result.price < 0
            or result.fee < 0
        ):
            error = RuntimeError("Broker returned malformed fill values")
            self._mark_unknown(client_order_id, error, result.order_id)
            raise error
        if result.qty <= 0:
            self.state.mark_order_rejected(client_order_id, "Venue returned a zero fill")
            return None
        if result.price <= 0:
            error = RuntimeError("Venue returned a positive fill without a valid price")
            self._mark_unknown(client_order_id, error, result.order_id)
            raise error
        if self.config.mode == "live" and not result.order_id:
            error = RuntimeError("Live venue returned a fill without an external order ID")
            self._mark_unknown(client_order_id, error)
            raise error
        if result.order_id:
            self.state.mark_order_submitted(client_order_id, result.order_id)
        slippage_ok = self.risk.fill_within_slippage(side, price, result.price)
        return client_order_id, result, slippage_ok

    @staticmethod
    def _entry_stops(signal: Signal, reference_price: float, fill_price: float) -> tuple[float, float]:
        """Carry ATR distances forward to the actual, not requested, fill."""
        stop_distance = max(0.0, reference_price - signal.stop_loss)
        take_profit_distance = max(0.0, signal.take_profit - reference_price)
        stop_loss = fill_price - stop_distance
        take_profit = fill_price + take_profit_distance
        if stop_loss <= 0 or take_profit <= fill_price:
            raise RuntimeError("Strategy supplied invalid protective stops for executed entry")
        return stop_loss, take_profit

    def _close_position(self, pos: Position, price: float, reason: str) -> bool:
        if self.state.has_unresolved_order(pos.pair_id):
            raise UnresolvedOrderError(
                f"Cannot close {pos.pair_id}: its prior order requires reconciliation"
            )
        pair = next(pair for pair in self.config.trading_pairs if pair.pair_id == pos.pair_id)
        submitted = self._submit_order(pair, "sell", pos.qty, price)
        if submitted is None:
            return False
        client_order_id, result, slippage_ok = submitted
        try:
            remaining = self.state.complete_exit(
                client_order_id,
                pos,
                result,
                datetime.now(timezone.utc).isoformat(),
            )
        except Exception as exc:
            self._mark_unknown(client_order_id, exc, result.order_id)
            raise
        if not slippage_ok:
            self.risk.halt_safely(
                f"exit_fill_outside_slippage:{pos.pair_id}:{client_order_id}"
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
        return True

    def _open_position(self, pair, signal: Signal, qty: float, price: float) -> bool:
        submitted = self._submit_order(pair, "buy", qty, price)
        if submitted is None:
            return False
        client_order_id, result, slippage_ok = submitted
        try:
            stop_loss, take_profit = self._entry_stops(signal, price, result.price)
            new_pos = Position(
                pair_id=pair.pair_id,
                venue=pair.venue,
                symbol=pair.symbol,
                qty=result.qty,
                entry_price=result.price,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
            self.state.complete_entry(
                client_order_id,
                new_pos,
                result,
                datetime.now(timezone.utc).isoformat(),
            )
        except Exception as exc:
            self._mark_unknown(client_order_id, exc, result.order_id)
            raise
        if not slippage_ok:
            self.risk.halt_safely(
                f"entry_fill_outside_slippage:{pair.pair_id}:{client_order_id}"
            )
        logger.info(
            "Opened %s qty=%.8f price=%.8f order_id=%s",
            pair.pair_id,
            result.qty,
            result.price,
            result.order_id or "paper",
        )
        return True

    def _collect_market_state(
        self,
    ) -> tuple[dict[str, float], list[tuple[Any, Signal, float]]]:
        prices: dict[str, float] = {}
        signals: list[tuple[Any, Signal, float]] = []
        for pair in self.config.trading_pairs:
            frame = self.market_data.fetch_ohlcv(pair.symbol, venue=pair.venue)
            timestamp = self.market_data.validate_for_trading(frame, pair.venue)
            price = float(frame["close"].iloc[-1])
            if not math.isfinite(price) or price <= 0:
                raise RuntimeError(f"{pair.pair_id} has no valid closing price")
            self.state.set_latest_price(pair.pair_id, price, timestamp, pair.venue)
            signal_frame = (
                frame.iloc[:-1]
                if self.config.use_closed_candles and len(frame) > 1
                else frame
            )
            signals.append((pair, self.strategy.generate(signal_frame), price))
            prices[pair.pair_id] = price
        if not prices:
            raise RuntimeError("No prices were collected for this trading cycle")
        return prices, signals

    def _record_cycle_failure(self, context: str, exc: Exception) -> None:
        logger.error("Trading cycle failed during %s: %s", context, exc, exc_info=True)
        try:
            self.risk.record_cycle_failure(f"{context}:{type(exc).__name__}:{exc}")
        except Exception:
            logger.exception("Could not persist trading-cycle failure state")

    def _ensure_managed_positions(self) -> None:
        configured_pairs = {pair.pair_id for pair in self.config.trading_pairs}
        positions = self.state.get_all_positions()
        unmanaged = [position.pair_id for position in positions if position.pair_id not in configured_pairs]
        if unmanaged:
            reason = f"unconfigured_tracked_positions:{','.join(unmanaged)}"
            self.risk.halt_safely(reason)
            raise RuntimeError(
                "Tracked positions no longer appear in TRADING_PAIRS; restore their "
                "configuration and reconcile before continuing"
            )
        unprotected = [
            position.pair_id
            for position in positions
            if (
                not all(
                    math.isfinite(value)
                    for value in (
                        position.qty,
                        position.entry_price,
                        position.stop_loss,
                        position.take_profit,
                    )
                )
                or position.qty <= 0
                or position.entry_price <= 0
                or position.stop_loss <= 0
                or position.stop_loss >= position.entry_price
                or position.take_profit <= position.entry_price
            )
        ]
        if unprotected:
            reason = f"unprotected_tracked_positions:{','.join(unprotected)}"
            self.risk.halt_safely(reason)
            raise RuntimeError(
                "Tracked positions must have finite 0 < stop_loss < entry_price < "
                "take_profit values; reconcile before continuing"
            )

    def preflight(self, *, acquire_lock: bool = False, verify_market_data: bool = True) -> None:
        """Verify required durable state and venue/data paths without trading."""
        if acquire_lock:
            self.state.acquire_bot_lock()
        if not self.state.ping():
            raise RuntimeError("PostgreSQL preflight failed")
        if self.state.has_any_unresolved_order():
            self.risk.halt_safely("unresolved_order_requires_manual_reconciliation")
            raise UnresolvedOrderError(
                "Unresolved order exists. Reconcile the venue and clear the safety halt explicitly."
            )
        self._ensure_managed_positions()
        if verify_market_data:
            prices, _ = self._collect_market_state()
            equity = self._total_equity(prices)
            if not math.isfinite(equity) or equity <= 0:
                raise RuntimeError("Preflight calculated invalid equity")

    def clear_safety_halt(self, reason: str) -> None:
        """Perform an explicit operator acknowledgement after reconciliation."""
        if not reason or len(reason.strip()) < 8:
            raise ValueError("Provide an operator reason of at least 8 characters")
        if self.state.has_any_unresolved_order():
            raise UnresolvedOrderError(
                "Cannot clear the safety halt while an order remains unresolved"
            )
        self.risk.clear_safety_halt(reason.strip())

    def step(self) -> bool:
        """Run one cycle. Returns true only when it completed normally."""
        try:
            if self.state.has_any_unresolved_order():
                self.risk.halt_safely("unresolved_order_requires_manual_reconciliation")
                logger.error("Trading paused: unresolved order requires reconciliation")
                return False
            self._ensure_managed_positions()
            prices, signals = self._collect_market_state()
            equity = self._total_equity(prices)
            self.risk.update(equity)
            self.state.record_equity(datetime.now(timezone.utc).isoformat(), equity)
        except Exception as exc:
            self._record_cycle_failure("data_or_equity", exc)
            return False

        logger.info("Equity=%.2f halted=%s", equity, self.risk.halted())
        if self.risk.liquidation_required():
            for pos in list(self.state.get_all_positions()):
                # A prior exit could have produced an unsafe/ambiguous result.
                # Do not use a daily/total-loss halt as permission to keep
                # sending orders once it has turned into a safety halt.
                if not self.risk.liquidation_required():
                    return False
                price = prices.get(pos.pair_id)
                if price is None:
                    self.risk.halt_safely(f"missing_liquidation_price:{pos.pair_id}")
                    return False
                try:
                    closed = self._close_position(pos, price, "risk_limit")
                except Exception as exc:
                    self._record_cycle_failure(f"liquidation:{pos.pair_id}", exc)
                    return False
                if not closed:
                    self.risk.halt_safely(f"risk_limit_exit_rejected:{pos.pair_id}")
                    return False
                if not self.risk.liquidation_required():
                    return False
            return True
        if self.risk.halted():
            logger.warning("Safety halt is active; no new orders will be submitted")
            return False

        current_exposure = self.risk.current_exposure(prices)
        if current_exposure is None:
            self.risk.halt_safely("could_not_value_existing_position")
            return False
        for pair, signal, price in signals:
            try:
                if self.risk.halted():
                    return False
                pos = self.state.get_position(pair.pair_id)
                if pos is not None:
                    exit_reason = None
                    if signal.action == -1:
                        exit_reason = "signal"
                    elif self.risk.check_exit(pos, price):
                        exit_reason = "stop_take_profit"
                    if exit_reason:
                        closed = self._close_position(pos, price, exit_reason)
                        if not closed:
                            self.risk.halt_safely(
                                f"exit_rejected:{pair.pair_id}:{exit_reason}"
                            )
                            return False
                        if self.risk.halted():
                            return False
                    continue

                if signal.action != 1 or not self.risk.can_open(pair.pair_id, equity, prices):
                    continue
                qty = self.risk.position_size(equity, price, current_exposure)
                if qty <= 0 or qty * price < self.config.min_notional:
                    continue
                if self._open_position(pair, signal, qty, price):
                    current_exposure = self.risk.current_exposure(prices)
                    if current_exposure is None:
                        self.risk.halt_safely("could_not_value_new_position")
                        return False
                if self.risk.halted():
                    return False
            except Exception as exc:
                self._record_cycle_failure(f"execution:{pair.pair_id}", exc)
                return False

        self.risk.record_cycle_success()
        return True

    def write_heartbeat(self) -> None:
        self.state.set_meta(HEARTBEAT_KEY, datetime.now(timezone.utc).isoformat())

    def run(self) -> None:
        logger.info(
            "Starting bot mode=%s pairs=%s",
            self.config.mode,
            [pair.pair_id for pair in self.config.trading_pairs],
        )
        try:
            self.preflight(acquire_lock=True)
            while not self._stop_event.is_set():
                started = time.monotonic()
                self.step()
                try:
                    self.write_heartbeat()
                except Exception:
                    logger.exception("Failed to write bot heartbeat")
                remaining = max(1.0, self.config.loop_seconds - (time.monotonic() - started))
                self._stop_event.wait(remaining)
        except BotInstanceLockError:
            logger.error("Refusing to start: another bot instance owns the database lock")
            raise
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.request_stop()
        try:
            self.market_data.close()
        except Exception:
            logger.exception("Failed to close market-data client")
        for broker in self.brokers.values():
            close = getattr(broker, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.exception("Failed to close %s broker", getattr(broker, "venue", "unknown"))
        self.state.close()
