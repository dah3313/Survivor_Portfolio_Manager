# Survivor Portfolio Manager (SPM)

## Purpose

The Survivor Portfolio Manager is an automated income-generation system for a Roth IRA held at Interactive Brokers. It is designed to provide a surviving spouse with reliable monthly income from an investment portfolio, requiring zero manual intervention after activation.

The portfolio transitions from a growth-oriented strategy (managed by a separate program, the IPM) into a structured withdrawal system. The SPM sells assets on a fixed schedule, raises cash for monthly ACH transfers, and protects the portfolio during market downturns using a crisis buffer and rules-based circuit breakers.

**This program manages real money. Every design decision prioritizes safety and auditability over cleverness.**

---

## How It Works (Plain English)

The portfolio holds four funds split into two buckets, plus a cash-like safety buffer:

- **Growth** (50% of core): FBCG and AVUV — equity funds that drive long-term appreciation.
- **Fixed Income** (50% of core): PIMIX and JPIE — bond funds that generate yield and provide relative stability.
- **Crisis Buffer** (separate): SGOV — a short-term Treasury fund holding roughly $72,000, kept outside the core so it doesn't interfere with balance calculations.

Each month, the program sells enough from the Fixed Income bucket to cover the $5,000 withdrawal (adjusted annually for inflation). The cash settles in the brokerage account and IBKR's automated ACH system transfers it to the linked bank account.

If the stock market drops significantly (measured by the S&P 500 falling below its 200-day moving average), the program shifts withdrawals to the SGOV buffer instead, leaving the core portfolio untouched so it can recover. Once the market recovers, withdrawals return to Fixed Income and dividends refill the buffer.

