# ibkr_client.py — Survivor Portfolio Manager (SPM)
# ==================================================
# Handles all communication with Interactive Brokers via ib_insync.
# Every method that touches the network includes timeout and error handling.

from ib_insync import IB, Stock, MarketOrder
import math
import logging
import time
import config

logger = logging.getLogger('spm.ibkr')


class IBKRClient:
    def __init__(self):
        self.ib = IB()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------
    def connect(self, retries=3, delay=30):
        """Connect to TWS / IB Gateway with retry logic."""
        for attempt in range(1, retries + 1):
            if not self.ib.isConnected():
                try:
                    self.ib.connect(
                        config.IBKR_HOST,
                        config.IBKR_PORT,
                        clientId=config.IBKR_CLIENT_ID,
                        timeout=20,
                    )
                    logger.info('Connected to IBKR at %s:%s', config.IBKR_HOST, config.IBKR_PORT)
                    return # Success
                except Exception as e:
                    logger.warning('Connection attempt %d failed: %s', attempt, e)
                    if attempt < retries:
                        logger.info('Sleeping for %d seconds before retrying...', delay)
                        time.sleep(delay)
                    else:
                        logger.error('Exhausted all connection retries.')
                        raise # Bubble up to trigger the AlertManager email

    def disconnect(self):
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info('Disconnected from IBKR')

    # ------------------------------------------------------------------
    # Portfolio state
    # ------------------------------------------------------------------
    def get_portfolio_state(self):
        """
        Returns a dict of {ticker: market_value} for every tracked ticker.
        SGOV and USD Cash are included but must be isolated from core calculations.
        """
        all_tickers = config.CORE_TICKERS + [config.TICKER_BUFFER]
        state = {t: 0.0 for t in all_tickers}
        state[config.CASH_TICKER] = 0.0 

        positions = self.ib.positions()
        
        # 1. Map positions
        pos_map = {pos.contract.symbol: pos.position for pos in positions}
        if 'USD' in pos_map: # Handle base currency
            state[config.CASH_TICKER] = pos_map['USD']

        # 2. Qualify contracts and request bulk tickers
        contracts = [Stock(symbol, 'SMART', 'USD') for symbol in all_tickers if symbol in pos_map]
        if contracts:
            self.ib.qualifyContracts(*contracts)
            tickers = self.ib.reqTickers(*contracts) # Batch request, no sleep needed
            
            for ticker in tickers:
                symbol = ticker.contract.symbol
                price = ticker.marketPrice()
                if math.isnan(price):
                    price = ticker.close
                if math.isnan(price):
                    logger.warning('No price available for %s — using 0', symbol)
                    price = 0.0
                
                state[symbol] = pos_map[symbol] * price

        logger.info('Portfolio state: %s', state)
        return state

    # ------------------------------------------------------------------
    # SMA — returns (current_price, sma_value) for a synthetic index
    # ------------------------------------------------------------------
    def get_synthetic_price_and_sma(self, symbols: list, duration_str, bar_size):
        """
        Fetches historical bars for multiple symbols and blends them into
        an equal-weight synthetic index. Returns a tuple:
            (synthetic_current_price, synthetic_sma_value)

        Both values are in the same unit (price-per-share) so the caller
        can safely compute a percentage drawdown.

        Returns (None, None) if data is unavailable or dates do not align.
        """
        if not symbols:
            logger.error('No symbols provided for synthetic index')
            return None, None

        all_bars = {}
        for symbol in symbols:
            contract = Stock(symbol, 'SMART', 'USD')
            self.ib.qualifyContracts(contract)

            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime='',
                durationStr=duration_str,
                barSizeSetting=bar_size,
                whatToShow='TRADES',
                useRTH=True,
                formatDate=1,
            )

            if not bars:
                logger.error('No historical bars returned for %s', symbol)
                return None, None
            all_bars[symbol] = bars

        # Align by date to ensure the math is accurate even if a fund
        # has a missing day/week of trading.
        date_map = {}
        for symbol, bars in all_bars.items():
            for bar in bars:
                d = bar.date
                if d not in date_map:
                    date_map[d] = {}
                date_map[d][symbol] = bar.close

        # Filter to dates where we have a closing price for EVERY symbol
        valid_dates = [d for d, prices in date_map.items() if len(prices) == len(symbols)]
        valid_dates.sort()

        if not valid_dates:
            logger.error('No overlapping trading dates found for %s', symbols)
            return None, None

        # Build the synthetic index (equal weight)
        weight = 1.0 / len(symbols)
        synthetic_history = []
        for d in valid_dates:
            prices = date_map[d]
            synthetic_close = sum(prices[sym] * weight for sym in symbols)
            synthetic_history.append(synthetic_close)

        sma_value = sum(synthetic_history) / len(synthetic_history)
        current_price = synthetic_history[-1]  # Most recent valid bar

        logger.info(
            'Synthetic Index %s — current: %.2f, SMA(%d bars): %.2f',
            symbols, current_price, len(synthetic_history), sma_value,
        )
        return current_price, sma_value

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------
    def sell_dollar_amount(self, symbol, dollar_amount, dry_run=False):
        """
        Submits a market sell order for a specific dollar amount using
        cashQty (fractional shares).

        Enforces MAX_SINGLE_TRADE_DOLLARS as a safety cap.
        Returns True if filled, False otherwise.
        If dry_run is True, logs the order but does not execute.
        """
        if dollar_amount <= 0:
            logger.warning('Skipping sell of %s — amount is $%.2f', symbol, dollar_amount)
            return False

        if dollar_amount > config.MAX_SINGLE_TRADE_DOLLARS:
            logger.error(
                'SAFETY CAP: Attempted sell of %s for $%.2f exceeds max $%.2f. '
                'Capping to max.',
                symbol, dollar_amount, config.MAX_SINGLE_TRADE_DOLLARS,
            )
            dollar_amount = config.MAX_SINGLE_TRADE_DOLLARS

        if dry_run:
            logger.info('[DRY RUN] Would SELL %s for $%.2f', symbol, dollar_amount)
            return True

        contract = Stock(symbol, 'SMART', 'USD')
        self.ib.qualifyContracts(contract)

        order = MarketOrder('SELL', totalQuantity=0, cashQty=dollar_amount)
        trade = self.ib.placeOrder(contract, order)

        # Wait up to 60 seconds for fill
        timeout = 60
        elapsed = 0
        while not trade.isDone() and elapsed < timeout:
            self.ib.waitOnUpdate(timeout=5)
            elapsed += 5

        if trade.orderStatus.status == 'Filled':
            logger.info('FILLED: Sold $%.2f of %s', dollar_amount, symbol)
            return True
        else:
            logger.error('ORDER INCOMPLETE: %s status=%s after %ds. Canceling order.', 
                         symbol, trade.orderStatus.status, elapsed)
            self.ib.cancelOrder(order) # Explicitly cancel the dangling order
            self.ib.sleep(2) # Give IBKR a moment to process the cancellation
            return False

    def buy_dollar_amount(self, symbol, dollar_amount, dry_run=False):
        """
        Submits a market buy order using fractional shares (cashQty).
        """
        if dollar_amount <= 0:
            return False

        if dollar_amount > config.MAX_SINGLE_TRADE_DOLLARS:
            logger.error('SAFETY CAP: Buy order %s capped at max.', symbol)
            dollar_amount = config.MAX_SINGLE_TRADE_DOLLARS

        if dry_run:
            logger.info('[DRY RUN] Would BUY %s for $%.2f', symbol, dollar_amount)
            return True

        contract = Stock(symbol, 'SMART', 'USD')
        self.ib.qualifyContracts(contract)

        order = MarketOrder('BUY', totalQuantity=0, cashQty=dollar_amount)
        trade = self.ib.placeOrder(contract, order)

        timeout = 60
        elapsed = 0
        while not trade.isDone() and elapsed < timeout:
            self.ib.waitOnUpdate(timeout=5)
            elapsed += 5

        if trade.orderStatus.status == 'Filled':
            logger.info('FILLED: Bought $%.2f of %s', dollar_amount, symbol)
            return True
        else:
            logger.error('ORDER INCOMPLETE: %s status=%s after %ds. Canceling order.', 
                         symbol, trade.orderStatus.status, elapsed)
            self.ib.cancelOrder(order) # Explicitly cancel the dangling order
            self.ib.sleep(2) # Give IBKR a moment to process the cancellation
            return False