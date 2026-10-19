# Crypto Desk

A "trading desk" of six agents that trades large coins (BTC, ETH, SOL, plus any you add) on your **CoinSpot** account in AUD. It was built for day trading, but testing showed that loses money after CoinSpot's costs, so by default it now runs **daily trend-following** (see section 3).

The six agents:

| Agent | What it does | Uses AI? |
|---|---|---|
| Market Scanner | Reads prices, works out RSI / MACD / EMAs / ATR / volume spikes, and flags possible trades | No |
| News Analyst | Reads crypto news, scores each coin's mood from -1 to +1, and flags hacks, regulation and outages | Yes |
| Entry Agent | Proposes buys with an entry price, stop-loss, take-profit, reasoning and confidence | Yes |
| Exit Agent | Watches open positions. Stops and take-profits run in plain code, so they still work if the AI fails | Yes + code |
| Skeptic | Argues against every proposal, then votes | Yes |
| Risk Manager | Hard rules with the final veto. It can't be talked out of a "no" | No |

> **It starts in PAPER mode (fake money).** It only trades real money when `LIVE_TRADING=true` is set in `.env` and every live-trading safety check passes. This is software, not financial advice. Most day-trading strategies lose money after fees, so paper trade for weeks before considering live.

## Build progress

| Stage | What | Status |
|---|---|---|
| 1 | Setup, config, CoinSpot read-only client, price data | ✅ done |
| 2 | Scanner + News | ✅ done |
| 3 | Entry / Exit / Skeptic agents | ✅ done |
| 4 | Risk Manager + tests | ✅ done |
| 5 | Paper trading engine + database | ✅ done |
| 6 | Backtester | ✅ done |
| 6b | Strategy upgrade: daily trend-following, 10% per trade | ✅ done |
| 7 | Telegram alerts/commands + dashboard | ✅ done |
| 8 | Watchdog + Docker + 24/7 running | ✅ done, see section 12 |
| 9 | Live trading (behind the safety flag) | only when you ask |
| R1 | Prop firm research: Tradeify 247 2-Step | ✅ done, see section 13 |
| R2 | Prop mode: the bot trades prop accounts (simulated now, DXtrade ready) | ✅ done, see section 14 |
| R3 | Real prop account: connection + test trades | ✅ done, see section 14 |

---

## 1. Setup on Windows (one time)

You need **Python 3.12 or newer**. You already have 3.14.

1. Open the `crypto-desk` folder in File Explorer.
2. Double-click **`setup.bat`**. It creates a private Python environment (`.venv`), installs everything and creates your `.env` file. The first run takes a few minutes. Wait for **"Setup complete"**.
3. Double-click **`test.bat`**. The last line should say something like `461 passed, 1 skipped`. The skipped test needs the internet; `.\test.bat --network` runs it too.
4. Double-click **`check.bat`**. You should see:
   - live CoinSpot prices and spreads for BTC, ETH and SOL
   - "CoinSpot vs global price" with small differences, usually under 0.5%, and **OK**
   - a candle table where every row says **OK**
   - **RESULT: ALL CHECKS PASSED**

### Running commands in PowerShell (for the extra options like `--debate`)

PowerShell must be **inside the `crypto-desk` folder**, or it will say *"'.\scan.bat' is not recognized"*. The easiest way to get there:

1. Open the `crypto-desk` folder in File Explorer.
2. Click the address bar at the top, so the folder path turns blue, type `powershell`, and press Enter.
3. A PowerShell window opens already in the right folder. The prompt ends in `\crypto-desk>`.

Or, in any PowerShell window, go to the folder with `cd`, keeping the quotes if the path has a space in it (use your own path):

```powershell
cd "C:\path\to\crypto-desk"
```

