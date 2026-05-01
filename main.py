# main.py — Survivor Portfolio Manager (SPM)
# ============================================
# Top-level orchestrator.  Runs once per scheduled execution (weekly for
# rebalancing checks, monthly for cash raising).
#
# Usage:
#   python main.py                  # live execution
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
# Persistent state (survives between runs)
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
    }


def save_state(state):
    os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)
    with open(config.STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)
    logger.info('State saved to %s', config.STATE_FILE)


# ------------------------------------------------------------------
# Core execution
# ------------------------------------------------------------------
def run_spm(dry_run=False):
    state = load_state()
    client = IBKRClient()
    now = datetime.datetime.now()
    current_month = now.month

    logger.info('========== SPM RUN START (dry_run=%s) ==========', dry_run)
    audit_log('run_start', {'dry_run': dry_run, 'month': current_month})

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

        # ---- 2. Fetch proxy index SMA data ----
        # 200-day SMA (for circuit breakers)
        proxy_price_200, sma_200 = client.get_price_and_sma(
            config.PROXY_INDEX_TICKER,
            config.SMA_200_PERIOD,
            config.SMA_200_BAR,
        )

        # 12-month SMA (for inflation freeze — only needed in November
        # but cheap to fetch, and useful for the audit log)
        proxy_price_12mo, sma_12mo = client.get_price_and_sma(
            config.PROXY_INDEX_TICKER,
            config.SMA_12MO_PERIOD,
            config.SMA_12MO_BAR,
        )

        audit_log('sma_data', {
            'proxy': config.PROXY_INDEX_TICKER,
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

        # Persist transition state
        state['in_buffer_transition'] = strategy.in_buffer_transition
        state['transition_price'] = strategy.transition_price

        audit_log('circuit_breakers', {
            'halt_rebalancing': halt_rebalancing,
            'force_buffer': force_buffer,
            'in_buffer_transition': strategy.in_buffer_transition,
            'transition_price': strategy.transition_price,
        })

        # ---- 4. Rebalancing (weekly) ----
        if not halt_rebalancing:
            rebal_trades = portfolio.generate_rebalance_trades()
            if rebal_trades:
                audit_log('rebalance_trades', {'trades': rebal_trades})
                for direction, ticker, amount in rebal_trades:
                    if direction == 'SELL':
                        logger.info('Rebalance SELL: %s $%.2f', ticker, amount)
                        client.sell_dollar_amount(ticker, amount, dry_run=dry_run)
                # NOTE: The BUY side of rebalancing is not yet implemented.
                # Cash raised sits in the account until the next run or
                # manual intervention.  Phase 2 will add buy-side logic.
        else:
            logger.info('Rebalancing HALTED by 200-day SMA circuit breaker')
            audit_log('rebalance_halted', {})

        # ---- 5. Monthly cash raising ----
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
            success = client.sell_dollar_amount(ticker, amount, dry_run=dry_run)
            if not success:
                logger.error('Order may not have filled: %s $%.2f', ticker, amount)

        # ---- 8. Save state ----
        save_state(state)
        audit_log('run_complete', {'state': state})
        logger.info('========== SPM RUN COMPLETE ==========')

        return True  # signal success to caller

    finally:
        client.disconnect()


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='Survivor Portfolio Manager')
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Log all decisions but execute no trades',
    )
    parser.add_argument(
        '--heartbeat', action='store_true',
        help='Send a heartbeat alert and exit',
    )
    args = parser.parse_args()

    alerter = AlertManager()

    if args.heartbeat:
        alerter.send_heartbeat()
        return

    try:
        run_spm(dry_run=args.dry_run)
        mode = ' [DRY RUN]' if args.dry_run else ''
        alerter.send_success(f'SPM executed successfully.{mode}')
    except Exception as e:
        logger.exception('SPM crashed')
        alerter.send_error('SPM terminated unexpectedly.', exception=e)
        sys.exit(1)


if __name__ == '__main__':
    main()
