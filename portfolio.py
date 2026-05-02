# portfolio.py — Survivor Portfolio Manager (SPM)
# ================================================
# Represents the live portfolio state and handles:
#   - Core vs. buffer balance separation
#   - Drift detection against 50/50 target
#   - Cash-raising sell order routing
#   - Rebalance trade generation

import logging
import config

logger = logging.getLogger('spm.portfolio')


class Portfolio:
    def __init__(self, live_balances: dict):
        """
        live_balances: dict of {ticker: market_value_dollars} from IBKRClient.
        The buffer and cash tickers are present but tracked separately.
        """
        self.balances = live_balances

        # Core buckets (buffer and cash excluded)
        self.growth_balance = sum(
            self.balances.get(t, 0.0) for t in config.TICKERS_GROWTH
        )
        self.fi_balance = sum(
            self.balances.get(t, 0.0) for t in config.TICKERS_FI
        )
        self.core_balance = self.growth_balance + self.fi_balance

        # Buffer and Cash — tracked but never mixed into core
        self.buffer_balance = self.balances.get(config.TICKER_BUFFER, 0.0)
        self.cash_balance = self.balances.get(config.CASH_TICKER, 0.0)

        logger.info(
            'Portfolio loaded — Core: $%.2f (Growth: $%.2f, FI: $%.2f), '
            'Buffer: $%.2f, Cash: $%.2f',
            self.core_balance, self.growth_balance, self.fi_balance,
            self.buffer_balance, self.cash_balance,
        )

    # ------------------------------------------------------------------
    # Drift detection
    # ------------------------------------------------------------------
    def get_drift(self):
        """
        Returns the current growth allocation fraction and whether the
        5/25 drift bands have been breached.

        Returns: (current_growth_pct, drifted: bool)
        """
        if self.core_balance <= 0:
            return 0.0, False

        current_growth_pct = self.growth_balance / self.core_balance
        target = config.TARGET_ALLOCATION_GROWTH

        # Absolute band: |actual - target| > 5pp
        abs_drift = abs(current_growth_pct - target)
        abs_breach = abs_drift > config.REBALANCE_BAND_ABSOLUTE

        # Relative band: |actual - target| / target > 25%
        rel_drift = abs_drift / target if target > 0 else 0.0
        rel_breach = rel_drift > config.REBALANCE_BAND_RELATIVE

        drifted = abs_breach or rel_breach

        logger.info(
            'Drift check — Growth: %.1f%% (target %.1f%%), '
            'abs_drift: %.1f pp, rel_drift: %.1f%%, breached: %s',
            current_growth_pct * 100, target * 100,
            abs_drift * 100, rel_drift * 100, drifted,
        )
        return current_growth_pct, drifted

    # ------------------------------------------------------------------
    # Rebalance trade generation
    # ------------------------------------------------------------------
    def generate_rebalance_trades(self):
        """
        If the 50/50 split has drifted beyond the bands, generate sell/buy
        orders to restore balance.

        Returns a list of tuples: [('SELL'|'BUY', ticker, dollar_amount), ...]
        Only generates the SELL side — the cash raised will be used to buy
        the underweight bucket. This keeps execution simple: sell first,
        then buy after settlement.
        """
        current_growth_pct, drifted = self.get_drift()
        if not drifted:
            return []

        target_growth = self.core_balance * config.TARGET_ALLOCATION_GROWTH
        excess_growth = self.growth_balance - target_growth

        trades = []

        if excess_growth > 0:
            # Growth is overweight — sell proportionally from growth tickers
            # and the cash will be deployed into FI on the next cycle
            for ticker in config.TICKERS_GROWTH:
                weight = (
                    self.balances[ticker] / self.growth_balance
                    if self.growth_balance > 0 else 0.0
                )
                amount = abs(excess_growth) * weight
                if amount > 0:
                    trades.append(('SELL', ticker, amount))
        else:
            # FI is overweight — sell proportionally from FI tickers
            excess_fi = abs(excess_growth)  # symmetric
            for ticker in config.TICKERS_FI:
                weight = (
                    self.balances[ticker] / self.fi_balance
                    if self.fi_balance > 0 else 0.0
                )
                amount = excess_fi * weight
                if amount > 0:
                    trades.append(('SELL', ticker, amount))

        logger.info('Rebalance trades: %s', trades)
        return trades

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
                amount = amount_from_fi * weight
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
                amount = amount_from_growth * weight
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