Then run commands like `.\scan.bat --pretend-position SOL`. The `.\` at the start is needed.

## 2. API keys (the `.env` file)

Open `.env` with Notepad. Every key is optional at this stage. Paper trading works with no keys at all.

| Line in `.env` | Where to get it | Needed for |
|---|---|---|
| `COINSPOT_READONLY_KEY` / `_SECRET` | CoinSpot → Settings → API → create a **Read Only** key | Optional. Lets `check.bat` confirm your account connects. |
| `COINSPOT_KEY` / `_SECRET` | CoinSpot → Settings → API → full-access key. **Never tick the withdraw option.** | Live trading only (Stage 9) |
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys | AI agents (Stage 2 onwards) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram's @BotFather, then `telegram_setup.bat` fills in the chat ID (section 11) | Alerts and phone commands |
| `CRYPTOPANIC_TOKEN` | cryptopanic.com/developers/api | Optional extra news source |
| `DASHBOARD_PASSWORD` | Make one up | Optional password for the web dashboard |
| `HEALTHCHECKS_URL` | healthchecks.io (free), section 12 | Optional: alerts you if the whole PC/server dies |

**Security rules the bot follows:**
- Keys live only in `.env`, which git ignores, so it's never committed or shared.
- Logs mask anything that looks like a key or token.
- If a full-access key is present, the bot asks CoinSpot for withdrawal details for that key. That request moves no money, but it only succeeds for keys that are allowed to withdraw. If it succeeds, the bot **refuses to run** until you replace the key with one that has withdrawals switched off.
- The code contains no withdraw, send, swap or instant-buy functions at all, and a test checks that this stays true.

## 3. Settings (`config/config.yaml`)

Every trading limit is in `config/config.yaml`, with a comment on each line. Edit it with Notepad. The defaults:

- **10%** of the portfolio max per trade (your choice; the original rule was 5%) · 3 open positions max · stop-loss required on every trade
- 3% daily loss limit, then no new trades until midnight (in your `timezone`)
- 6 new trades per day max
- a trade is rejected unless its expected profit clearly beats CoinSpot's fees plus the spread
- trading is blocked on stale data or API errors

If you misspell a setting or enter an impossible value, the bot refuses to start and tells you which line is wrong.

### Strategy (`strategy.mode`)

| Mode | What it does | Runs in the bot? |
|---|---|---|
| **`trend`** (default) | Once a day, after the daily candle closes (00:00 UTC), it buys a coin whose daily close breaks above its 20-day high while above its 50-day average. It holds for days to weeks and exits only via a trailing stop 2 daily ATRs below the highest price since buying. There is no fixed take-profit, so winners can run. | ✅ yes |
| `day` | The original strategy: 15-minute setups, trades lasting hours, 24h max hold. | ✅ yes |
| `swing` | 1-hour setups, bigger targets, up to 3-day holds. | backtest only |

**Why trend:** we tested every option fairly. Settings were tuned on older data, then judged on more recent data they had never seen:
- **Day trading lost money in all 40+ variants.** The average trade lost about what a round trip costs (0.4–0.5%), so the signals had no edge.
- **Limit orders didn't rescue it.** Waiting at the bid saves the spread, but it let more weak trades through, and those lost more.
- **Swing trading got closer, but still lost.**
- **Daily trend-following was the only approach that made money on unseen data.** On the tuning years (2020–24) all 12 variants were profitable. On the unseen 2 years (Oct 2024–Oct 2026) the chosen setting made +1.9% at 5% per trade, which is about +3.6% at your chosen 10%. Its worst drop was about 5% at 5% per trade, while BTC itself fell 54%. It also beat holding BTC with the same amount of money invested.

**Be realistic:** that's a small edge from a small sample (46 trades in 2 years), and nothing guarantees it continues. Holding BTC outright made far more in those years, but with drops of 54–77%. Trend-following trades rarely, so expect weeks with no trades at all.

Limit orders (`execution:`) stay backtest-only, since they didn't help.

## 4. How the bot gets its data

CoinSpot's API has **no price-history (candle) endpoint**, and indicators like RSI need candles. So:
- **Indicators** use free public candles from **Binance**. If Binance is unreachable, they come from **Kraken** in AUD.
- **Every actual price, spread and fill** comes from **CoinSpot** itself.
- A safety check compares CoinSpot's price with the global price. If they're more than 1.5% apart, something is wrong, and new trades are blocked.

The bot only uses CoinSpot's cheapest order type, **market orders at 0.1%**. It never uses instant buy/sell or CoinSpot's own stop and take-profit orders, which cost 1%. So stop-losses run inside the bot. That's why the 24/7 setup, automatic restarts and alerts in later stages matter: **if the bot is off, nothing is watching your stops.**

## 5. Market scan (`scan.bat`)

Double-click **`scan.bat`** to run the Market Scanner and the News Analyst once. It only reads data and never trades. You'll see:

- **Market Scanner table**, one row per coin:
  - CoinSpot price and spread
  - the 1h **trend** (up / down / flat)
  - **RSI** on 5m / 15m / 1h. Under 30 is oversold, over 70 is overbought.
  - **MACD** momentum ("+ rising" is good for buying)
  - **ATR**: how big a typical 15m candle is
  - **Volume** compared with normal. 2.0x or more is a spike.
- **Setups**: a reason to consider buying. There are three kinds, and you can switch each one on or off in `config.yaml`:
  - *trend pullback*: in an uptrend, price dips back to its average and turns up again
  - *volume breakout*: price makes a new high on a big volume spike
  - *oversold bounce*: in a long-term uptrend, RSI bounces back out of oversold
  Most scans find **nothing**. That's normal, and it's what keeps AI costs near zero.
- **Warning signs**: bearish signals. They only matter for coins the bot holds, and the Exit Agent uses them from Stage 3.
- **Data problems**: if a price is stale, candles are missing, or CoinSpot's price is too far from the global price, that coin can't be traded until it's fixed.
- **News**: the latest headlines per coin from 7 free news feeds. Words like "hack", "SEC" or "outage" mark a headline as a *possible event*.
- **News Analyst (AI)**: it runs **only for coins the scanner flagged**. It scores the news mood from -1 to +1 with a one-line reason, and decides if a possible event is really a **major event**. A major event blocks new trades in that coin for 6 hours. Hack or outage news that names **CoinSpot** blocks all coins immediately, without waiting for the AI.

To try the AI even when nothing is flagged, run `.\scan.bat --score-all` in PowerShell. You need `ANTHROPIC_API_KEY` in `.env` first. Each AI call costs a small fraction of a cent. The bot records every call and stops calling the AI for the day once `llm.daily_spend_cap_usd` (default US$1.00) is reached.

**Changing what the AI is told:** the instructions are plain text in `prompts/news.md`. Edit them with Notepad, and the next call uses your version. The answer format is fixed in code, so you can't break it by editing the prompt.

**CryptoPanic:** it no longer has a free API, and only paid plans work. The bot uses it only if you put a `CRYPTOPANIC_TOKEN` in `.env`. The free RSS feeds are enough.

## 6. The debate: Entry Agent, Skeptic, Exit Agent

When the scanner flags a coin, the agents debate it:

1. The **Entry Agent** reads a briefing with prices, costs, indicators, setups, news and the desk rules. It proposes **BUY**, with an entry, stop-loss, take-profit, confidence (0–100) and its reasoning, or **NO TRADE**. Passing is a normal answer.
2. Basic sanity check: the stop must be below the entry, which must be below the take-profit, and the entry must match the market price. A broken proposal is thrown out without paying for the Skeptic.
3. The **Skeptic** lists the strongest reasons the trade could fail, then votes **BUY** (go ahead), **WAIT** (not yet) or **SKIP** (no).
4. Only a BUY vote goes on to the **Risk Manager** (Stage 4), which has the final veto.

For coins the desk holds, two things happen:
- **Code exits** run every minute with no AI involved: the stop-loss, a **trailing stop** (once the price is 1% up, the stop follows 0.8% under the highest price, so gains are protected), the take-profit, and a 24-hour maximum hold. They keep working even if the AI is down or out of budget.
- The **Exit Agent** (AI) is only asked when there's a warning sign or bad news. It can **HOLD**, **TIGHTEN_STOP** (stops can only move up) or **SELL**. A SELL must also get the Skeptic's agreement.

If any AI call fails, the answer counts as "no", so a failure never causes a trade.

Every debate is saved with its full transcript, and the dashboard shows them (section 11). To edit what each agent is told, change `prompts/entry.md`, `prompts/exit.md` or `prompts/skeptic.md`.

**Try it** (needs `ANTHROPIC_API_KEY` in `.env`; each run costs well under 1 cent). Run these in PowerShell from the `crypto-desk` folder:

```powershell
.\scan.bat --debate BTC
```

```powershell
.\scan.bat --pretend-position SOL
```

`--debate` runs a full entry debate for BTC even if the scanner didn't flag it. `--pretend-position` pretends the desk bought SOL 3 hours ago, then shows the code exit check and asks the Exit Agent and the Skeptic what to do. Nothing is ever bought or sold by `scan.bat`.

**Heads-up about the fee rule:** CoinSpot's spread is often 0.1–0.5%. With the default rule that the take-profit must be 3× the round-trip cost, most setups need a target of 1.5% or more, so the desk trades rarely. That's deliberate, and `risk.min_edge_multiple` in `config.yaml` is the setting to change if you want it less strict.

## 7. The Risk Manager (the final veto)

Even when both AI agents agree, nothing is bought until the **Risk Manager** says yes. It's plain code with no AI, so it can't be talked round. It checks **every** rule every time and logs all the reasons for a "no". All limits are in `config/config.yaml` under `risk:`.

| Rule | Default |
|---|---|
| A stop-loss is required, below the entry and no more than 5% away (20% in trend mode, where daily stops are wider) | `max_stop_distance_pct: 5.0` / `strategy.trend.max_stop_distance_pct: 20` |
| Max size per trade (fee included) | `max_position_pct: 10.0` (10% of the portfolio) |
| Max open positions, and only one per coin | `max_open_positions: 3` |
| Daily loss limit: after this, no new trades until local midnight | `daily_loss_limit_pct: 3.0` |
| Max new trades per day | `max_entries_per_day: 6` |
| Reward:risk at least | `min_reward_risk: 1.5` |
| Take-profit gain must be at least N × (fees + spread + slippage) | `min_edge_multiple: 3.0` |
| Entry Agent confidence at least | `min_confidence: 60` |
| The Skeptic must have voted BUY | always |
| Blocked by: stale prices, CoinSpot errors, data problems, major news events, /pause, /kill | always |
| Live mode: never use more than the live capital cap | `live.max_capital_aud: 200` |

**Selling is never blocked** by pause, kill, the daily limit or news. Selling only reduces risk, so stop-losses always work.

**See it work:** double-click **`risk_demo.bat`**. It checks six sample trade ideas against **live** CoinSpot prices with a pretend A$10,000 paper account. Then it shows how the same good idea is refused when trading is paused, after a 3% losing day, with 3 positions open, and so on. No AI, no key needed, no orders. For another coin, run `.\risk_demo.bat --coin ETH` in PowerShell.

**Testing:** the Risk Manager has the most tests in the project (about 120). Every rule is tested on its own and at its exact limit, for example a 3.00% loss is refused and 2.99% is allowed. Garbage numbers are always refused. Randomised tests generate thousands of trades each run and confirm that **every approved trade obeys every rule**, and that trades meeting every rule are never wrongly refused.

## 8. Paper trading: running the bot

Double-click **`paper.bat`** and **leave the window open**. The bot trades with a **pretend A$10,000** (`paper.starting_balance_aud`), using live CoinSpot prices.

**What it does by itself:**
- **Every minute (no AI):** checks CoinSpot prices, runs the stop-loss, trailing stop, take-profit and max-hold checks on every position, records your equity, and checks the 3% daily loss limit.
- **In trend mode (the default), once a day** just after the daily candle closes (about 00:01 UTC):
  1. checks each coin for the breakout signal
  2. for a coin with a signal: the News Analyst, Entry Agent and Skeptic decide yes or no, the Risk Manager checks it, and the paper trade is placed

  The stop and trailing stop come from the tested rule, not the AI. If the PC was off at the daily close and it's been more than 12 hours, that day's signal is skipped rather than bought late. Run `scan.bat` to see how close each coin is to a signal.
- **In day mode, every 15 minutes**, at :00:20, :15:20, :30:20 and :45:20, just after each 15-minute candle closes:
  1. runs the Market Scanner
  2. refreshes the news
  3. for held coins showing warning signs, the Exit Agent and Skeptic review them
  4. for flagged coins, the Entry Agent and Skeptic debate them
  5. the Risk Manager checks anything agreed
  6. the paper trade is placed
- **Paper fills are realistic:** a buy works through CoinSpot's **real order book**, just as a real order would, then the 0.1% fee and a little extra slippage are added. For example, buying and instantly selling about A$500 of ETH cost about A$3.24 (0.65%) in a live test.
- **It doesn't waste AI money:** if a new trade couldn't happen anyway (paused, at the daily loss limit, at max positions or max trades), it doesn't ask the AI.

**Keeping it running:**
- If the bot crashes, `paper.bat` restarts it after 15 seconds.
- If a loop ever freezes, the bot notices, exits and gets restarted.
- On every start it reloads its positions, cash, switches and today's numbers from the database, so nothing is lost.
- **While the window is closed or the PC is asleep, nothing watches your stops.** For paper that only means missed fake trades. For live trading, run it 24/7 on a server (section 12).

**While it runs**, use these from another window (double-click, or run in PowerShell inside the folder):

| Command | What it does |
|---|---|
| `status.bat` | Equity, cash, today's P&L, open positions, today's trades, AI spend, debates, vetoes, recent events, and whether the bot is running. Safe to run any time. |
| `control.bat pause` | No new trades. Open positions are still managed. |
| `control.bat resume` | New trades allowed again. |
| `control.bat kill` | Stops trading and AI calls. Stop-losses still protect open positions. |
| `control.bat kill --close-all` | Kill, and also sell every open position. |
| `control.bat resume --confirm` | Lifts a kill. |
| `control.bat close-all` | Sells every position and keeps running. |

The bot picks up a switch change within a minute. Telegram and the web dashboard have the same switches (section 11).

**Stopping:** press **Ctrl+C** in the bot window, then answer `Y` if Windows asks "Terminate batch job". A Ctrl+C stop is recorded, so the watchdog (section 12) knows you stopped it on purpose and doesn't raise an alarm.

**Time zone:** the bot's "day" (daily loss limit, trades per day, daily summary at 23:55, tax dates) follows `timezone:` in `config.yaml`. Set it to your own time zone, for example `Australia/Sydney`, `Europe/London` or `America/New_York`.

## 9. Tax records (`export_tax.bat`)

Every trade is recorded. Each sell is a CGT event, and its cost base includes the buy fee. Your proceeds are what you received after the sell fee, and the gain is proceeds minus cost base. Each sale is matched to the exact buy it came from. To make the CSV, run in PowerShell:

```powershell
.\export_tax.bat --fy 2026
```

FY2026 = 1 July 2025 to 30 June 2026, in your local time. The file appears in `reports\` with one row per trade:
- date (local and UTC), coin, side, quantity, AUD price, AUD amount, fee, net amount
- for sells: cost base, capital gain or loss, days held and purchase date

By default only **live** trades are included. Add `--include-paper` to try it with paper trades. This is a record-keeping aid, not tax advice: give the file to your accountant.

## 10. Backtesting (`backtest.bat`)

Double-click **`backtest.bat`**. It replays the last 180 days of prices through the desk and opens a report in your browser. It doesn't need the PC left on overnight: a 6-month test takes about a minute. The first run downloads the price history into `data\candles\`; later runs only download what's new.

**What's in the report** (`reports\backtest_<date>\report.html`):
- **total return**, **win rate**, **max drawdown** (the worst fall from a high point) and **fees paid**
- the same figures for **just holding BTC** over the same period
- a chart of the account value against holding BTC, with a drawdown panel underneath
- `trades.csv` with every trade, and `equity_hourly.csv`

**How it works:** it replays 5-minute candles through the *same code* the live desk uses:
- the scanner rules
- the Risk Manager (every rule)
- the exits (stop-loss, trailing stop, take-profit, 24h max)
- the order handling

Three things keep it honest:
- **No peeking ahead.** At each moment it only sees candles that had already closed.
- **No instant fills.** An entry fills at the **next** candle's open.
- **Pessimistic stops.** If one candle touched both the stop and the target, it assumes the stop came first.

**What it can't test:**
- **The AI agents.** Every setup the scanner finds is taken, using its suggested stop and target. The real desk is pickier, because the Entry Agent and Skeptic say no to weak setups. Paper trading is how the AI gets tested.
- **News.** Major-event blocks can't be replayed.
- **CoinSpot's real spread** isn't recorded historically, so `backtest.assumed_spread_pct` (0.2%) is used throughout.

The backtester uses the strategy in `config.yaml`, so by default it tests **trend mode**, on hourly candles. Test the trend strategy over 6 years with:

```powershell
.\backtest.bat --days 2190
```

The report also shows **"same money invested in BTC"**. Your per-trade limit keeps most of the money in cash, so that's the fair comparison, not putting everything into BTC. For the results that led to trend mode, see section 3.

Change one setting at a time and re-run. Be wary of tuning until the past looks perfect: settings tuned to fit the past rarely work as well in the future. Tune on older data, then check on recent data.

**Options** (in PowerShell):

```powershell
.\backtest.bat --mode day --days 365
```

```powershell
.\backtest.bat --mode swing --entry limit --tp limit
```

```powershell
.\backtest.bat --coins BTC,ETH --spread 0.1
```

## 11. Telegram and the dashboard

### Telegram: alerts and commands on your phone

**One-time setup:**
1. Create the bot (already done: yours is **@jcryptodesk_bot**, and its token is in `.env`).
2. On your phone, open **t.me/jcryptodesk_bot** (or search `jcryptodesk_bot` in Telegram), press **Start**, and send it **hi**.
3. Double-click **`telegram_setup.bat`**. It finds your chat ID from that message, saves it to `.env` (it asks first), and sends you a test message.
4. That's it. A bot that's already running switches Telegram on by itself **within a minute**, with no restart needed.

**What it sends you:** every trade, Risk Manager vetoes, the daily loss limit being hit, errors, start-ups and restarts, and a daily summary at 23:55.

**Commands** (send them to your bot; it only obeys **your** chat):

| Command | What it does |
|---|---|
| `/status` | Account value, today's P&L, switches, AI spend |
| `/positions` | Open positions with live profit/loss |
| `/pause` | No new trades. Open positions are still managed |
| `/resume` | New trades allowed again |
| `/kill` | Stops trading and AI. Stop-losses still protect open positions |
| `/kill close` | Kill **and sell everything**. Asks you to send `/confirm` within 60 seconds |
| `/resume confirm` | Lifts a kill |
| `/help` | This list |

### The dashboard (web page)

Double-click **`dashboard.bat`**. Your browser opens **http://127.0.0.1:8501** after a few seconds. It works whether or not the bot is running, and only this PC can open it.

It shows:
- account value over time and open positions
- recent trades and the Risk Manager's decisions
- every debate (Entry Agent vs Skeptic) in full
- AI spend by agent, news blocks and recent events

The sidebar has the **Pause / Resume / Kill / Kill and sell everything** switches. Tick "I'm sure" first for the kill buttons. Set `DASHBOARD_PASSWORD` in `.env` if you want a password. The page refreshes itself every 30 seconds. Close the black window to stop the dashboard; the bot keeps running.

## 12. Running 24/7

The bot can only protect positions while it's running. There are three layers:

1. **Restarts by itself.** `paper.bat` (or Docker) restarts the bot if it crashes. If a loop freezes, the bot notices, exits and gets restarted. Everything is reloaded from the database.
2. **The watchdog** (`watchdog.bat`, or automatic in Docker). This is a separate small program that messages you on Telegram if the bot hasn't checked prices for **10 minutes**. It reminds you every hour while the bot is down, and tells you when it's back. If you stopped the bot yourself (Ctrl+C or `docker compose stop`), you get one calm message instead of alarms. Settings: `watchdog:` in `config.yaml`.
3. **healthchecks.io** (optional, free). It covers the one thing the first two can't report: the **whole PC or server** being off, asleep or offline.
   1. Sign up at healthchecks.io and create a check with **Period 5 minutes** and **Grace 10 minutes**.
   2. Copy its ping URL (`https://hc-ping.com/...`) into `.env` as `HEALTHCHECKS_URL=`.
   3. Connect it to your email or Telegram on their website.

   The watchdog pings it every minute while the bot is healthy. If the pings stop, healthchecks.io alerts you.

