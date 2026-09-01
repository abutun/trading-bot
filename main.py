from __future__ import annotations

import argparse
import json
import logging
import re
import signal
from datetime import datetime, timezone

from backtest import run_backtest
from bot import TradingBot
from config import Config


class JsonFormatter(logging.Formatter):
    """Structured stdout logs with conservative redaction for operations."""

    _secret_assignment = re.compile(
        r"(?i)\b(api[_-]?(?:key|secret|passphrase)|password|private[_-]?key|authorization)"
        r"([=:]\s*)([^\s,;]+)"
    )
    _private_key = re.compile(r"\b0x[a-fA-F0-9]{64}\b")

    @classmethod
    def _redact(cls, value: str) -> str:
        value = cls._secret_assignment.sub(r"\1\2[REDACTED]", value)
        return cls._private_key.sub("[REDACTED_PRIVATE_KEY]", value)

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": self._redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exception"] = self._redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(config: Config) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if config.log_file:
        handlers.append(logging.FileHandler(config.log_file))
    formatter: logging.Formatter
    if config.json_logs:
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        handlers=handlers,
        force=True,
    )


def _install_signal_handlers(bot: TradingBot) -> None:
    def request_shutdown(signum, _frame) -> None:
        logging.getLogger(__name__).info(
            "Received signal %s; stopping after the active operation", signum
        )
        bot.request_stop()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, request_shutdown)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Production-safe Binance, EVM/MetaMask, and Polymarket trading bot"
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--backtest",
        action="store_true",
        help="Run an isolated historical backtest instead of the trading loop",
    )
    modes.add_argument(
        "--preflight",
        action="store_true",
        help="Verify database, account equity, and fresh market data without placing an order",
    )
    modes.add_argument(
        "--once",
        action="store_true",
        help="Run one guarded trading cycle, then exit",
    )
    modes.add_argument(
        "--clear-safety-halt",
        metavar="REASON",
        help="Clear a reconciled safety halt with a durable operator reason",
    )
    args = parser.parse_args()

    config = Config.from_env(runtime_role="bot")
    setup_logging(config)

    if args.backtest:
        run_backtest(config)
        return 0

    bot = TradingBot(config)
    try:
        if args.preflight:
            bot.preflight(acquire_lock=True)
            logging.getLogger(__name__).info("Preflight passed; no order was submitted")
            return 0
        if args.clear_safety_halt is not None:
            bot.preflight(acquire_lock=True, verify_market_data=False)
            bot.clear_safety_halt(args.clear_safety_halt)
            logging.getLogger(__name__).warning("Safety halt cleared by explicit operator action")
            return 0
        if args.once:
            bot.preflight(acquire_lock=True)
            passed = bot.step()
            bot.write_heartbeat()
            return 0 if passed else 1

        _install_signal_handlers(bot)
        bot.run()
        return 0
    finally:
        bot.close()


if __name__ == "__main__":
    raise SystemExit(main())
