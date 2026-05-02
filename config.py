# config.py — Survivor Portfolio Manager (SPM)
# ============================================
# This program manages automated monthly income withdrawals from a Roth IRA
# held at Interactive Brokers. It is designed to run unattended on a dedicated
# Linux host under systemd, producing income for a surviving spouse.

# --- IBKR Connection ---
IBKR_HOST = '127.0.0.1'
IBKR_PORT = 4001        # IB Gateway (live). Paper: 4002
IBKR_CLIENT_ID = 10     # Unique per concurrent connection

# --- Portfolio Definition ---
# Core holdings — the income-generating engine.
# Growth bucket (50% of core target)
TICKERS_GROWTH = ['FBCG', 'AVUV']
# Fixed-income bucket (50% of core target)
TICKERS_FI = ['PIMIX', 'JPIE']
# All core tickers for iteration convenience
CORE_TICKERS = TICKERS_GROWTH + TICKERS_FI

# Crisis buffer — held in the same account but tracked separately.
# NOT included in core balance or drift calculations.
TICKER_BUFFER = 'SGOV'
BUFFER_TARGET_DOLLARS = 72_000.0

# Target allocation within the core (must sum to 1.0)
TARGET_ALLOCATION_GROWTH = 0.50
TARGET_ALLOCATION_FI = 0.50

# --- Withdrawal ---
BASELINE_MONTHLY_WITHDRAWAL = 5_000.0

# --- Proxy Index for SMA Calculations ---
# We compare the proxy's current price to its own SMA to gauge market regime.
# This avoids the apples-to-oranges bug of comparing portfolio dollars to
# an index price.
PROXY_INDEX_TICKER = 'SPY'

# --- Rebalancing Drift Bands (5/25 Rule) ---
# Checked weekly. Rebalancing is halted when the 200-day SMA circuit breaker
# is tripped.
REBALANCE_BAND_ABSOLUTE = 0.05   # 5 percentage-point drift
REBALANCE_BAND_RELATIVE = 0.25   # 25% relative drift from target

# --- SMA Lookback Periods ---
# ib_insync reqHistoricalData uses calendar durations.
# 200 trading days ≈ 40 weeks of weekly bars.
SMA_200_PERIOD = '40 W'
SMA_200_BAR = '1 week'
# 12 calendar months ≈ 52 weeks.
SMA_12MO_PERIOD = '52 W'
SMA_12MO_BAR = '1 week'

# --- Cash Buffer (ACH Backup) ---
# Maintained outside of rebalancing and core calculations.
CASH_BUFFER_TARGET = 6000.0
CASH_TICKER = 'USD'

# --- Buffer Refill Mechanics ---
# After crisis mode recovery, wait 60 days before starting to refill SGOV.
# Refill rate is 8.3% per year (approx 0.69% per month), taken from overweight.
BUFFER_REFILL_DELAY_DAYS = 60
BUFFER_REFILL_ANNUAL_RATE = 0.083

# --- Circuit Breaker Thresholds ---
# These compare SPY's current price vs. its own 200-day SMA.
# -5%: halt all rebalancing
HALT_REBALANCE_THRESHOLD = -0.05
# -7.5%: shift monthly withdrawals from FI to SGOV buffer
SHY_TRANSITION_THRESHOLD = -0.075
# Recovery: exit crisis when SPY is 3% above the price at which we entered
RECOVERY_ABOVE_TRANSITION = 0.03

# --- Inflation Guardrails (Annual, November) ---
ANNUAL_INFLATION_RATE = 0.03
# If core is down ≥5% vs 12-month SMA, skip inflation adjustment
INFLATION_FREEZE_THRESHOLD = -0.05

# --- November Special Dividend ---
BONUS_EVAL_MONTH = 11
BONUS_GROWTH_YOY_THRESHOLD = 0.25   # 25% YoY growth bucket return
BONUS_EXCESS_TAKE_RATE = 0.20       # take 20% of excess above threshold

# --- Safety Rails ---
# Maximum dollar amount a single trade can execute. Prevents a bug from
# liquidating the entire account in one order.
MAX_SINGLE_TRADE_DOLLARS = 15_000.0

# --- Logging ---
LOG_DIR = '/var/log/spm'
LOG_FILE = 'spm.log'
AUDIT_FILE = 'spm_audit.jsonl'   # append-only structured audit trail

# --- State Persistence ---
STATE_FILE = '/home/spm/spm_state.json'
