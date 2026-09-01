"""Strict environment-file loading for sensitive operator scripts."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values


def replace_process_environment(env_path: Path) -> dict[str, str]:
    """Make an explicitly selected dotenv file the process's only config source.

    Migration and recovery commands must never silently inherit an ambient
    `POSTGRES_DSN` or `MYSQL_*` value from a shell, Compose session, or CI
    runner.  These short-lived CLI processes do not need the inherited
    environment after Python has started, so replacing it is safer than a
    precedence convention.
    """
    resolved = env_path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Environment file does not exist: {resolved}")
    values = dotenv_values(resolved)
    selected = {
        str(key): str(value)
        for key, value in values.items()
        if key is not None and value is not None
    }
    os.environ.clear()
    os.environ.update(selected)
    return selected
