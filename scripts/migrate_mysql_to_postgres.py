"""Copy the legacy MySQL bot state into PostgreSQL.

Run this only while the bot and dashboard are stopped. The destination must be
empty; this makes reruns fail safely rather than merging an old state into a
live bot database.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pymysql
from dotenv import load_dotenv
from pymysql.cursors import DictCursor


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate bot state from MySQL to PostgreSQL")
    parser.add_argument(
        "--env-file", default=".env", help="Environment file containing source MYSQL_* and target POSTGRES_* settings"
    )
    args = parser.parse_args()

    load_dotenv(ROOT / args.env_file, override=False)
    required_mysql = ("MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE")
    missing = [name for name in required_mysql if not os.getenv(name)]
    if missing:
        raise SystemExit(f"Missing MySQL source settings: {', '.join(missing)}")

    config = Config.from_env()
    target = StateStore(config)  # Creates the PostgreSQL schema before copying.
    try:
        with target._conn.cursor() as cur:  # Intentional preflight against the target database.
            for table in ("positions", "trades", "meta", "equity_history", "orders"):
                cur.execute(f"SELECT COUNT(*) AS count FROM {table}")
                if cur.fetchone()["count"]:
                    raise SystemExit(
                        f"PostgreSQL {table} table is not empty; refusing to merge data"
                    )
        target._conn.commit()

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
            positions = fetch_all(source, "positions")
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
            cur.execute("SELECT setval(pg_get_serial_sequence('trades', 'id'), COALESCE((SELECT MAX(id) FROM trades), 1), true)")
            cur.execute("SELECT setval(pg_get_serial_sequence('equity_history', 'id'), COALESCE((SELECT MAX(id) FROM equity_history), 1), true)")

        print(
            f"Migrated {len(positions)} positions, {len(trades)} trades, "
            f"{len(meta)} metadata records, and {len(equity)} equity records."
        )
    finally:
        target.close()


if __name__ == "__main__":
    main()
