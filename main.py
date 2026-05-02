# main.py — Survivor Portfolio Manager (SPM)
# ============================================
# Top-level orchestrator.  Runs once per scheduled execution (weekly for
# rebalancing checks, monthly for cash raising).
#
# Usage:
#   python main.py                  # live execution (requires USB token or latched state)
#   python main.py --dry-run        # log everything but execute no trades
#   python main.py --heartbeat      # send a heartbeat alert and exit

import argparse
import datetime
import json
import logging
import os
import sys

import config
from alert import AlertManager
from ibkr_client import IBKRClient
from portfolio import Portfolio
from strategy import Strategy

# ------------------------------------------------------------------
# Logging setup
# ------------------------------------------------------------------
os.makedirs(config.LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s — %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(config.LOG_DIR, config.LOG_FILE)),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger('spm.main')


# ------------------------------------------------------------------
# Structured audit log (append-only JSONL)
# ------------------------------------------------------------------
def audit_log(event_type, data):
    """Append a structured JSON record to the audit trail."""
    record = {
        'timestamp': datetime.datetime.now().isoformat(),
        'event': event_type,
        **data,
    }
    path = os.path.join(config.LOG_DIR, config.AUDIT_FILE)
    try:
        with open(path, 'a') as f:
            f.write(json.dumps(record) + '\n')
    except Exception as e:
        logger.error('Failed to write audit log: %s', e)


# ------------------------------------------------------------------
# Persistent state & Hardware Token
# ------------------------------------------------------------------
def load_state():
    if os.path.exists(config.STATE_FILE):
        with open(config.STATE_FILE, 'r') as f:
            return json.load(f)
    return {
        'current_monthly_withdrawal': config.BASELINE_MONTHLY_WITHDRAWAL,
        'in_buffer_transition': False,
        'transition_price': None,
        'last_november_growth_value': 0.0,
        'is_live_latched': False,
        'recovery_date': None,       
        'sgov_target_dollars': config.BUFFER_TARGET_DOLLARS,
        'last_idle_heartbeat_month': 0  # NEW: Tracks the monthly dormant ping
    }

def save_state(state):
    os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)
    with open(config.STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)
    logger.info('State saved to %s', config.STATE_FILE)


def evaluate_hardware_token(state, cmd_line_dry_run):
    """
    Evaluates the presence of the USB token.
    Latches the system to LIVE mode if the token is found.
    Returns: (updated_state, effective_dry_run_flag)
    """
    token_path = '/mnt/usb/spm_token.json'

    # If the user passed --dry-run via command line, honor it regardless of latch.
    if cmd_line_dry_run:
        logger.info("Command line --dry-run overrides hardware token logic.")
        return state, True

    # SCENARIO A: Already latched from a previous run.
    if state.get('is_live_latched', False):
        logger.info("System is LIVE LATCHED from a previous token insertion.")
        return state, False 

    # SCENARIO B: Not latched, but the token has been inserted!
    if os.path.exists(token_path):
        try:
            with open(token_path, 'r') as f:
                token_data = json.load(f)
            
            # 1. Ingest the baseline data from the IPM's token
            state['current_monthly_withdrawal'] = token_data.get(
                'current_monthly_withdrawal', 
                state['current_monthly_withdrawal']
            )
            state['last_november_growth_value'] = token_data.get(
                'last_november_growth_value', 
                state.get('last_november_growth_value', 0.0)
            )
            state['sgov_target_dollars'] = token_data.get(
                'sgov_target_dollars', 
                state.get('sgov_target_dollars', config.BUFFER_TARGET_DOLLARS)
            )
            
            # 2. LATCH THE SYSTEM
            state['is_live_latched'] = True
            
            logger.critical("*** HARDWARE TOKEN DETECTED. SYSTEM LATCHED TO LIVE TRADING. ***")
            audit_log('system_latched', {'token_data': token_data})
            return state, False # False means live execution

        except Exception as e:
            logger.error("USB token found but unreadable! Defaulting to dry-run. Error: %s", e)
            return state, True # Force dry-run for safety

    # SCENARIO C: Not latched, no token. Sentinel mode.
    logger.info("No hardware token found. Forcing DRY-RUN mode.")
    return state, True


