# Production runbook

This runbook treats the bot as a real-money order service. Complete every
stage in order. A green container alone is not authorization to trade.

## 1. Server and secret boundary

- Use a dedicated Linux host or isolated VM, automatic security updates, SSH
  keys only, and a host firewall.
- Keep `.env`, `.env.bot`, `.env.dashboard`, wallet exports, database dumps,
  and TLS files outside Git. The Docker build explicitly excludes them.
- Create one low-balance account/wallet per venue. Disable Binance withdrawals
  and enable IP allowlisting. EVM/Polymarket signers must never be a primary
  wallet.
- Use a secret manager or protected server-side files with owner-only access;
  do not put secrets in systemd unit files, shell history, CI logs, or images.
- Keep dashboard secrets in its own injected environment. The dashboard refuses
  a global `SECRET_MANAGER` so it cannot accidentally load trading signers.
- Pin a trusted RPC endpoint and independently verify chain, router, base-token,
  and quote-token addresses before funding the EVM wallet.

## 2. Database

The default [`docker-compose.yml`](docker-compose.yml) database is for local
development and CI smoke tests only. For `DEPLOYMENT_ENV=production`, use
managed PostgreSQL or an operator-maintained instance with TLS. The app rejects
insecure production database settings.

Production uses the explicit
[`docker-compose.production.yml`](docker-compose.production.yml) overlay. It
removes the bot/dashboard dependency on the local `postgres` service and puts
that service behind a `local-postgres` profile, so a normal production `up`
cannot silently create a second, local database. Every production command in
this runbook uses both Compose files.

Install the database provider's CA certificate on the host. A CA certificate
is public but tamper-sensitive: keep its parent directory root-owned and make
the certificate readable by the unprivileged container process. Docker Compose
mounts it as a read-only secret at `/run/secrets/postgres-ca.pem`.

```bash
sudo install -d -o root -g root -m 0755 /etc/trading-bot
sudo install -o root -g root -m 0444 provider-postgres-ca.pem \
  /etc/trading-bot/postgres-ca.pem
```

Do not use `sslmode=prefer`, `disable`, or a self-signed/unverified endpoint
in production. Use `sslmode=verify-full` and the provider hostname that
matches the certificate subject.

Create the bot role/database once:

```sql
CREATE ROLE tradingbot LOGIN PASSWORD '<long-random-secret>';
CREATE DATABASE trading_bot OWNER tradingbot;
```

Start the bot once to apply [`migrations/001_initial.sql`](migrations/001_initial.sql),
then give the dashboard a read-only role:

```sql
CREATE ROLE tradingbot_dashboard LOGIN PASSWORD '<another-long-random-secret>';
GRANT CONNECT ON DATABASE trading_bot TO tradingbot_dashboard;
\c trading_bot
GRANT USAGE ON SCHEMA public TO tradingbot_dashboard;
GRANT SELECT ON TABLE positions, trades, meta, equity_history, orders
  TO tradingbot_dashboard;
ALTER DEFAULT PRIVILEGES FOR ROLE tradingbot IN SCHEMA public
  GRANT SELECT ON TABLES TO tradingbot_dashboard;
```

Put the write role only in `.env.bot`; put the read-only role only in
`.env.dashboard`. The dashboard opens `StateStore(..., initialize_schema=False)`
and therefore needs no DDL privilege.

Back up before every configuration change and daily thereafter:

```bash
pg_dump --format=custom --file="trading_bot-$(date +%F).dump" trading_bot
```

Test a restore in a separate database regularly. A backup that has never been
restored is only a hypothesis.

## 3. Configuration and preflight

Create isolated deployment files from the templates:

```bash
cp .env.example .env
cp .env.bot.example .env.bot
cp .env.dashboard.example .env.dashboard
chmod 600 .env .env.bot .env.dashboard
```

For production, set all of these before the first launch. The first setting
belongs in the Compose project `.env`; it is only a host path, not a database
credential:

```dotenv
POSTGRES_CA_FILE=/etc/trading-bot/postgres-ca.pem
```

