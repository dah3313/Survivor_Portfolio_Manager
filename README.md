# Survivor Portfolio Manager (SPM)

## Purpose

The Survivor Portfolio Manager is an automated income-generation system for a Roth IRA held at Interactive Brokers. It is designed to provide a surviving spouse with reliable monthly income from an investment portfolio, requiring zero software intervention after activation.

The portfolio transitions from a growth-oriented strategy (managed by a separate program, the IPM) into a structured withdrawal system. The SPM sells assets on a fixed schedule, raises cash for monthly ACH transfers, and protects the portfolio during market downturns using a crisis buffer and rules-based circuit breakers.

**This program manages real money. Every design decision prioritizes safety, physical security, and auditability over cleverness.**

---

## How It Works (Plain English)

The portfolio holds four funds split into two core buckets, plus a cash-like safety buffer:

- **Growth** (50% of core): FBCG and AVUV — equity funds that drive long-term appreciation.
- **Fixed Income** (50% of core): PIMIX and JPIE — bond funds that generate yield and provide relative stability.
- **Crisis Buffer** (separate): SGOV — a short-term Treasury fund holding roughly $72,000, kept outside the core so it doesn't interfere with balance calculations.

**Dividends:** All dividends are configured at the broker level to automatically reinvest (DRIP). The SPM does not micromanage dividend sweeps.

Each month, the program sells enough from the Fixed Income bucket to cover the withdrawal (adjusted annually for inflation). The cash settles in the brokerage account and IBKR's automated ACH system transfers it to the linked bank account.

**The Circuit Breaker:** The system calculates a "Synthetic Index" mirroring the exact daily price of the Growth bucket (FBCG + AVUV). If this index drops significantly below its 200-day moving average, the program shifts withdrawals to the SGOV buffer instead, leaving the core portfolio untouched so it can recover. Once the market recovers, withdrawals return to Fixed Income.

Every November, the program checks whether to apply an inflation raise and whether the Growth bucket earned a one-time bonus payout from an exceptionally strong year.

---

## Architecture & The Hardware Token

The SPM and the IPM (Investment Portfolio Manager) are hosted on parallel, dedicated Linux mini-PCs. They are stateless, executing via `systemd` timers rather than continuous loops to survive broker API resets and power outages.

The entire system is secured by a **Hardware Token Protocol** (a physical USB drive mounted at `/mnt/usb/` containing `spm_token.json`). 
* The active IPM writes the inflation-adjusted withdrawal baseline to this token continuously. 
* The dormant SPM requires this token to activate live trading. 

  ┌─────────────────────────────────────────────────┐
  │  main.py — Orchestrator                         │
  │  Validates USB Token, coordinates modules,      │
  │  and writes to an append-only audit trail.      │
  ├────────────┬────────────┬───────────┬───────────┤
  │ strategy.py│portfolio.py│ibkr_client│  alert.py │
  │ Synthetic  │ Balance    │ IBKR API  │ Email/SMS │
  │ index eval,│ tracking,  │ routing   │ heartbeat │
  │ safeguards │ T+1 trades │           │ alerts    │
  ├────────────┴────────────┴───────────┴───────────┤
  │  config.py — All constants, thresholds, tickers │
  └─────────────────────────────────────────────────┘

# The Switchover Protocol (In Case of husband passing)

There are no scripts to run, no SSH keys to manage, and no remote desktops to navigate. The switchover is entirely physical.

Locate the active IPM mini-PC (the black box) and the dormant SPM mini-PC (the silver box). Both should be powered on and connected to the internet.

Unplug the USB thumb drive from the IPM box.

Plug the USB thumb drive into the SPM box.

What happens under the hood:

The IPM (Primary Porfolio Manager) Fails Closed: At its next scheduled run, the IPM will notice the USB token is missing. It will instantly crash, halting all growth-phase trades and silencing its weekly heartbeat text.

The SPM Latches Live: At its next scheduled run, the SPM will detect the USB token. It reads the exact, current inflation-adjusted withdrawal amount from the token, saves it to its permanent internal state (is_live_latched = True), and immediately takes over management of the Roth IRA.


## Module Reference

**config.py**

All tunable parameters. Nothing is hardcoded elsewhere.
* Synthetic Index Tickers: Ties the circuit breakers directly to the assets taking the risk (FBCG/AVUV).
* SGOV buffer target: ($72,000)
* Withdrawal baseline: ($5,000/month)
* Circuit breaker thresholds: (-5% halt rebalancing, -7.5% enter crisis, +3% recovery)
* Safety cap: ($15,000 max single trade — prevents bugs from liquidating the account)