### On this Windows PC
- Run `paper.bat`, plus `watchdog.bat` in a second window if you want the alerts.
- The bot stops the PC from **sleeping** while it runs (`keep_pc_awake`), but a shutdown, a Windows Update restart or a power cut still stops it.
- **Start automatically when you log in:** press `Win+R`, type `shell:startup`, press Enter, then right-click → New → Shortcut to `paper.bat` (and another to `watchdog.bat`).

### On a server (recommended before live trading)
A small Linux server ("VPS") runs all day for about A$6–10 a month. Pick a **Sydney** or **Singapore** region: some US regions can't reach Binance's price data.

The server runs three Docker containers:
- **bot:** restarts after crashes and server reboots
- **watchdog:** sends the Telegram alerts
- **dashboard:** reachable from the server only

Docker's own log files are capped at 5 × 10 MB per container. The database clears out old scan, price and news rows every day, so the disk doesn't fill up. **Trades and decisions are kept forever.**

**Setup (one time):**
1. Create an Ubuntu 24.04 server (DigitalOcean, Vultr, Linode, AWS Lightsail…) in Sydney or Singapore, and log in with `ssh root@YOUR-SERVER-IP`.
2. Install Docker: `curl -fsSL https://get.docker.com | sh`
3. Copy the project folder to the server. From PowerShell on this PC, in the folder that **contains** `crypto-desk`, run `scp -r crypto-desk root@YOUR-SERVER-IP:/root/`. You can delete `.venv` on the server afterwards because it isn't used there. Copying `data/desk.db` keeps your paper history; leave it out for a fresh start.
4. On the server: `cd /root/crypto-desk`, then `docker compose up -d --build`. The first build takes a few minutes.
5. Check: `docker compose ps` should show the bot as **healthy** after about 3 minutes. Run `docker compose logs -f bot` to watch it (Ctrl+C stops watching, not the bot).