# ------------------------------------------------------------------
# Core execution
# ------------------------------------------------------------------
def run_spm(cmd_line_dry_run=False):
    state = load_state()
    
    # Check the token and determine actual execution mode
    state, effective_dry_run = evaluate_hardware_token(state, cmd_line_dry_run)

    client = IBKRClient()
    now = datetime.datetime.now()
    current_month = now.month

    logger.info('========== SPM RUN START (effective_dry_run=%s) ==========', effective_dry_run)
    audit_log('run_start', {'effective_dry_run': effective_dry_run, 'month': current_month})

    client.connect()

    try:
        # ---- 1. Gather live data ----
        live_balances = client.get_portfolio_state()
        portfolio = Portfolio(live_balances)

        audit_log('portfolio_snapshot', {
            'core_balance': portfolio.core_balance,
            'growth_balance': portfolio.growth_balance,
            'fi_balance': portfolio.fi_balance,
            'buffer_balance': portfolio.buffer_balance,
            'balances': live_balances,
        })

        # ---- 2. Fetch synthetic proxy index SMA data ----
        # 200-day SMA (for circuit breakers)
        proxy_price_200, sma_200 = client.get_synthetic_price_and_sma(
            config.SYNTHETIC_INDEX_TICKERS,
            config.SMA_200_PERIOD,
            config.SMA_200_BAR,
        )

        # 12-month SMA (for inflation freeze — only needed in November)
        proxy_price_12mo, sma_12mo = client.get_synthetic_price_and_sma(
            config.SYNTHETIC_INDEX_TICKERS,
            config.SMA_12MO_PERIOD,
            config.SMA_12MO_BAR,
        )

        audit_log('sma_data', {
            'proxy': 'SYNTHETIC_GROWTH',
            'proxy_tickers': config.SYNTHETIC_INDEX_TICKERS,
            'price_200': proxy_price_200,
            'sma_200': sma_200,
            'price_12mo': proxy_price_12mo,
            'sma_12mo': sma_12mo,
        })

        # ---- 3. Evaluate circuit breakers ----
        strategy = Strategy(
            in_buffer_transition=state['in_buffer_transition'],
            transition_price=state['transition_price'],
        )
        halt_rebalancing, force_buffer = strategy.evaluate_circuit_breakers(
            proxy_price_200, sma_200,
        )

        # Detect transition changes to set/clear the Recovery Clock
        if state['in_buffer_transition'] and not force_buffer:
            state['recovery_date'] = now.isoformat()
            logger.info("Crisis mode exited. Recovery clock started at %s", state['recovery_date'])
            audit_log('recovery_started', {'date': state['recovery_date']})
        
        if not state['in_buffer_transition'] and force_buffer:
            state['recovery_date'] = None # Clear the clock if we dip back into crisis

        # Persist transition state
        state['in_buffer_transition'] = strategy.in_buffer_transition
        state['transition_price'] = strategy.transition_price

        audit_log('circuit_breakers', {
            'halt_rebalancing': halt_rebalancing,
            'force_buffer': force_buffer,
            'in_buffer_transition': strategy.in_buffer_transition,
            'transition_price': strategy.transition_price,
        })

        # ---- 4. Weekly Rebalancing & Refill Logic ----
        
        # Determine if Refill Mode is active
        refill_active = False
        if not strategy.in_buffer_transition and state.get('recovery_date'):
            recovery_date = datetime.datetime.fromisoformat(state['recovery_date'])
            days_since_recovery = (now - recovery_date).days
            if days_since_recovery >= config.BUFFER_REFILL_DELAY_DAYS:
                refill_active = True

        if not halt_rebalancing:
            # 4a. Execute Drift Sells & Cash Deployment Buys (SGOV gets first claim if refill_active)
            rebal_trades = portfolio.generate_rebalance_trades(
                sgov_target=state['sgov_target_dollars'], 
                refill_active=refill_active
            )
            if rebal_trades:
                audit_log('rebalance_and_deploy_trades', {'trades': rebal_trades})
                for direction, ticker, amount in rebal_trades:
                    if direction == 'SELL':
                        logger.info('Rebalance SELL: %s $%.2f', ticker, amount)
                        client.sell_dollar_amount(ticker, amount, dry_run=effective_dry_run)
                    elif direction == 'BUY':
                        logger.info('Rebalance/Refill BUY: %s $%.2f', ticker, amount)
                        client.buy_dollar_amount(ticker, amount, dry_run=effective_dry_run)

            # 4b. Execute Buffer Refill Sells (if we need to raise more cash for SGOV)
            if refill_active:
                refill_sells = portfolio.route_buffer_refill_sells(
                    sgov_target=state['sgov_target_dollars'],
                    monthly_refill_rate=config.BUFFER_REFILL_ANNUAL_RATE
                )
                if refill_sells:
                    audit_log('buffer_refill_sells', {'trades': refill_sells})
                    for ticker, amount in refill_sells:
                        logger.info('Buffer Refill SELL: %s $%.2f', ticker, amount)
                        client.sell_dollar_amount(ticker, amount, dry_run=effective_dry_run)
        else:
            logger.info('Rebalancing & Refills HALTED by 200-day SMA circuit breaker')
            audit_log('rebalance_halted', {})

        # ---- 5. Monthly cash raising target prep ----
        target_withdrawal = state['current_monthly_withdrawal']

        # ---- 6. November annual review ----
        if current_month == config.BONUS_EVAL_MONTH:
            logger.info('--- November Annual Review ---')

            # A. Inflation adjustment (unless frozen)
            freeze = strategy.evaluate_inflation_freeze(
                proxy_price_12mo, sma_12mo,
            )
            if freeze:
                logger.info('Inflation adjustment FROZEN (market down vs 12mo SMA)')
                audit_log('inflation_frozen', {})
            else:
                old_withdrawal = state['current_monthly_withdrawal']
                state['current_monthly_withdrawal'] *= (1 + config.ANNUAL_INFLATION_RATE)
                target_withdrawal = state['current_monthly_withdrawal']
                logger.info(
                    'Inflation adjusted: $%.2f → $%.2f',
                    old_withdrawal, target_withdrawal,
                )
                audit_log('inflation_adjusted', {
                    'old': old_withdrawal,
                    'new': target_withdrawal,
                })

            # B. Special dividend
            prev_growth = state['last_november_growth_value']
            if prev_growth > 0:
                bonus = strategy.evaluate_november_bonus(
                    portfolio.growth_balance, prev_growth,
                )
                if bonus > 0:
                    target_withdrawal += bonus
                    logger.info('November bonus: +$%.2f', bonus)
                    audit_log('november_bonus', {'bonus': bonus})

            # Save this year's growth value for next November
            state['last_november_growth_value'] = portfolio.growth_balance

        # ---- 7. Execute cash raising ----
        logger.info('Raising $%.2f for withdrawal', target_withdrawal)
        sell_orders = portfolio.route_cash_raising(
            target_withdrawal, force_buffer=force_buffer,
        )

        audit_log('cash_raising', {
            'target': target_withdrawal,
            'force_buffer': force_buffer,
            'orders': sell_orders,
        })

        for ticker, amount in sell_orders:
            logger.info('SELL %s for $%.2f', ticker, amount)
            success = client.sell_dollar_amount(ticker, amount, dry_run=effective_dry_run)
            if not success:
                logger.error('Order may not have filled: %s $%.2f', ticker, amount)

        # ---- 8. Save state ----
        save_state(state)
        audit_log('run_complete', {'state': state})
        logger.info('========== SPM RUN COMPLETE ==========')

        return effective_dry_run  # return the mode to the caller for accurate alerting

    finally:
        client.disconnect()


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='Survivor Portfolio Manager')
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Force dry-run regardless of hardware token presence',
    )
    parser.add_argument(
        '--heartbeat', action='store_true',
        help='Send a heartbeat alert and exit',
    )
    args = parser.parse_args()

    alerter = AlertManager()

    if args.heartbeat:
        state = load_state()
        is_latched = state.get('is_live_latched', False)
        current_month = datetime.datetime.now().month

        if is_latched:
            # If the system is live, send the standard frequent heartbeat
            alerter.send_custom(
                subject="[SPM] Heartbeat — LIVE LATCHED",
                body="The SPM is actively managing the portfolio."
            )
        else:
            # If the system is idle, only text once per month
            last_month = state.get('last_idle_heartbeat_month', 0)
            if current_month != last_month:
                alerter.send_custom(
                    subject="[SPM] Monthly Sentinel Check — IDLE",
                    body="Hardware and network are functional. SPM is dormant."
                )
                # Update state so it stays quiet until next month
                state['last_idle_heartbeat_month'] = current_month
                save_state(state)
        return

    try:
        effective_dry_run = run_spm(cmd_line_dry_run=args.dry_run)
        
        mode_str = ' [DRY RUN / NO TOKEN]' if effective_dry_run else ' [LIVE LATCHED]'
        alerter.send_success(f'SPM executed successfully.{mode_str}')
        
    except Exception as e:
        logger.exception('SPM crashed')
        alerter.send_error('SPM terminated unexpectedly.', exception=e)
        sys.exit(1)


if __name__ == '__main__':
    main()