Set these in both `.env.bot` and `.env.dashboard` (use the write role only in
the bot file and the read-only role only in the dashboard file):

```dotenv
DEPLOYMENT_ENV=production
POSTGRES_DSN=postgresql://<role>:<percent-encoded-password>@<provider-host>:5432/trading_bot?sslmode=verify-full
POSTGRES_SSLROOTCERT=/run/secrets/postgres-ca.pem
```

Then add these bot-only live settings to `.env.bot`:

```dotenv
BOT_MODE=live
LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_TRADING_RISK
```

And these dashboard-only settings to `.env.dashboard`:

```dotenv
DASHBOARD_SECURE_COOKIES=true
DASHBOARD_USERNAME=<non-default-user>
DASHBOARD_PASSWORD=<at-least-16-characters>
DASHBOARD_SECRET_KEY=<at-least-32-random-characters>
```

`POSTGRES_DSN` takes precedence over individual `POSTGRES_*` fields. Percent
encode reserved characters in the password; do not put `sslrootcert` in the
DSN, because the Compose secret mount is supplied through
`POSTGRES_SSLROOTCERT` instead.

Also set conservative numeric limits in `.env.bot`:

- `MAX_ORDER_NOTIONAL_USD` is the per-order hard cap.
- `MAX_POSITION_PCT` and `MAX_TOTAL_EXPOSURE_PCT` limit concentration.
- `MAX_ORDER_SLIPPAGE_BPS` is a global fill bound. EVM and Polymarket venue
  bounds cannot exceed it.
- `EVM_MAX_GAS_PRICE_GWEI`, `EVM_MAX_GAS_LIMIT`, and
  `EVM_MIN_CONFIRMATIONS` cap chain costs and finality.

Do not set `BOT_MODE=live` until the exact configuration has completed a
paper/testnet soak. Before each live deployment:

```bash
docker compose -f docker-compose.yml -f docker-compose.production.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.production.yml \
  run --rm --no-deps bot python main.py --preflight
```

Preflight must finish with no unresolved order and valid equity/data for every
configured pair. It does not place an order.

## 4. Launch and health checks

```bash
docker compose -f docker-compose.yml -f docker-compose.production.yml up -d bot dashboard
docker compose -f docker-compose.yml -f docker-compose.production.yml ps
docker compose -f docker-compose.yml -f docker-compose.production.yml logs --tail=100 bot
curl --fail http://127.0.0.1:8080/healthz
curl --fail http://127.0.0.1:8080/readyz
```

`/healthz` is process liveness. `/readyz` requires PostgreSQL plus a fresh bot
heartbeat, so it is the appropriate operator alert target. The dashboard shows
unresolved orders and persisted prices as `fresh`, `stale`, or `unavailable`; a
stale/unavailable price is never silently displayed as the entry price.

Place the dashboard behind a TLS reverse proxy. Configure the proxy to:

- redirect HTTP to HTTPS;
- authenticate at a second layer if possible;
- rate-limit `/login` and alert on 429 responses or repeated
  `dashboard_auth_failure` structured logs;
- forward `X-Forwarded-*` only from the known proxy, then set
  `TRUSTED_PROXY_COUNT` to that exact count;
- restrict dashboard ingress to your VPN/admin IPs where practical.

The application login limiter is intentionally bounded and process-local;
Gunicorn has multiple workers, so the reverse-proxy/WAF limit is required for
complete protection.

## 5. Operational controls

The bot writes one order intent before every external call. `pending` or
`unknown` means the outcome is not proven. The next bot start refuses to trade
when any unresolved order exists.

If an order becomes unresolved:

1. Stop the bot: `docker compose -f docker-compose.yml -f docker-compose.production.yml stop bot`.
2. Preserve logs and make a PostgreSQL backup.
3. Locate the durable `client_order_id` and `broker_order_id` on the dashboard
   or with the read-only helper, then check the venue’s authoritative
   order/transaction history:

   ```bash
   # Local Compose
   docker compose run --rm bot python scripts/list_unresolved_orders.py

   # External PostgreSQL production overlay
   docker compose -f docker-compose.yml -f docker-compose.production.yml \
     run --rm --no-deps bot python scripts/list_unresolved_orders.py
   ```