**Tip:** log in with an SSH key instead of a password, and give the server a short name in your SSH settings (for example `crypto-desk`), so `ssh crypto-desk` is all you type. Once the server runs the bot, stop running `paper.bat` on your PC: only one copy of the bot should trade an account.

**Shortcuts on this PC** (double-click):

| File | What it does |
|---|---|
| `server_status.bat` | The server's containers + the same report as `status.bat` |
| `server_dashboard.bat` | Opens the server's dashboard in your browser (http://127.0.0.1:8502) through a secure tunnel. Leave its window open while you use the dashboard |

Telegram works the same wherever the bot runs.

**Everyday commands on the server** (log in with `ssh crypto-desk`, then `cd /root/crypto-desk`):

| Command | What it does |
|---|---|
| `docker compose ps` | Is everything running and healthy? |
| `docker compose logs -f bot` | Watch the bot's output |
| `docker compose run --rm bot python -m desk.status` | Same as `status.bat` |
| `docker compose run --rm bot python -m desk.control pause` | Same as `control.bat pause` (or use Telegram) |
| `docker compose run --rm bot python -m desk.tax_export --fy 2027` | Tax CSV into `reports/` |
| `docker compose restart bot` | After editing `config/config.yaml` |
| `docker compose up -d --build` | After copying new code over |
| `docker compose stop` | Stop everything (no alarm) |

