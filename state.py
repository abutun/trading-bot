from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import pymysql
from pymysql.cursors import DictCursor

from models import Position


logger = logging.getLogger(__name__)


class StateStore:
    """MySQL-backed state store.

    Stores positions, trades, equity history and risk metadata in MySQL so
    the bot runs on a real database (and can be monitored by the dashboard).
    Tables are created automatically on first run.
    """

    def __init__(self, config):
        self._conn = pymysql.connect(
            host=config.mysql_host,
            port=config.mysql_port,
            user=config.mysql_user,
            password=config.mysql_password,
            database=config.mysql_database,
            charset="utf8mb4",
            autocommit=False,
            cursorclass=DictCursor,
        )

        self._init_schema()
        logger.info(
            "StateStore connected to MySQL %s:%s/%s",
            config.mysql_host,
            config.mysql_port,
            config.mysql_database,
        )

    def _init_schema(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS positions (
                pair_id VARCHAR(128) PRIMARY KEY,
                venue VARCHAR(32) NOT NULL,
                symbol VARCHAR(64) NOT NULL,
                qty DOUBLE NOT NULL,
                entry_price DOUBLE NOT NULL,
                stop_loss DOUBLE DEFAULT 0,
                take_profit DOUBLE DEFAULT 0,
                opened_at VARCHAR(64) NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS trades (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                ts VARCHAR(64) NOT NULL,
                pair_id VARCHAR(128) NOT NULL,
                venue VARCHAR(32) NOT NULL,
                symbol VARCHAR(64) NOT NULL,
                side VARCHAR(8) NOT NULL,
                qty DOUBLE NOT NULL,
                price DOUBLE NOT NULL,
                fee DOUBLE DEFAULT 0,
                order_id VARCHAR(128),
                INDEX idx_trades_ts (ts)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS meta (
                `key` VARCHAR(128) PRIMARY KEY,
                value TEXT
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS equity_history (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                ts VARCHAR(64) NOT NULL,
                equity DOUBLE NOT NULL,
                INDEX idx_equity_ts (ts)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
        ]

        with self._conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)

        self._conn.commit()

    def _execute(self, sql: str, params: tuple = ()) -> int:
        with self._conn.cursor() as cur:
            affected = cur.execute(sql, params)

        self._conn.commit()
        return affected

    def _query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def _row_to_position(self, row: Dict[str, Any]) -> Position:
        return Position(
            pair_id=row["pair_id"],
            venue=row["venue"],
            symbol=row["symbol"],
            qty=float(row["qty"]),
            entry_price=float(row["entry_price"]),
            stop_loss=float(row["stop_loss"] or 0.0),
            take_profit=float(row["take_profit"] or 0.0),
            opened_at=row["opened_at"],
        )

    # --- Positions -------------------------------------------------------

    def get_position(self, pair_id: str) -> Optional[Position]:
        rows = self._query("SELECT * FROM positions WHERE pair_id = %s", (pair_id,))
        return self._row_to_position(rows[0]) if rows else None

    def upsert_position(self, pos: Position) -> None:
        self._execute(
            """
            INSERT INTO positions (
                pair_id, venue, symbol, qty, entry_price, stop_loss, take_profit, opened_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                venue = VALUES(venue),
                symbol = VALUES(symbol),
                qty = VALUES(qty),
                entry_price = VALUES(entry_price),
                stop_loss = VALUES(stop_loss),
                take_profit = VALUES(take_profit),
                opened_at = VALUES(opened_at)
            """,
            (
                pos.pair_id,
                pos.venue,
                pos.symbol,
                pos.qty,
                pos.entry_price,
                pos.stop_loss,
                pos.take_profit,
                pos.opened_at,
            ),
        )

    def delete_position(self, pair_id: str) -> None:
        self._execute("DELETE FROM positions WHERE pair_id = %s", (pair_id,))

    def get_positions_by_venue(self, venue: str) -> List[Position]:
        rows = self._query("SELECT * FROM positions WHERE venue = %s", (venue,))
        return [self._row_to_position(row) for row in rows]

    def get_all_positions(self) -> List[Position]:
        rows = self._query("SELECT * FROM positions")
        return [self._row_to_position(row) for row in rows]

    # --- Trades -----------------------------------------------------------

    def add_trade(
        self,
        ts: str,
        pair_id: str,
        venue: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        fee: float,
        order_id: str,
    ) -> None:
        self._execute(
            """
            INSERT INTO trades (ts, pair_id, venue, symbol, side, qty, price, fee, order_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (ts, pair_id, venue, symbol, side, qty, price, fee, order_id),
        )

    def get_recent_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._query(
            "SELECT * FROM trades ORDER BY id DESC LIMIT %s", (limit,)
        )

    # --- Equity history -----------------------------------------------------

    def record_equity(self, ts: str, equity: float) -> None:
        self._execute(
            "INSERT INTO equity_history (ts, equity) VALUES (%s, %s)", (ts, equity)
        )

    def get_equity_history(self, limit: int = 2000) -> List[Dict[str, Any]]:
        rows = self._query(
            "SELECT ts, equity FROM equity_history ORDER BY id DESC LIMIT %s", (limit,)
        )
        rows.reverse()
        return rows

    # --- Meta -----------------------------------------------------------------

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        rows = self._query("SELECT value FROM meta WHERE `key` = %s", (key,))
        return rows[0]["value"] if rows else default

    def set_meta(self, key: str, value: str) -> None:
        self._execute(
            """
            INSERT INTO meta (`key`, value) VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE value = VALUES(value)
            """,
            (key, value),
        )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pragma: no cover - best effort on shutdown
            pass
