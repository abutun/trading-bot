"""Print durable unresolved order intents without contacting a trading venue."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from scripts._environment import replace_process_environment  # noqa: E402
from state import StateStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List pending/unknown durable order intents from PostgreSQL"
    )
    parser.add_argument(
        "--env-file",
        help=(
            "Explicit environment file containing PostgreSQL settings. When omitted, "
            "use the process environment (for example Docker Compose env_file)."
        ),
    )
    args = parser.parse_args()
    if args.env_file:
        env_path = Path(args.env_file)
        if not env_path.is_absolute():
            env_path = ROOT / env_path
        try:
            replace_process_environment(env_path)
        except FileNotFoundError as exc:
            parser.error(str(exc))
    config = Config.from_env(load_dotenv_file=False, runtime_role="bot")
    state = StateStore(config, initialize_schema=False)
    try:
        print(json.dumps(state.get_unresolved_orders(), default=str, indent=2))
    finally:
        state.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