**The dashboard from your PC:** it only listens on the server itself, so it's never exposed to the internet. `server_dashboard.bat` opens a secure tunnel for you. By hand, it's `ssh -L 8502:127.0.0.1:8501 crypto-desk` in PowerShell, then browse to http://127.0.0.1:8502.

**Backups:** everything that matters is `data/desk.db`. To take a copy, run `docker compose stop bot`, then `cp data/desk.db data/backup-$(date +%F).db`, then `docker compose start bot`. Download it to your PC with `scp root@YOUR-SERVER-IP:/root/crypto-desk/data/desk.db .`

**Important:**
- **Run only one bot per Telegram bot token.** If the PC bot and the server bot both run with the same token, Telegram refuses one of them. Stop `paper.bat` on the PC once the server is running.
- `.env` holds your keys. Keep the server's login secure, and **never** put the dashboard port on the public internet.
- **Updating the code later:** copy the changed files to `/root/crypto-desk/` on the server, then run `docker compose up -d --build` there. The bot stops cleanly and comes back with everything reloaded.
- Ubuntu installs security updates by itself. Very occasionally one needs a restart (`reboot`); the bot comes back on its own afterwards.

## 13. Prop firm research (Tradeify 247)

`prop_research.bat` replays the **Tradeify 247 2-Step** challenge (+10% then +5%, 3% daily loss, 6% max loss, $100K for $580) from every week of the last 6 years. It uses 9 of their top-10 coins, with their real fees and leverage, and shows how often the bot would pass, how long that takes, and what a funded account then pays out. Add `--compare` to also see the strategy families side by side. It takes a few minutes.

