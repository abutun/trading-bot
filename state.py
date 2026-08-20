from __future__ import annotations

import sqlite3

from models import Position


class StateStore:
    def __init__(self, db_path: str = "state.db"):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row

        cur = self.conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                pair_id TEXT PRIMARY KEY,
                venue TEXT NOT NULL,
                symbol TEXT NOT NULL,
                qty REAL NOT NULL,
                entry_price REAL NOT NULL,
                stop_loss REAL DEFAULT 0,
                take_profit REAL DEFAULT 0,
                opened_at TEXT NOT NULL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                pair_id TEXT NOT NULL,
                venue TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                price REAL NOT NULL,
                fee REAL DEFAULT 0,
                order_id TEXT
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )

        self.conn.commit()

    def _row_to_position(self, row: sqlite3.Row) -> Position:
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

    def get_position(self, pair_id: str) -> Position | None:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE pair_id = ?", (pair_id,)
        ).fetchone()

        if row is None:
            return None

        return self._row_to_position(row)

    def upsert_position(self, pos: Position) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO positions (
                pair_id, venue, symbol, qty, entry_price, stop_loss, take_profit, opened_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
        self.conn.commit()

    def delete_position(self, pair_id: str) -> None:
        self.conn.execute("DELETE FROM positions WHERE pair_id = ?", (pair_id,))
        self.conn.commit()

    def get_positions_by_venue(self, venue: str) -> list[Position]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE venue = ?", (venue,)
        ).fetchall()

        return [self._row_to_position(row) for row in rows]

    def get_all_positions(self) -> list[Position]:
        rows = self.conn.execute("SELECT * FROM positions").fetchall()
        return [self._row_to_position(row) for row in rows]

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
        self.conn.execute(
            """
            INSERT INTO trades (
                ts, pair_id, venue, symbol, side, qty, price, fee, order_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, pair_id, venue, symbol, side, qty, price, fee, order_id),
        )
        self.conn.commit()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()

        if row is None:
            return default

        return row["value"]

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
