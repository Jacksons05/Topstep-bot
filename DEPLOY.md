# Deploying the Polymarket bot to Railway

This bot runs as a **worker** (no web port). Railway runs `python run.py`
continuously (per `railway.toml` / `Procfile`) and restarts it on failure.
State (positions + P&L) is persisted to a Railway Postgres database via
`DATABASE_URL`, which Railway injects automatically once you add the DB.

> Start in `paper` mode. Only switch `TRADING_MODE=live` after weeks of paper
> P&L convince you. Live mode places real GTC orders with real USDC.e.

---

## 0. Commit local changes first

Railway deploys from your Git repo, so anything uncommitted won't ship. There
are pending local edits (runtime bump to Python 3.12, `backtest.py`, and the new
`dashboard.py`). Commit and push them:

```bash
cd polymarket-bot
git add -A
git commit -m "Bump runtime to 3.12; add dashboard + backtest dotenv loading"
git push origin main
```

`.env` is gitignored and will **not** be pushed — that's correct. You set those
values in the Railway dashboard instead (step 3).

---

## 1. Create the Railway project

**Option A — from GitHub (recommended):** push the repo to GitHub, then in
Railway: **New Project → Deploy from GitHub repo →** pick this repo. Railway
auto-detects the Python app via nixpacks and uses the `startCommand` in
`railway.toml`.

**Option B — Railway CLI:**

```bash
npm i -g @railway/cli
railway login
railway init          # create/link a project
railway up            # deploy current directory
```

---

## 2. Add Postgres (for persistent state)

In the Railway project: **New → Database → Add PostgreSQL.**

Railway automatically injects `DATABASE_URL` into your service. The bot creates
its own tables on first start (`positions`, `state_meta`) and appends
`sslmode=require` to the connection string for you. No manual SQL needed.

Without a database the bot still runs, but **state is lost on every restart**
(it logs a warning). For a 24/7 deploy you want Postgres.

---

## 3. Set environment variables

In the service's **Variables** tab, add the following. `DATABASE_URL` is already
there from step 2 — don't set it manually.

### Required to start

| Variable | Value | Notes |
|---|---|---|
| `TRADING_MODE` | `paper` | Keep `paper` until proven. `live` = real money. |
| `LLM_ENABLED` | `true` | You chose to keep the LLM layer on. |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | **Paste your own key here.** Bot won't start with LLM on and this empty. Get it from console.anthropic.com. |

### Scanner / thresholds (have sane defaults — override to taste)

| Variable | Default | Notes |
|---|---|---|
| `SCAN_INTERVAL_SEC` | `60` | Seconds between scans. |
| `MIN_VOLUME_USD` | `10000` | Your local `.env` used `50000`. |
| `MIN_LIQUIDITY_USD` | `2000` | Skip thin books. |
| `MIN_HOURS_TO_RESOLUTION` | `24` | Skip markets resolving too soon. |
| `MAX_MARKETS` | `2000` | Your local `.env` used `300`. |
| `ARB_THRESHOLD` | `0.01` | Flag if `yes_ask + no_ask <= 1 - this`. |
| `EDGE_THRESHOLD` | `0.10` | Flag if `|llm_prob - price| >= this`. |
| `LLM_MODEL` | `claude-sonnet-4-6` | |
| `LLM_MAX_MARKETS_PER_CYCLE` | `15` | Cost control — only top-N by volume get an LLM call. |
| `LLM_MIN_CONFIDENCE` | `medium` | `low\|medium\|high`. |

### Trading / risk limits

| Variable | Default | Notes |
|---|---|---|
| `BANKROLL_USD` | `1000` | |
| `MAX_POSITION_PCT` | `0.02` | Max 2% of bankroll per market. |
| `MAX_CONCURRENT` | `10` | Max open positions. |
| `STOP_LOSS_PCT` | `0.20` | Exit if 20% against thesis. |
| `DAILY_DRAWDOWN_PCT` | `0.10` | Kill-switch: pause new entries if down >10% in a day. |
| `MIN_EXECUTABLE_SIZE_USD` | `5` | |

### Optional

| Variable | Notes |
|---|---|
| `DISCORD_WEBHOOK` | Get trade alerts pushed to Discord. |
| `LOG_FILE` | Defaults to `signals.log` (ephemeral on Railway). |

### Live-only (leave blank until `TRADING_MODE=live`)

| Variable | Notes |
|---|---|
| `POLY_PRIVATE_KEY` | Polygon wallet private key, funded with USDC.e. |
| `POLY_FUNDER_ADDRESS` | Optional proxy/funder address. |
| `CLOB_HOST` | Defaults to `https://clob.polymarket.com`. |

---

## 4. Deploy & verify

After variables are set, Railway redeploys automatically (or hit **Deploy**).
Watch the service **Logs**. A healthy start looks like:

```
=== Polymarket scanner starting | mode=paper | interval=60s | llm=on ===
scan: 94 tradeable markets | open=0 | realized=$0.00 | dayPnL=$0.00
```

If you instead see `CONFIG ERROR: LLM_ENABLED but ANTHROPIC_API_KEY is empty`,
your key didn't save — re-check the Variables tab.

---

## 5. Monitoring (optional)

`dashboard.py` is a zero-dependency local status page. Run it **on your laptop**
pointed at the same Postgres as Railway:

```bash
export DATABASE_URL="<the same Postgres URL from Railway's Variables tab>"
python dashboard.py            # http://127.0.0.1:8787
```

It reads the DB the bot writes to, so you get live positions/P&L without
exposing anything publicly.

---

## Going live (only when paper P&L convinces you)

1. Fund a Polygon wallet with USDC.e.
2. Set `POLY_PRIVATE_KEY` (and `POLY_FUNDER_ADDRESS` if using a proxy).
3. Set `TRADING_MODE=live`.
4. Redeploy. The bot pauses 5s on startup so you can abort.

**Known caveat (from the README):** structural-arb is flagged on both legs but
the executor currently opens the *cheaper leg only* — wire the second leg before
trusting arb P&L with real money.