**What the research found (Oct 2026):**
- **Day trading doesn't work, even at their low fees.** Hourly breakouts were about break-even before costs, and dip-buying lost heavily.
- **What works is a daily "rotation":** each day, hold the 3 coins that have been strongest over the last 2 weeks, but only while Bitcoin is above its 50-day average. It makes about 12 trades a month, each typically held about 2 days. It made about +9.6% a year at a small 0.5% risk per trade, in both the tuning years and the 2 unseen years.
- **Challenge settings:**
  - 1% risk per trade
  - trades shrink as the account nears the loss floor
  - the bot stops for the day at −2%, before the firm's −3% limit
- **Challenge result:**
  - passed both phases from **59% of start dates** (46% with pessimistic costs)
  - typically took about **6 months**
  - **never** hit a loss limit
  - the attempts that failed simply hadn't reached the target after a year, and Tradeify has no deadline
- **Faster challenge setting:** hold the **2** strongest coins at **1.5%** risk. It passed both phases from 64% of start dates (62% with pessimistic costs), typically in about **4.5 months**, with 43% passing within 6 months. In about 1 in 8 attempts it hit a loss limit. Once funded, switch back to top 3 / 1%, which essentially never lost the account.
- **Funded $100K account:** about **$500–600 a month** to you on average. With the firm's ~$300K cap, that's about 3 accounts, which all trade the same way.

