from __future__ import annotations

import logging

from config import Config


def main() -> None:
    config = Config.from_env(runtime_role="dashboard")

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from dashboard.app import create_app

    app = create_app(config)

    print(f"Dashboard: http://{config.dashboard_host}:{config.dashboard_port}")
    app.run(host=config.dashboard_host, port=config.dashboard_port)


if __name__ == "__main__":
    main()
