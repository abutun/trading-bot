# Trading Bot SaaS — Product & Architecture Sketch

## 1. Positioning

**One-liner:** "Set-and-forget momentum trading for Binance, EVM, and Polymarket, with real risk management."

**Target user:** retail crypto traders who know what an EMA is but don't want to babysit charts or maintain Python.

**Differentiators:**
- Risk management first (drawdown kill-switch, daily loss limit) — most consumer bots hide this
- Paper trading before live (you already have `paper.py` — make it the onboarding)
- Transparent: users see every order, the exact strategy logic, and a one-click kill switch

## 2. Pricing tiers

| Tier | Price | Includes |
|---|---|---|
| Free | $0 | Paper trading, 1 symbol, EMA/RSI strategy. This is the funnel. |
| Starter | $29/mo | Live trading, up to 3 symbols, Telegram/email alerts |
| Pro | $79/mo | 10 symbols, all strategies, in-browser backtests, EVM wallets, API/webhooks |
| (Later) Performance | base + 10% of monthly profit | Optional, for users who want skin in the game. Controversial — keep it opt-in and clearly disclosed |

Annual = 2 months free. Affiliate program at 30% recurring from day one (traders refer traders).

## 3. User journey (MVP)

1. Sign up (email + password or Google OAuth)
2. Create a Binance API key (**spot trade only, withdrawals disabled**) → paste into app
3. Server validates the key: can read account ✓, can place orders ✓, withdrawals disabled ✓ (reject or hard-warn otherwise)
4. Pick strategy + symbol + risk params → **paper trades by default** for a few days
5. One-click "Go live" with the identical config
6. Dashboard: equity curve, open positions, trade log, P&L, big red KILL SWITCH button
7. Telegram alert on every fill and every risk event (stop hit, kill-switch triggered)

## 4. Architecture (multi-tenant)

```
[Web app]  ──►  [API: FastAPI]  ──►  [Postgres]   [Redis]
                     │                    ▲            ▲
                     ▼                    │            │
              [Orchestrator / worker pool]──────────────┘
               • one asyncio bot task per active user-strategy
               • reuses brokers/ + strategy.py + risk.py
                     │
              [Backtest worker (arq on Redis)] ──► results → Postgres + S3

[WebSocket]  live equity/trade updates: bot task ──► Redis pub/sub ──► web app
```

### Components

- **Web app** — onboarding, key vault UI, strategy config, dashboard. Rebuild the Flask templates in Next.js/React (or keep Flask + HTMX if you want speed over polish).
- **API (FastAPI)** — JWT auth, tenant CRUD, key management, strategy config, backtest job submission, WebSocket endpoint. Natural upgrade from the existing Flask dashboard.
- **Bot engine** — refactor `bot.py` into a `BotEngine(config, broker, state)` class. One instance per user-strategy, run as an asyncio task in a worker process. Per-tenant isolation: exceptions contained per task; watchdog restarts crashed tasks; startup reconciliation (compare local state vs exchange before resuming).
- **Secret vault** — the only service that can decrypt exchange keys. AES-256-GCM at rest, master key in AWS KMS (you already support AWS Secrets Manager — extend that pattern). MVP shortcut: encrypt/decrypt in-process with a KMS-wrapped key; document it.
- **State** — PostgreSQL now persists positions, trades, equity, metadata, and durable order intents. For SaaS, add JSONB per-tenant strategy config and `user_id` to every table; extend the existing `orders` records with tenant/audit metadata. A restart must never double-trade.
- **Backtest service** — parameterize `backtest.py` into an async job (arq/Celery on Redis). Output: equity curve, max drawdown, Sharpe, win rate, trade list → stored in Postgres/S3, rendered in the browser.
- **Alerts** — Telegram bot + email worker; per-user subscriptions, triggered from the bot engine via Redis.

### Scaling path

- **MVP:** 1–2 VMs. The bot loop is I/O-bound (polling exchange APIs), and each user has their *own* Binance API weight budget, so one asyncio process comfortably runs ~50–100 active strategies.
- **Growth:** worker pool sharded by user ID, then containers/k8s per shard. Backtests scale independently (they're CPU-bound batch jobs).

## 5. Reuse vs. rebuild

| Existing | Action |
|---|---|
| `brokers/*` (binance, evm, paper) | **Reuse** — parameterize per tenant instead of env vars |
| `strategy.py`, `risk.py` | **Reuse** — the core engine, wrap in `BotEngine` |
| `backtest.py` | **Reuse** — parameterize, turn into a job service |
| `state.py` | **Extend** — add `user_id`, Postgres, orders table |
| `config.py` (env vars) | **Replace** — per-tenant config rows in DB; env only for infra secrets |
| `dashboard/` (Flask) | **Rebuild** — becomes the web app frontend |
| `main.py` loop | **Replace** — engine class + orchestrator |

**Net new:** auth, billing (Stripe), secret vault, backtest jobs, alerts, WebSocket layer.

## 6. Security & trust (this IS the product)

Users are handing you keys to their money. Trust is the moat:

- Enforce **withdrawals-disabled** at key connection; re-check periodically
- Keys encrypted (KMS-wrapped), **audit log of every order** placed on a user's behalf
- Per-user kill switch + global kill switch (infra incident → halt all trading)
- **Reconciliation on startup**: local state vs. exchange before any new orders
- EVM: dedicated hot wallet per user, never a shared key. Later: user-deployed contract / EIP-712 signed orders so you never hold the private key at all
- Public trust page: how keys are stored, exact permissions required, incident history

## 7. Risks & hard parts

1. **Liability** — you're executing with users' funds. Mitigate: paper-first onboarding, clear ToS, start with small AUM, global kill switch.
2. **Exchange API changes/outages** — per-tenant circuit breakers, error budgets, "degraded mode" (manage exits only, no new entries).
3. **Strategy decay** — the product is the *platform*, not one strategy. Ship more strategies (grid, DCA, momentum variants) and let users tune parameters; that's what keeps churn low.
4. **Support load** — retail users ping "is it working?" constantly. A great dashboard + Telegram alerts on every event cuts this dramatically.

## 8. Roadmap

- **Phase 0 (2–3 wks):** `BotEngine` refactor, Postgres + `user_id`, FastAPI auth, key vault, Stripe.
- **Phase 1 — MVP (4–6 wks):** onboarding flow, paper trading live in prod, dashboard v2, Telegram alerts. **Launch Free + Starter.**
- **Phase 2 (1–2 mo):** in-browser backtests, 2–3 more strategies, Pro tier, EVM wallets.
- **Phase 3:** strategy marketplace / copy-trading — users publish strategies, you take 10–20% rev share. This is the viral loop and the real long-term value.

### GTM

- Landing page with **anonymized live performance stats** (aggregate equity curves of real paper-trading users — proof without risk)
- YouTube: backtest walkthroughs, "I let the bot trade $X for 30 days"
- Telegram community as the support + referral hub
- 30% affiliate from day one
