import argparse
import logging

from backtest import run_backtest
from bot import TradingBot
from config import Config


def setup_logging(config: Config) -> None:
    handlers = [logging.StreamHandler()]

    if config.log_file:
        handlers.append(logging.FileHandler(config.log_file))

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Binance, EVM/MetaMask, and Polymarket trading bot"
    )
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Run a simple backtest instead of live/paper trading",
    )

    args = parser.parse_args()

    config = Config.from_env()
    setup_logging(config)

    if args.backtest:
        run_backtest(config)
    else:
        bot = TradingBot(config)
        bot.run()


if __name__ == "__main__":
    main()
