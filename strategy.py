# strategy.py — Survivor Portfolio Manager (SPM)
# ===============================================
# Stateless evaluation of market conditions.  State (transition flags) is
# passed in from the caller and returned as updated values — this module
# never reads or writes files.
#
# KEY FIX vs. original Gemini code:
#   The circuit breakers now compare the proxy index's CURRENT PRICE
#   against its own SMA — both in the same unit (dollars per share).
#   The original code compared portfolio dollar value against SPY's SMA,
#   which is an apples-to-oranges comparison that would always trigger.

import logging
import config

logger = logging.getLogger('spm.strategy')


class Strategy:
    def __init__(self, in_buffer_transition=False, transition_price=None):
        """
        in_buffer_transition: Are we currently in crisis mode (withdrawing
                              from SGOV instead of FI)?
        transition_price:     The proxy index price at which we entered
                              crisis mode.  Used to calculate the recovery
                              target.
        """
        self.in_buffer_transition = in_buffer_transition
        self.transition_price = transition_price

    # ------------------------------------------------------------------
    # Circuit breakers — evaluated weekly
    # ------------------------------------------------------------------
    def evaluate_circuit_breakers(self, proxy_current_price, proxy_sma_200):
        """
        Compares the proxy index (SPY) current price against its own
        200-day SMA.

        Returns: (halt_rebalancing: bool, force_buffer: bool)

        Side effects: updates self.in_buffer_transition and
                      self.transition_price.
        """
        if proxy_sma_200 is None or proxy_sma_200 <= 0:
            logger.warning('Invalid SMA-200 value; skipping circuit breaker eval')
            return False, self.in_buffer_transition

        if proxy_current_price is None or proxy_current_price <= 0:
            logger.warning('Invalid current price; skipping circuit breaker eval')
            return False, self.in_buffer_transition

        drawdown = (proxy_current_price - proxy_sma_200) / proxy_sma_200

        logger.info(
            'Circuit breaker — proxy: %.2f, SMA-200: %.2f, drawdown: %.2f%%',
            proxy_current_price, proxy_sma_200, drawdown * 100,
        )

        # Tier 1: Halt rebalancing at -5%
        halt_rebalancing = drawdown <= config.HALT_REBALANCE_THRESHOLD

        # Tier 2: Enter crisis mode at -7.5%
        if not self.in_buffer_transition and drawdown <= config.SHY_TRANSITION_THRESHOLD:
            self.in_buffer_transition = True
            self.transition_price = proxy_current_price
            logger.warning(
                'ENTERING CRISIS MODE — proxy price %.2f is %.1f%% below SMA',
                proxy_current_price, drawdown * 100,
            )

        # Recovery: exit crisis when proxy recovers 3% above transition price
        if self.in_buffer_transition and self.transition_price is not None:
            recovery_target = self.transition_price * (1 + config.RECOVERY_ABOVE_TRANSITION)
            if proxy_current_price >= recovery_target:
                logger.info(
                    'EXITING CRISIS MODE — proxy %.2f >= recovery target %.2f',
                    proxy_current_price, recovery_target,
                )
                self.in_buffer_transition = False
                self.transition_price = None

        return halt_rebalancing, self.in_buffer_transition

    # ------------------------------------------------------------------
    # Annual evaluations — run once in November
    # ------------------------------------------------------------------
    def evaluate_inflation_freeze(self, proxy_current_price, proxy_sma_12mo):
        """
        Should we freeze the annual inflation adjustment?

        Compares proxy index price to its 12-month SMA.
        Returns True if the freeze should be applied (market is down ≥5%).
        """
        if proxy_sma_12mo is None or proxy_sma_12mo <= 0:
            return False
        if proxy_current_price is None or proxy_current_price <= 0:
            return False

        drawdown = (proxy_current_price - proxy_sma_12mo) / proxy_sma_12mo
        freeze = drawdown <= config.INFLATION_FREEZE_THRESHOLD

        logger.info(
            'Inflation freeze eval — proxy: %.2f, SMA-12mo: %.2f, '
            'drawdown: %.2f%%, freeze: %s',
            proxy_current_price, proxy_sma_12mo, drawdown * 100, freeze,
        )
        return freeze

    def evaluate_november_bonus(self, current_growth_value, prev_year_growth_value):
        """
        Check whether the growth bucket earned a special dividend.

        Trigger: YoY return on the growth bucket > 25%
        Action:  Take 20% of the excess above 25%

        Returns the bonus dollar amount (0.0 if not triggered).
        """
        if prev_year_growth_value <= 0:
            return 0.0

        yoy_return = (
            (current_growth_value - prev_year_growth_value)
            / prev_year_growth_value
        )

        if yoy_return > config.BONUS_GROWTH_YOY_THRESHOLD:
            excess_pct = yoy_return - config.BONUS_GROWTH_YOY_THRESHOLD
            bonus = (prev_year_growth_value * excess_pct) * config.BONUS_EXCESS_TAKE_RATE
            logger.info(
                'November bonus triggered — YoY: %.1f%%, excess: %.1f%%, '
                'bonus: $%.2f',
                yoy_return * 100, excess_pct * 100, bonus,
            )
            return bonus

        logger.info(
            'November bonus NOT triggered — YoY: %.1f%% (threshold: %.1f%%)',
            yoy_return * 100, config.BONUS_GROWTH_YOY_THRESHOLD * 100,
        )
        return 0.0