**ibkr_client.py**

Handles all communication with Interactive Brokers via the `ib_insync` library. 
* `connect()` — Includes a 3-attempt retry loop to survive transient network drops or the mandatory 24-hour IB Gateway resets.
* `get_synthetic_price_and_sma()` — Fetches historical bars for multiple tickers, securely aligns them by valid trading dates, and calculates the blended price and SMA.
* `sell_dollar_amount()` / `buy_dollar_amount()` — Submits fractional-share market orders. Enforces the $15k safety cap. **Includes a 60-second timeout with automatic cancellation of dangling orders if the market halts or liquidity dries up.**

**strategy.py**

Pure evaluation logic — no side effects. Receives synthetic market data, returns decisions for circuit breakers, inflation freezes, and November bonuses.

**portfolio.py**

Tracks live balances and generates trade instructions.
* `generate_rebalance_trades()` — Generates SELL orders if the 50/50 allocation drifts beyond the 5/25 safety bands. Generates BUY orders to deploy settled cash (T+1) into the underweight bucket.
* `route_cash_raising()` — The withdrawal hierarchy: SGOV → Fixed Income → Growth.
* `route_buffer_refill_sells()` — Calculates the exact monthly siphon from core assets to rebuild the SGOV buffer after a crisis.

**alert.py**

Sends notifications via email (full detail) and SMS (short summary via email-to-text gateway). Includes the critical 6-hour `send_heartbeat()` to act as a dead-man's switch if the host loses power.

**main.py**

The orchestrator.
* Evaluates the USB Hardware Token (Latching logic).
* Connects to IBKR and snapshots portfolio balances.
* Fetches synthetic SMA data.
* Evaluates circuit breakers.
* Executes T+1 rebalance buys and drift-correction sells.
* Calculates and executes monthly cash-raising sells.
* **Atomically saves state** (guaranteed file integrity against power loss), writes audit log, and sends alerts.

## Execution Schedule

The program is run by systemd timers on a dedicated Linux host.
Timer Frequency, Command & Purpose
Weekly check Every Monday 9:30 AM ET - Evaluate drift, circuit breakers, and deploy settled T+1 cash.
Monthly withdrawal 3 business days before ACH date - Raise cash for the monthly transfer.	
Heartbeat Every 6 hours	--heartbeat - Confirm the host is alive and report Latch Status.

Note: Without the USB token inserted, the Weekly and Monthly runs default to Dry-Run (paper) mode.
Deployment & State Integrity
Directory Structure
Plaintext

/home/spm/
├── spm/		Source code
├── spm_state.json	Persistent internal state
/var/log/spm/
├── spm.log		Human-readable log
└── spm_audit.jsonl	Structured audit trail
/mnt/usb/
└── spm_token.json	The Hardware Token

### Safety Features

* **The Hardware Latch:** The system defaults to inert paper-trading. It must be physically authorized via USB to execute real trades.
* **Max Single Trade Cap ($15,000):** No single order can exceed this amount.
* **Fail Closed Architecture:** If IBKR is permanently offline, or the machine loses power, the script crashes cleanly and does nothing. The baseline USD cash buffer in the account absorbs the impact of missed runs for the automated ACH pull.
* **Network Resilience:** The connection protocol uses automatic retries with backoffs to gracefully wait out broker resets or transient internet drops.
* **Dangling Order Protection:** If a trade fails to fill within 60 seconds (e.g., due to a market halt), the system explicitly cancels the live order to prevent it from executing hours later and destroying settlement assumptions.
* **Atomic State Persistence:** The internal state file is written to a temporary location and atomically swapped, guaranteeing the system never wakes up to a corrupted or half-written memory state after an unexpected power loss. 
* **Structured Audit Trail:** Every run appends timestamped JSON records covering portfolio snapshots, SMA values, trade orders, and state changes.

What's Not Yet Built

Systemd unit files and timers: The Linux service definitions for both SPM and the heartbeat timer.

Headless Gateway Wrapper: Configuration of IBC (IBController) to handle Interactive Brokers' mandatory 24-hour resets and headless authentication.

Financial Context

The SPM converts a Roth IRA into a structured income floor to bridge the gap provide by the husbands income after his death until Social Security survivor benefits begin, and to supplement them afterward.

The portfolio is not an experiment. It is a lifeline.