Every November, the program checks whether to apply an inflation raise and whether the Growth bucket earned a one-time bonus payout from an exceptionally strong year.

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│  main.py — Orchestrator                         │
│  Loads state, coordinates all modules, logs     │
│  every decision to an append-only audit trail.  │
├────────────┬────────────┬───────────┬───────────┤
│ strategy.py│portfolio.py│ibkr_client│  alert.py │
│ Market     │ Balance    │ IBKR API  │ Email/SMS │
│ regime     │ tracking,  │ connection│ alerts    │
│ evaluation │ drift,     │ prices,   │           │
│            │ sell-order │ orders    │           │
│            │ routing    │           │           │
├────────────┴────────────┴───────────┴───────────┤
│  config.py — All constants, thresholds, tickers │
└─────────────────────────────────────────────────┘
```

---

## Module Reference

### config.py

All tunable parameters in one place. Nothing is hardcoded elsewhere. Key settings:

- **Portfolio tickers and target allocation** (50/50 Growth vs. Fixed Income)
- **SGOV buffer target** ($72,000)
- **Withdrawal baseline** ($5,000/month)
- **Circuit breaker thresholds** (-5% halt rebalancing, -7.5% enter crisis, +3% recovery)
- **Inflation rate** (3% annual, frozen if market is down ≥5% vs. 12-month SMA)
- **November bonus rules** (20% of excess above 25% YoY growth return)
- **Safety cap** ($15,000 max single trade — prevents bugs from liquidating the account)
- **File paths** for logs, audit trail, and persistent state

### ibkr_client.py

Handles all communication with Interactive Brokers via the `ib_insync` library.

- `get_portfolio_state()` — Returns a dictionary of current market values for every tracked ticker. SGOV is included but the caller keeps it separate from core calculations.
- `get_price_and_sma(symbol, duration, bar_size)` — Returns both the current price and the SMA of the same symbol. This is the fix for the critical bug in the original code, which compared portfolio dollar values against an index share price.
- `sell_dollar_amount(symbol, amount, dry_run)` — Submits a fractional-share market sell order using IBKR's `cashQty` parameter. Enforces the safety cap. In dry-run mode, logs the order without executing.

### strategy.py

Pure evaluation logic — no side effects, no file I/O. Receives market data, returns decisions.

- `evaluate_circuit_breakers(proxy_price, proxy_sma_200)` — Compares SPY's current price to its own 200-day SMA. Returns two flags: whether to halt rebalancing (-5%) and whether to force withdrawals from the buffer (-7.5%). Tracks the transition price so recovery can be calculated as +3% above the entry point.
- `evaluate_inflation_freeze(proxy_price, proxy_sma_12mo)` — Should the annual inflation raise be skipped? Compares SPY price to its 12-month SMA.
- `evaluate_november_bonus(current_growth, prev_year_growth)` — Calculates the one-time bonus if the Growth bucket returned more than 25% YoY.

### portfolio.py

Tracks live balances and generates trade instructions.

- `get_drift()` — Computes the current Growth allocation and checks it against the 5/25 drift bands (5 percentage-point absolute, 25% relative).
- `generate_rebalance_trades()` — If drift is detected and rebalancing is not halted, calculates the sell orders needed to restore the 50/50 split. Currently generates sell-side only; the cash is deployed on a subsequent run.
- `route_cash_raising(target, force_buffer)` — The withdrawal hierarchy: SGOV → Fixed Income → Growth. Returns a list of (ticker, dollar_amount) sell orders.

### alert.py

Sends notifications via email (full detail) and SMS (short summary via Verizon's email-to-text gateway).

- `send_success()` — Called after a clean run.
- `send_error()` — Called if the program crashes. Email gets the full traceback; SMS gets a one-liner.
- `send_heartbeat()` — Confirms the host machine is alive. Run on a separate timer so that if SPM itself fails to execute, you still know the machine is up.
- Credentials are loaded from environment variables, never stored in code.

### main.py

The orchestrator. Runs the full pipeline in order:

1. Load persistent state from disk
2. Connect to IBKR and snapshot portfolio balances
3. Fetch SPY price and SMA data
4. Evaluate circuit breakers
5. Check drift and generate rebalance trades (if not halted)
6. In November: evaluate inflation adjustment and bonus
7. Calculate and execute monthly cash-raising sells
8. Save state, write audit log, send alert

Supports `--dry-run` (full logic, no trades) and `--heartbeat` (send alive signal and exit).

---

## Execution Schedule

The program is run by `systemd` timers on a dedicated Linux host.

| Timer | Frequency | Command | Purpose |
|-------|-----------|---------|---------|
| Weekly check | Every Monday 9:30 AM ET | `python main.py --dry-run` | Evaluate drift and circuit breakers, log state. No trades. |
| Monthly withdrawal | 3 business days before ACH date | `python main.py` | Raise cash for the monthly transfer. Executes trades. |
| Heartbeat | Every 6 hours | `python main.py --heartbeat` | Confirm the host is alive. |

The weekly dry-run ensures you always have fresh SMA data and drift status in the audit log even in months where no action is needed.

---

## Deployment

### Prerequisites

- Python 3.10+
- `ib_insync` (`pip install ib_insync`)
- IB Gateway running on the same host (port 4001 for live, 4002 for paper)
- IBKR account with API access enabled and the Roth IRA linked

### Environment Variables

Set these in the systemd unit file or a sourced env file (never in the code):

```bash
export SPM_SMTP_SERVER="smtp.gmail.com"
export SPM_SMTP_PORT="587"
export SPM_EMAIL_SENDER="your_bot_email@gmail.com"
export SPM_EMAIL_PASSWORD="your_gmail_app_password"
export SPM_EMAIL_RECIPIENT="your_personal_email@gmail.com"
export SPM_SMS_GATEWAY="5551234567@vtext.com"
```

### Directory Structure

```
/home/spm/
├── spm/
│   ├── config.py
│   ├── main.py
│   ├── strategy.py
│   ├── portfolio.py
│   ├── ibkr_client.py
│   └── alert.py
├── spm_state.json          ← persistent state between runs
/var/log/spm/
├── spm.log                 ← human-readable log
└── spm_audit.jsonl         ← structured audit trail (append-only)
```

### First Run

```bash
# Paper trading first — always
# Set IBKR_PORT=4002 in config.py for paper trading

