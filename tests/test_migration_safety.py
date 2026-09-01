from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from scripts._environment import replace_process_environment
from scripts.migrate_mysql_to_postgres import (
    assert_empty_locked_target,
    assert_no_unresolved_legacy_orders,
    assert_protected_legacy_positions,
)
from state import BotInstanceLockError


class _LegacyCursor:
    def __init__(self, connection):
        self.connection = connection
        self.sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, _params=()):
        self.sql = sql

    def fetchone(self):
        if self.sql.startswith("SHOW TABLES") and self.connection.has_orders:
            return {"table": "orders"}
        return None

    def fetchall(self):
        if self.sql.startswith("SHOW COLUMNS"):
            return [{"Field": column} for column in self.connection.order_columns]
        if self.sql.startswith("SELECT status"):
            return [{"status": status} for status in self.connection.statuses]
        return []


class _LegacyConnection:
    def __init__(self, *, has_orders=True, order_columns=("status",), statuses=()):
        self.has_orders = has_orders
        self.order_columns = order_columns
        self.statuses = statuses

    def cursor(self):
        return _LegacyCursor(self)


def test_selected_environment_replaces_ambient_database_settings():
    with TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "migration.env"
        path.write_text("POSTGRES_DATABASE=selected\nMYSQL_USER=selected-user\n")
        with patch.dict(
            os.environ,
            {
                "POSTGRES_DATABASE": "ambient-production",
                "POSTGRES_DSN": "postgresql://ambient-target",
                "MYSQL_USER": "ambient-user",
            },
            clear=True,
        ):
            replace_process_environment(path)
            assert os.environ["POSTGRES_DATABASE"] == "selected"
            assert os.environ["MYSQL_USER"] == "selected-user"
            assert "POSTGRES_DSN" not in os.environ


def test_legacy_unresolved_orders_refuse_migration():
    source = _LegacyConnection(statuses=("filled", "pending"))

    with pytest.raises(SystemExit, match="unresolved/non-terminal"):
        assert_no_unresolved_legacy_orders(source)


def test_legacy_terminal_orders_are_verified_but_not_migrated():
    source = _LegacyConnection(statuses=("filled", "cancelled", "rejected"))

    assert assert_no_unresolved_legacy_orders(source) == 3


def test_unprotected_legacy_position_refuses_migration():
    with pytest.raises(SystemExit, match="valid stop < entry < take-profit"):
        assert_protected_legacy_positions(
            [{"pair_id": "binance:BTC/USDT", "entry_price": 100, "stop_loss": 0}]
        )


class _TargetCursor:
    def __init__(self, target):
        self.target = target

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _sql):
        return None

    def fetchone(self):
        return {"count": 0}


class _TargetConnection:
    def __init__(self, target):
        self.target = target
        self.committed = False

    def cursor(self):
        return _TargetCursor(self.target)

    def commit(self):
        self.committed = True


class _Target:
    def __init__(self, lock_error=None):
        self.lock_error = lock_error
        self.locked = False
        self._conn = _TargetConnection(self)

    def acquire_bot_lock(self):
        if self.lock_error:
            raise self.lock_error
        self.locked = True


def test_migration_refuses_a_contended_bot_lock_before_target_preflight():
    target = _Target(lock_error=BotInstanceLockError("already locked"))

    with pytest.raises(BotInstanceLockError):
        assert_empty_locked_target(target)
    assert not target._conn.committed