4. Reconcile the actual position, trade, fee, and terminal order outcome in a
   reviewed database transaction. Do not delete an order intent or blindly mark
   it rejected merely to resume automation.
5. Confirm there are no `pending`/`unknown` rows, then explicitly acknowledge
   the safety state:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.production.yml \
     run --rm --no-deps bot python main.py \
     --clear-safety-halt "reconciled <venue> order <id> on YYYY-MM-DD"
   ```

6. Run `--preflight` again before starting the loop.

The clearance command cannot bypass an unresolved durable order. That is
intentional.

## 6. Venue-specific checks

### Binance

- Use `BINANCE_ORDER_MODE=ioc_limit`; live config refuses `market` mode.
- Confirm the API key is Spot-only, withdrawal-disabled, and IP restricted.
- Make a testnet IOC order and confirm CCXT returns a terminal status.
- Non-terminal or unqueryable orders are treated as uncertain; never resend
  them manually without checking Binance by order ID/client order ID.

### EVM

- The bot verifies RPC chain ID and router bytecode at startup.
- The configured router must implement the supplied Uniswap V2-compatible ABI;
  routes requiring a different ABI are unsupported and must not be improvised.
- Native tokens are unsupported: configure wrapped ERC-20 tokens.
- A receipt timeout after broadcast is an unknown transaction state. Wait for
  the transaction hash in a block explorer/RPC before any follow-up action.
- Keep `EVM_APPROVE_MAX=false` unless you have explicitly accepted a permanent
  high allowance risk.

### Polymarket

- Use the V2 credentials/API client only. Legacy `POLYMARKET_RELAYER_*` values
  are rejected in live mode.
- Confirm account funding, approval/allowance, signer/signature type, funder,
  and selected outcome token before preflight.
- V2 FOK responses must be exact and terminally `matched`; ambiguous responses
  must be reconciled through Polymarket order history before clearance.

## 7. Upgrade and rollback

1. Stop bot and dashboard; do not use `docker compose down -v` on a live
   instance because it removes the database volume.
2. Take a database backup and record the deployed Git commit and image digest.
   The Docker Python base and local-development PostgreSQL image are pinned to
   verified multi-architecture digests in the Compose/Docker files. Runtime
   Python packages are exact-pinned and hash-verified in
   [`requirements.lock`](requirements.lock).
   For each release, build and push the tested application image once, then
   deploy its registry digest rather than a mutable tag.
3. When `requirements.txt` changes, regenerate and review the runtime lock
   before building the image:

   ```bash
   uv pip compile --python-version 3.11 --universal --generate-hashes \
     --output-file requirements.lock requirements.txt
   uv pip compile --python-version 3.11 --universal --generate-hashes \
     --output-file requirements-dev.lock requirements-dev.txt
   uv pip compile --python-version 3.11 --universal --generate-hashes \
     --output-file requirements-migration.lock requirements-migration.txt
   ```

4. Build/test the new revision, then run `python main.py --preflight` with the
   intended production environment.
5. Start the bot and monitor its first heartbeat, database writes, and
   dashboard readiness.
6. To roll back code, stop the bot, deploy the previous tested image/revision,
   and preflight again. Restore PostgreSQL only when the database itself is the
   problem; restoring older order state without venue reconciliation can create
   duplicate-order risk.

## 8. Alert conditions

Alert immediately when any of the following occurs:

- dashboard `/readyz` fails or heartbeat is stale;
- any `pending`/`unknown` order exists;
- `halted_safety`, `halted_daily`, or `halted_total` is true in `meta`;
- repeated bot cycle failure count reaches the configured threshold;
- PostgreSQL backup fails or restore verification is overdue;
- host disk, memory, clock sync, or TLS certificate checks fail.
