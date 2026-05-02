# portfolio.py — Survivor Portfolio Manager (SPM)
# ================================================
# Represents the live portfolio state and handles:
#   - Core vs. buffer balance separation
#   - Drift detection against 50/50 target
#   - Cash-raising sell order routing
#   - Rebalance trade generation (SELL and T+1 BUY)
#   - SGOV Buffer Refill mechanics

import logging
import config

logger = logging.getLogger('spm.portfolio')


class Portfolio:
    def __init__(self, live_balances: dict):
        self.balances = live_balances

        # Core buckets
        self.growth_balance = sum(self.balances.get(t, 0.0) for t in config.TICKERS_GROWTH)
        self.fi_balance = sum(self.balances.get(t, 0.0) for t in config.TICKERS_FI)
        self.core_balance = self.growth_balance + self.fi_balance

        # Buffer and Cash
        self.buffer_balance = self.balances.get(config.TICKER_BUFFER, 0.0)
        self.cash_balance = self.balances.get(config.CASH_TICKER, 0.0)

        logger.info(
            'Portfolio loaded — Core: $%.2f (Growth: $%.2f, FI: $%.2f), '
            'Buffer: $%.2f, Cash: $%.2f',
            self.core_balance, self.growth_balance, self.fi_balance,
            self.buffer_balance, self.cash_balance,
        )

    def get_drift(self):
        if self.core_balance <= 0: return 0.0, False

        current_growth_pct = self.growth_balance / self.core_balance
        target = config.TARGET_ALLOCATION_GROWTH

        abs_drift = abs(current_growth_pct - target)
        rel_drift = abs_drift / target if target > 0 else 0.0

        drifted = (abs_drift > config.REBALANCE_BAND_ABSOLUTE) or (rel_drift > config.REBALANCE_BAND_RELATIVE)
        return current_growth_pct, drifted

    # ------------------------------------------------------------------
    # Rebalance & T+1 Buy Deployment
    # ------------------------------------------------------------------
    def generate_rebalance_trades(self, sgov_target=0.0, refill_active=False):
        """
        Generates buy/sell orders. If refill_active is True, the SGOV buffer 
        gets first claim on any settled cash before the core rebalances.
        """
        trades = []
        deployable_cash = max(0.0, self.cash_balance - config.CASH_BUFFER_TARGET)

        # 1. BUY SIDE — First Claim: SGOV Buffer
        if refill_active and deployable_cash > 50.0:
            sgov_deficit = max(0.0, sgov_target - self.buffer_balance)
            if sgov_deficit > 0:
                amount_for_sgov = min(deployable_cash, sgov_deficit)
                trades.append(('BUY', config.TICKER_BUFFER, round(amount_for_sgov, 2)))
                deployable_cash -= amount_for_sgov
                logger.info('Deployed $%.2f of settled cash directly to SGOV.', amount_for_sgov)

        # 1. BUY SIDE — Second Claim: Core Rebalancing
        if deployable_cash > 50.0:
            ideal_core = self.core_balance + deployable_cash
            target_growth = ideal_core * config.TARGET_ALLOCATION_GROWTH
            target_fi = ideal_core * config.TARGET_ALLOCATION_FI

            deficit_growth = target_growth - self.growth_balance
            deficit_fi = target_fi - self.fi_balance

            deficits = [
                ('GROWTH', max(0, deficit_growth), config.TICKERS_GROWTH),
                ('FI', max(0, deficit_fi), config.TICKERS_FI)
            ]
            deficits.sort(key=lambda x: x[1], reverse=True)

            for bucket_name, deficit, tickers in deficits:
                if deployable_cash > 50.0 and deficit > 0:
                    amount_to_buy = min(deployable_cash, deficit)
                    split_amount = round(amount_to_buy / len(tickers), 2)
                    for ticker in tickers:
                        trades.append(('BUY', ticker, split_amount))
                    deployable_cash -= amount_to_buy

        # 2. SELL SIDE — Trim overweight positions if drifted
        current_growth_pct, drifted = self.get_drift()
        if drifted:
            target_growth = self.core_balance * config.TARGET_ALLOCATION_GROWTH
            excess_growth = self.growth_balance - target_growth

            if excess_growth > 0:
                for ticker in config.TICKERS_GROWTH:
                    weight = self.balances[ticker] / self.growth_balance if self.growth_balance > 0 else 0.0
                    amount = round(excess_growth * weight, 2)
                    if amount > 50.0: trades.append(('SELL', ticker, amount))
            else:
                excess_fi = abs(excess_growth)
                for ticker in config.TICKERS_FI:
                    weight = self.balances[ticker] / self.fi_balance if self.fi_balance > 0 else 0.0
                    amount = round(excess_fi * weight, 2)
                    if amount > 50.0: trades.append(('SELL', ticker, amount))

        return trades

    # ------------------------------------------------------------------
    # Buffer Refill Math
    # ------------------------------------------------------------------
    def route_buffer_refill_sells(self, sgov_target, monthly_refill_rate):
        """
        Calculates sell orders to raise cash for SGOV.
        Sources from the overweight bucket first, then proportionally from Growth.
        """
        sgov_deficit = max(0.0, sgov_target - self.buffer_balance)
        if sgov_deficit <= 0: return []

        target_refill = min(sgov_target * monthly_refill_rate, sgov_deficit)
        
        # If we already have settled cash sitting idle (e.g. from dividends), 
        # reduce the amount we need to sell this week.
        deployable_cash = max(0.0, self.cash_balance - config.CASH_BUFFER_TARGET)
        cash_to_raise = target_refill - deployable_cash

        if cash_to_raise < 50.0:
            return []

        trades = []
        remaining = cash_to_raise

        # Step 1: Pull from the overweight side of the core
        target_growth = self.core_balance * config.TARGET_ALLOCATION_GROWTH
        target_fi = self.core_balance * config.TARGET_ALLOCATION_FI

        excess_growth = max(0.0, self.growth_balance - target_growth)
        excess_fi = max(0.0, self.fi_balance - target_fi)

        if excess_growth > 0 and remaining > 0:
            amount = min(excess_growth, remaining)
            for ticker in config.TICKERS_GROWTH:
                weight = self.balances.get(ticker, 0) / self.growth_balance
                trades.append((ticker, round(amount * weight, 2)))
            remaining -= amount

        if excess_fi > 0 and remaining > 0:
            amount = min(excess_fi, remaining)
            for ticker in config.TICKERS_FI:
                weight = self.balances.get(ticker, 0) / self.fi_balance
                trades.append((ticker, round(amount * weight, 2)))
            remaining -= amount

        # Step 2: If we STILL need cash, pull proportionally from Growth assets
        if remaining > 1.0:
            for ticker in config.TICKERS_GROWTH:
                weight = self.balances.get(ticker, 0) / self.growth_balance if self.growth_balance else 0
                trades.append((ticker, round(remaining * weight, 2)))

        logger.info('Buffer Refill SELL targets generated to raise $%.2f: %s', cash_to_raise, trades)
        return [(t, a) for t, a in trades if a > 0.50]

    # ------------------------------------------------------------------
    # Cash-raising for monthly withdrawal
    # ------------------------------------------------------------------
    def route_cash_raising(self, target_amount, force_buffer=False):
        """
        Determines which assets to sell to meet the monthly withdrawal.

        Hierarchy:
          Crisis mode (force_buffer=True):  SGOV → FI → Growth
          Normal mode (force_buffer=False):       FI → Growth

        Returns: [(ticker, dollar_amount), ...]
        """
        sell_orders = []
        remaining = target_amount

        # Path A: Crisis — pull from buffer first
        if force_buffer and remaining > 0:
            available = self.buffer_balance
            if available >= remaining:
                sell_orders.append((config.TICKER_BUFFER, remaining))
                remaining = 0.0
            elif available > 0:
                sell_orders.append((config.TICKER_BUFFER, available))
                remaining -= available

        # Path B: Normal / Crisis fallback — pull proportionally from FI
        if remaining > 0 and self.fi_balance > 0:
            amount_from_fi = min(remaining, self.fi_balance)
            for ticker in config.TICKERS_FI:
                weight = (
                    self.balances[ticker] / self.fi_balance
                    if self.fi_balance > 0 else 0.0
                )
                amount = round(amount_from_fi * weight, 2)
                if amount > 0:
                    sell_orders.append((ticker, amount))
            remaining -= amount_from_fi

        # Path C: Last resort — pull proportionally from Growth
        if remaining > 0 and self.growth_balance > 0:
            amount_from_growth = min(remaining, self.growth_balance)
            for ticker in config.TICKERS_GROWTH:
                weight = (
                    self.balances[ticker] / self.growth_balance
                    if self.growth_balance > 0 else 0.0
                )
                amount = round(amount_from_growth * weight, 2)
                if amount > 0:
                    sell_orders.append((ticker, amount))
            remaining -= amount_from_growth

        if remaining > 0:
            logger.critical(
                'SHORTFALL: Could not raise full withdrawal. '
                'Deficit: $%.2f', remaining,
            )

        logger.info('Cash-raising orders: %s', sell_orders)
        return sell_orders