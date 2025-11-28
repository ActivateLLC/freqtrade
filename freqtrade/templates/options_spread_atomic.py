# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
"""
Atomic Options Spread Strategy using OKX Batch Orders

This strategy uses OKX's native batch order API for atomic spread execution.
Both legs are submitted simultaneously, reducing execution risk.

Advantages over sequential leg execution:
- Both legs fill together or not at all
- No partial spread risk
- Better price execution
- Lower slippage
"""

from datetime import datetime, timezone
from pandas import DataFrame
from typing import Optional

from freqtrade.strategy import IStrategy, Trade, Order
from freqtrade.enums import SignalDirection
from freqtrade.exchange.okx import Okx

import talib.abstract as ta
from technical import qtpylib


class OptionsSpreadAtomic(IStrategy):
    """
    Atomic spread execution using OKX batch orders.

    This strategy demonstrates using create_spread_order() for
    simultaneous execution of both spread legs.
    """

    INTERFACE_VERSION = 3
    can_short: bool = False

    # No position adjustment needed - spreads created atomically
    position_adjustment_enable: bool = False

    minimal_roi = {
        "60": 0.10,
        "30": 0.20,
        "0": 0.50,
    }

    stoploss = -0.70
    trailing_stop = True
    trailing_stop_positive = 0.20
    trailing_stop_positive_offset = 0.30
    trailing_only_offset_is_reached = True

    timeframe = "15m"
    process_only_new_candles = True
    use_exit_signal = True

    # Spread parameters
    spread_width_pct: float = 0.05  # 5% between strikes
    min_days_to_expiry: int = 7
    max_days_to_expiry: int = 30

    startup_candle_count: int = 30

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Add technical indicators"""
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)

        bollinger = qtpylib.bollinger_bands(qtpylib.typical_price(dataframe), window=20, stds=2)
        dataframe["bb_lowerband"] = bollinger["lower"]
        dataframe["bb_upperband"] = bollinger["upper"]

        macd = ta.MACD(dataframe)
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        dataframe["macdhist"] = macd["macdhist"]

        dataframe["ema_9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema_21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Entry signals for bull/bear spreads"""

        # Bull spread entry
        dataframe.loc[
            (
                (dataframe["rsi"] > 30) &
                (dataframe["rsi"] < 50) &
                (dataframe["close"] > dataframe["ema_21"]) &
                (dataframe["macdhist"] > 0) &
                (dataframe["adx"] > 20)
            ),
            ["enter_long", "enter_tag"],
        ] = (1, "bull_call_spread")

        # Bear spread entry
        dataframe.loc[
            (
                (dataframe["rsi"] < 70) &
                (dataframe["rsi"] > 50) &
                (dataframe["close"] < dataframe["ema_21"]) &
                (dataframe["macdhist"] < 0) &
                (dataframe["adx"] > 20)
            ),
            ["enter_long", "enter_tag"],
        ] = (1, "bear_put_spread")

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Exit signals"""
        dataframe.loc[
            ((dataframe["rsi"] > 70) & (dataframe["macdhist"] < 0)),
            ["exit_long", "exit_tag"],
        ] = (1, "trend_reversal")

        return dataframe

    def select_option_contract(
        self,
        pair: str,
        signal_direction: SignalDirection,
        current_rate: float,
        dataframe: DataFrame,
        metadata: dict,
        **kwargs,
    ) -> dict | None:
        """
        For atomic spreads, we override this to create the spread immediately
        using batch orders instead of position adjustment.
        """
        enter_tag = kwargs.get("enter_tag", "")

        if "bull_call" in enter_tag:
            return self._create_bull_call_spread(pair, current_rate)
        elif "bear_put" in enter_tag:
            return self._create_bear_put_spread(pair, current_rate)

        return None

    def _create_bull_call_spread(self, pair: str, current_rate: float) -> dict | None:
        """
        Create bull call spread atomically using batch orders.

        Buy lower strike call, sell higher strike call.
        """
        try:
            # Get expiry
            underlying = pair.replace("/", "-")
            expiries = self.dp.exchange.get_option_expiry_dates(underlying)
            expiry = self._select_expiry(expiries)

            if not expiry:
                return None

            # Calculate strikes
            lower_strike = current_rate * 1.02  # 2% OTM
            higher_strike = lower_strike * (1 + self.spread_width_pct)

            # Round strikes
            lower_strike = self._round_strike(lower_strike, current_rate)
            higher_strike = self._round_strike(higher_strike, current_rate)

            # Build option symbols
            # Format: BTC-USD-250131-50000-C
            leg1_symbol = f"{underlying}-{expiry}-{int(lower_strike)}-C"
            leg2_symbol = f"{underlying}-{expiry}-{int(higher_strike)}-C"

            # Get current market prices for the options
            # (In production, fetch actual option prices)
            leg1_price = current_rate * 0.03  # Approximate premium
            leg2_price = current_rate * 0.015  # Lower premium for higher strike

            # Create spread using batch order
            if isinstance(self.dp.exchange, Okx):
                result = self.dp.exchange.create_spread_order(
                    leg1_symbol=leg1_symbol,
                    leg1_side="buy",
                    leg1_amount=1.0,
                    leg1_price=leg1_price,
                    leg2_symbol=leg2_symbol,
                    leg2_side="sell",
                    leg2_amount=1.0,
                    leg2_price=leg2_price,
                    order_type="limit",
                )

                self.dp.send_msg(
                    f"Created BULL CALL SPREAD:\n"
                    f"Buy  {leg1_symbol} @ {leg1_price}\n"
                    f"Sell {leg2_symbol} @ {leg2_price}\n"
                    f"Max Profit: {higher_strike - lower_strike - (leg1_price - leg2_price)}\n"
                    f"Max Loss: {leg1_price - leg2_price}"
                )

                # Return the long leg info for trade tracking
                return {
                    "option_type": "call",
                    "strike_price": lower_strike,
                    "expiry_date": expiry,
                    "spread_type": "bull_call",
                    "spread_width": higher_strike - lower_strike,
                }

        except Exception as e:
            self.dp.send_msg(f"Error creating bull call spread: {e}")
            return None

    def _create_bear_put_spread(self, pair: str, current_rate: float) -> dict | None:
        """
        Create bear put spread atomically using batch orders.

        Buy higher strike put, sell lower strike put.
        """
        try:
            underlying = pair.replace("/", "-")
            expiries = self.dp.exchange.get_option_expiry_dates(underlying)
            expiry = self._select_expiry(expiries)

            if not expiry:
                return None

            # Calculate strikes
            higher_strike = current_rate * 0.98  # 2% below current
            lower_strike = higher_strike * (1 - self.spread_width_pct)

            # Round strikes
            higher_strike = self._round_strike(higher_strike, current_rate)
            lower_strike = self._round_strike(lower_strike, current_rate)

            # Build option symbols
            leg1_symbol = f"{underlying}-{expiry}-{int(higher_strike)}-P"
            leg2_symbol = f"{underlying}-{expiry}-{int(lower_strike)}-P"

            # Approximate premiums
            leg1_price = current_rate * 0.03
            leg2_price = current_rate * 0.015

            # Create spread using batch order
            if isinstance(self.dp.exchange, Okx):
                result = self.dp.exchange.create_spread_order(
                    leg1_symbol=leg1_symbol,
                    leg1_side="buy",
                    leg1_amount=1.0,
                    leg1_price=leg1_price,
                    leg2_symbol=leg2_symbol,
                    leg2_side="sell",
                    leg2_amount=1.0,
                    leg2_price=leg2_price,
                    order_type="limit",
                )

                self.dp.send_msg(
                    f"Created BEAR PUT SPREAD:\n"
                    f"Buy  {leg1_symbol} @ {leg1_price}\n"
                    f"Sell {leg2_symbol} @ {leg2_price}\n"
                    f"Max Profit: {higher_strike - lower_strike - (leg1_price - leg2_price)}\n"
                    f"Max Loss: {leg1_price - leg2_price}"
                )

                return {
                    "option_type": "put",
                    "strike_price": higher_strike,
                    "expiry_date": expiry,
                    "spread_type": "bear_put",
                    "spread_width": higher_strike - lower_strike,
                }

        except Exception as e:
            self.dp.send_msg(f"Error creating bear put spread: {e}")
            return None

    def _select_expiry(self, expiries: list[str]) -> str | None:
        """Select appropriate expiry"""
        if not expiries:
            return None

        current_time = datetime.now(timezone.utc)

        for expiry in sorted(expiries):
            try:
                if len(expiry) == 6:
                    expiry_date = datetime.strptime(expiry, "%y%m%d")
                else:
                    expiry_date = datetime.strptime(expiry, "%Y-%m-%d")

                days_to_expiry = (expiry_date - current_time).days

                if self.min_days_to_expiry <= days_to_expiry <= self.max_days_to_expiry:
                    return expiry
            except ValueError:
                continue

        return expiries[0] if expiries else None

    def _round_strike(self, strike: float, current_rate: float) -> float:
        """Round strike to appropriate interval"""
        if current_rate > 10000:
            return round(strike / 100) * 100
        elif current_rate > 1000:
            return round(strike / 10) * 10
        else:
            return round(strike, 2)

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> bool | str:
        """Custom exit for spreads"""
        if not trade.expiry_date:
            return False

        days_to_expiry = (trade.expiry_date - current_time).days

        # Exit near expiry
        if days_to_expiry <= 2 and current_profit > 0.10:
            return "near_expiry"

        # Exit when approaching max profit
        if current_profit > 0.60:
            return "max_profit"

        return False

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> bool:
        """Confirm spread entry"""
        # Ensure we're using OKX exchange
        if not isinstance(self.dp.exchange, Okx):
            self.dp.send_msg("Atomic spreads require OKX exchange")
            return False

        return True
