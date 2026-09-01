"""Copy the legacy MySQL bot state into PostgreSQL.

Run this only while the bot and dashboard are stopped. The destination must be
empty; this makes reruns fail safely rather than merging an old state into a
live bot database. The selected environment file replaces (rather than merges
with) the process environment, so an ambient shell cannot redirect a copy.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pymysql
from pymysql.cursors import DictCursor


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from scripts._environment import replace_process_environment  # noqa: E402
from state import StateStore  # noqa: E402


def as_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def fetch_all(conn: Any, table: str) -> Iterable[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM `{table}`")
        return list(cur.fetchall())


TERMINAL_LEGACY_ORDER_STATUSES = {
    "filled",
    "rejected",
    "cancelled",
    "canceled",
    "expired",
    "failed",
    "closed",
    "complete",
    "completed",
}
TARGET_STATE_TABLES = ("positions", "trades", "meta", "equity_history", "orders")


def source_has_table(conn: Any, table: str) -> bool:
    """Return whether an expected legacy table exists without interpolating input."""
    with conn.cursor() as cur:
        cur.execute("SHOW TABLES LIKE %s", (table,))
        return cur.fetchone() is not None


def assert_no_unresolved_legacy_orders(conn: Any) -> int:
    """Refuse a migration that would drop an in-flight legacy order intent.

    Older installations did not always have an ``orders`` table. Terminal
    historic records are not copied because they do not affect restart safety;
    any unknown/non-terminal record requires manual venue reconciliation first.
    """
    if not source_has_table(conn, "orders"):
        return 0
    with conn.cursor() as cur:
        cur.execute("SHOW COLUMNS FROM `orders`")
        columns = {str(row["Field"]).lower() for row in cur.fetchall()}
        if "status" not in columns:
            raise SystemExit(
                "Legacy orders table has no status column; manually reconcile it "
                "before PostgreSQL migration"
            )
        cur.execute("SELECT status FROM `orders`")
        statuses = [row.get("status") for row in cur.fetchall()]
    unresolved = [
        str(status)
        for status in statuses
        if status is None or str(status).strip().lower() not in TERMINAL_LEGACY_ORDER_STATUSES
    ]
    if unresolved:
        examples = ", ".join(repr(value) for value in unresolved[:5])
        raise SystemExit(
            "Legacy orders contain unresolved/non-terminal statuses "
            f"({examples}); reconcile the venue before migration"
        )
    return len(statuses)


def assert_protected_legacy_positions(positions: Iterable[dict[str, Any]]) -> None:
    """Reject carried positions without a valid durable stop and take-profit."""
    for row in positions:
        pair_id = str(row.get("pair_id", "<unknown>"))
        try:
            entry = float(row["entry_price"])
            stop = float(row.get("stop_loss", 0))
            target = float(row.get("take_profit", 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemExit(
                f"Legacy position {pair_id!r} has invalid protective price fields; "
                "manually reconcile before migration"
            ) from exc
        if (
            not all(math.isfinite(value) for value in (entry, stop, target))
            or entry <= 0
            or stop <= 0
            or stop >= entry
            or target <= entry
        ):
            raise SystemExit(
                f"Legacy position {pair_id!r} lacks a valid stop < entry < take-profit; "
                "manually reconcile before migration"
            )


def assert_empty_locked_target(target: StateStore) -> None:
    """Take the bot leader lock, then reject any non-empty destination table."""
    # This is the same database-scoped leader lock that the bot holds. It
    # prevents an auto-restarted worker from trading while state is copied.
    target.acquire_bot_lock()
    with target._conn.cursor() as cur:  # Intentional preflight against the target database.
        for table in TARGET_STATE_TABLES:
            cur.execute(f"SELECT COUNT(*) AS count FROM {table}")
            if cur.fetchone()["count"]:
                raise SystemExit(
                    f"PostgreSQL {table} table is not empty; refusing to merge data"
                )
    target._conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate bot state from MySQL to PostgreSQL")
    parser.add_argument(
        "--env-file", default=".env", help="Environment file containing source MYSQL_* and target POSTGRES_* settings"
    )
    args = parser.parse_args()

    env_path = Path(args.env_file)
    if not env_path.is_absolute():
        env_path = ROOT / env_path
    replace_process_environment(env_path)
    required_mysql = ("MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE")
    missing = [name for name in required_mysql if not os.getenv(name)]
    if missing:
        raise SystemExit(f"Missing MySQL source settings: {', '.join(missing)}")

    # The explicitly selected migration file is the only configuration source.
    # Do not merge a developer's ambient .env into a production copy job.
    config = Config.from_env(load_dotenv_file=False, runtime_role="bot")
    target = StateStore(config)  # Creates the PostgreSQL schema before copying.
    try:
        assert_empty_locked_target(target)

        source = pymysql.connect(
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=int(os.getenv("MYSQL_PORT", "3306")),
            user=os.environ["MYSQL_USER"],
            password=os.environ["MYSQL_PASSWORD"],
            database=os.environ["MYSQL_DATABASE"],
            charset="utf8mb4",
            cursorclass=DictCursor,
        )
        try:
            legacy_terminal_orders = assert_no_unresolved_legacy_orders(source)
            positions = fetch_all(source, "positions")
            assert_protected_legacy_positions(positions)
            trades = fetch_all(source, "trades")
            meta = fetch_all(source, "meta")
            equity = fetch_all(source, "equity_history")
        finally:
            source.close()

        with target._transaction() as cur:
            for row in positions:
                cur.execute(
                    """
                    INSERT INTO positions (pair_id, venue, symbol, qty, entry_price, stop_loss, take_profit, opened_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        row["pair_id"], row["venue"], row["symbol"], row["qty"], row["entry_price"],
                        row.get("stop_loss", 0), row.get("take_profit", 0), as_timestamp(row["opened_at"]),
                    ),
                )
            for row in trades:
                cur.execute(
                    """
                    INSERT INTO trades (id, ts, pair_id, venue, symbol, side, qty, price, fee, order_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        row["id"], as_timestamp(row["ts"]), row["pair_id"], row["venue"], row["symbol"],
                        row["side"], row["qty"], row["price"], row.get("fee", 0), row.get("order_id"),
                    ),
                )
            for row in meta:
                cur.execute(
                    'INSERT INTO meta ("key", value) VALUES (%s, %s)',
                    (row["key"], row.get("value")),
                )
            for row in equity:
                cur.execute(
                    "INSERT INTO equity_history (id, ts, equity) VALUES (%s, %s, %s)",
                    (row["id"], as_timestamp(row["ts"]), row["equity"]),
                )
            cur.execute(
                "SELECT setval(pg_get_serial_sequence('trades', 'id'), "
                "COALESCE((SELECT MAX(id) FROM trades), 1), "
                "EXISTS (SELECT 1 FROM trades))"
            )
            cur.execute(
                "SELECT setval(pg_get_serial_sequence('equity_history', 'id'), "
                "COALESCE((SELECT MAX(id) FROM equity_history), 1), "
                "EXISTS (SELECT 1 FROM equity_history))"
            )

        print(
            f"Migrated {len(positions)} positions, {len(trades)} trades, "
            f"{len(meta)} metadata records, and {len(equity)} equity records. "
            f"Verified {legacy_terminal_orders} terminal legacy orders; no order "
            "intents were copied."
        )
    finally:
        target.close()


if __name__ == "__main__":
    main()