# Dry run to verify connectivity and logic
python main.py --dry-run

# Check the audit log
cat /var/log/spm/spm_audit.jsonl | python -m json.tool

# Verify alerts
python main.py --heartbeat
```

---

## IPM → SPM Switchover

The IPM (Investment Portfolio Manager) and SPM manage the same Roth IRA holdings at IBKR. Only one runs at a time.

A desktop shortcut on the Windows machine opens a remote session to the Linux host and runs a switchover script that:

1. Stops the IPM systemd timers
2. Disables the IPM service
3. Enables and starts the SPM systemd timers
4. Sends a confirmation alert: "SPM is now active"

The switchover script will be written after both portfolio managers are complete.

---

## Redundancy

Two fanless mini-PCs run in parallel with daily state synchronization. If the primary host goes silent (no heartbeat for 24 hours), the secondary takes over automatically. The sync mechanism and failover logic will be designed after the core software is stable.

---

## Safety Features

- **Max single trade cap** ($15,000): No single sell order can exceed this amount, regardless of what the logic calculates. A bug that tries to sell $350,000 will be capped and logged as an error.
- **Dry-run mode**: The full pipeline runs and logs every decision, but no orders are submitted to IBKR.
- **Structured audit trail**: Every run appends timestamped JSON records covering portfolio snapshots, SMA values, circuit breaker results, trade orders, and state changes. This is the forensic record.
- **Alert on failure**: If the program crashes for any reason, an error alert is sent with the full traceback. Alert failures themselves never crash the program.
- **Heartbeat monitoring**: A separate timer confirms the host is alive, independent of whether SPM ran successfully.
- **Buffer isolation**: SGOV is tracked but excluded from core balance and drift calculations so it never triggers false rebalancing.

---

## Audit Trail Format

Each line in `spm_audit.jsonl` is a standalone JSON object:

```json
{"timestamp": "2026-04-30T09:31:00", "event": "run_start", "dry_run": false, "month": 4}
{"timestamp": "2026-04-30T09:31:02", "event": "portfolio_snapshot", "core_balance": 700000.0, "growth_balance": 350000.0, "fi_balance": 350000.0, "buffer_balance": 72000.0}
{"timestamp": "2026-04-30T09:31:03", "event": "sma_data", "proxy": "SPY", "price_200": 520.50, "sma_200": 510.30}
{"timestamp": "2026-04-30T09:31:03", "event": "circuit_breakers", "halt_rebalancing": false, "force_buffer": false}
{"timestamp": "2026-04-30T09:31:04", "event": "cash_raising", "target": 5000.0, "force_buffer": false, "orders": [["PIMIX", 3125.0], ["JPIE", 1875.0]]}
{"timestamp": "2026-04-30T09:31:06", "event": "run_complete"}
```

This format is human-readable, machine-parseable, and trivially searchable with `grep` or `jq`.

---

## What's Not Yet Built

- **Buy-side rebalancing**: The sell side generates cash to restore 50/50 balance, but the code to deploy that cash into the underweight bucket is not yet written.
- **Dividend capture and routing**: Post-crisis, dividends from all four core funds should refill the SGOV buffer. This requires subscribing to IBKR dividend events or polling account transactions.
- **Systemd unit files and timers**: The service definitions for both SPM and the heartbeat timer.
- **IPM ↔ SPM switchover script**: The one-click script that stops one manager and starts the other.
- **Secondary host failover**: The sync and automatic promotion logic for the redundant mini-PC.

---

## Financial Context

This system exists because military retirement pay and VA disability benefits do not transfer to a surviving spouse. If the servicemember dies before Social Security eligibility, the spouse loses roughly $4,500/month in household income. The SPM converts a Roth IRA into a structured income floor to bridge that gap until Social Security survivor benefits begin, and to supplement them afterward.

The portfolio is not an experiment. It is a lifeline.