All of this is a simulation of the past, and Tradeify 247's accounts are themselves simulated, with payouts at their discretion.

## 14. Prop mode: the bot on Tradeify 247

With `prop: enabled: true` in `config/config.yaml`, the bot trades the prop account(s) instead of CoinSpot. It uses the strategy from section 13. `paper.bat` and the server start it the same way as before.

**What it does by itself:**
- **Every minute:**
  - checks each account
  - at **−2% on the day** (the firm's limit is 3%), closes everything and rests until the firm's new day at 22:00 UTC
  - when the **phase target** is reached, closes everything and stops trading that account
  - records any position the platform's stop-loss has closed
- **Every hour:** moves trailing stops up (never down) and records equity.
- **22:00 UTC, the firm's new day:**
  - sets a new daily starting point and lifts the brake
  - charges the overnight fee (simulated account only; the real platform charges its own)
  - sends a daily summary
  - on a funded account, tells you when profit can be withdrawn
- **00:00 UTC, the rotation:**
  - sells coins that dropped out of the top 3
  - buys the new strongest coins (only those actually rising, and only while Bitcoin is above its 50-day average)
  - sizes each buy so a stop-out loses 1.5% of the account (the challenge setting), and less when the account is near the 6% loss floor
  - every new trade gets a stop-loss **order on the platform**, so it's protected even if the bot is off
- **AI review** (`ai_review`):
  - `shadow` (the default): the AI gives an opinion on each buy and it's recorded, but it never blocks
  - `veto`: the AI can skip a buy
  - The AI only vetoes for concrete problems such as a hack, delisting or legal action, never for "it went up a lot".
- **Switches:** `/pause`, `/resume`, `/kill`, `/kill close` on Telegram (and `control.bat`) work as before. `/status` and `/positions` show the prop accounts.

**Simulated or real** (`broker`):
- `paper` (now): a simulated Tradeify 247 $100K account with their real fees, swap, spreads and leverage, using live prices. It's a free forward test.
- `dxtrade` (R3): your real account(s). Put these in `.env`:
  - `DXTRADE_URL` (the API address; ask Tradeify 247 support for their "DXtrade REST API URL")
  - `DXTRADE_USERNAME`, `DXTRADE_PASSWORD`, `DXTRADE_DOMAIN` (usually `default`)
  - optionally `DXTRADE_ACCOUNTS`

  Then set `broker: dxtrade` and `account_size` to your account's size, and restart. With several accounts on one login, each gets the same trades, sized to itself.

**Tradeify 247's platform, as tested:**
- API address: `https://dx.tradeify247.co/dxsca-web`, domain `default`, every coin has a 0.01 order size step.
- Each position has its own ID, which the bot names when closing it or placing its stop-loss.
- The platform's "close all" command doesn't work there, so the bot closes positions one by one.
- A full test cycle on 0.01 ETH (buy, stop-loss, move the stop, close) costs a few cents.

**When you pass a phase:**
1. The bot stops trading that account and sends you a message.
2. Once Tradeify moves you on, set `phase: "2"` (then `"funded"`) in `config.yaml`, and update the login if Tradeify gives you a new one.
3. Restart the bot (`docker compose restart bot` on the server).

**Back to CoinSpot:** set `enabled: false` and restart.

## 15. Files and folders

```
config/config.yaml   all settings
prompts/             the AI agents' instructions (editable text files)
.env                 your secret keys (never share)
desk/                the bot's code
tests/               automated tests
data/desk.db         database: every price check, scan, headline, AI call, decision and trade
logs/                log files (rotated automatically, max ~60 MB)
reports/             backtest reports and tax CSVs
Dockerfile, docker-compose.yml, docker/   running on a server (section 12)
desk/prop/           prop firm: rules, strategies, simulator, engine, brokers (sections 13-14)
desk/exchange/dxtrade.py   connection to the DXtrade platform (Tradeify 247)
```

## Troubleshooting

- **"Please run setup.bat first"**: run `setup.bat`.
- **`check.bat` shows a FAILED Binance row**: some countries and hosting regions block Binance. The bot switches to Kraken automatically. If both fail, check your internet connection.
- **"CONFIG ERROR"**: the message names the setting and line that's wrong in `config/config.yaml`.

- **No Telegram messages**: run `telegram_setup.bat`. If it says it can't find your message, open your bot in Telegram, press **Start**, send "hi", and run it again. Check that `TELEGRAM_CHAT_ID=` in `.env` now has a number.
- **"Another copy of the bot is already running"**: a `paper.bat` window is already open (check the taskbar), or another bot is using this database. Only one copy may run at a time.
- **Docker shows the bot as "unhealthy" or restarting**: run `docker compose logs --tail 100 bot`. The last lines say why (often a typo in `config.yaml` or `.env`).

*(Switching to live trading will be added here in Stage 9.)*
