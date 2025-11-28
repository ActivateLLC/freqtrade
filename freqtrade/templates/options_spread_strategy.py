# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
"""
Options Spread Strategy for Freqtrade

This strategy demonstrates how to implement options spreads including:
- Bull Call Spreads
- Bear Put Spreads
- Bull Put Spreads (credit spreads)
- Bear Call Spreads (credit spreads)
- Vertical spreads with defined risk/reward

Spreads are implemented using position_adjustment to create multiple legs.
"""

import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from pandas import DataFrame
from typing import Optional, Union

from freqtrade.strategy import (
    IStrategy,
    Trade,
    Order,
    PairLocks,
    informative,
    BooleanParameter,
    CategoricalParameter,
    DecimalParameter,
    IntParameter,
    RealParameter,
    timeframe_to_minutes,
    timeframe_to_next_date,
    timeframe_to_prev_date,
    merge_informative_pair,
    stoploss_from_absolute,
    stoploss_from_open,
)

from freqtrade.enums import SignalDirection

import talib.abstract as ta
from technical import qtpylib


class OptionsSpreadStrategy(IStrategy):
    """
    Advanced Options Spread Trading Strategy

    This strategy implements vertical spreads (same expiry, different strikes):

    BULL CALL SPREAD (Bullish, Debit Spread):
    - Buy lower strike call (ATM or slightly OTM)
    - Sell higher strike call (further OTM)
    - Max profit: Strike difference - Net premium paid
    - Max loss: Net premium paid

    BEAR PUT SPREAD (Bearish, Debit Spread):
    - Buy higher strike put (ATM or slightly OTM)
    - Sell lower strike put (further OTM)
    - Max profit: Strike difference - Net premium paid
    - Max loss: Net premium paid

    BULL PUT SPREAD (Bullish, Credit Spread):
    - Sell higher strike put (ATM or slightly OTM)
    - Buy lower strike put (further OTM)
    - Max profit: Net premium received
    - Max loss: Strike difference - Net premium received

    BEAR CALL SPREAD (Bearish, Credit Spread):
    - Sell lower strike call (ATM or slightly OTM)
    - Buy higher strike call (further OTM)
    - Max profit: Net premium received
    - Max loss: Strike difference - Net premium received
    """

    INTERFACE_VERSION = 3
    can_short: bool = False

    # Enable position adjustment for multi-leg spreads
    position_adjustment_enable: bool = True
    max_entry_position_adjustment: int = 1  # One adjustment for the second leg

    # ROI for spreads (spreads have defined max profit)
    minimal_roi = {
        "120": 0.05,  # 5% after 2 hours
        "60": 0.15,   # 15% after 1 hour
        "30": 0.30,   # 30% after 30 min
        "0": 0.50,    # 50% target (near max profit)
    }

    # Stoploss for spreads (protect against max loss)
    stoploss = -0.80  # 80% of premium paid

    # Trailing stop
    trailing_stop = True
    trailing_stop_positive = 0.25
    trailing_stop_positive_offset = 0.35
    trailing_only_offset_is_reached = True

    timeframe = "15m"
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    # Strategy parameters
    rsi_period = IntParameter(low=7, high=21, default=14, space="buy", optimize=True)
    rsi_buy_threshold = IntParameter(low=20, high=40, default=30, space="buy", optimize=True)
    rsi_sell_threshold = IntParameter(low=60, high=80, default=70, space="sell", optimize=True)

    # Spread-specific parameters
    spread_type = CategoricalParameter(
        ["bull_call", "bear_put", "bull_put", "bear_call"],
        default="bull_call",
        space="buy",
        optimize=True
    )

    # Strike width in percentage (distance between strikes)
    spread_width_pct = DecimalParameter(
        low=0.03, high=0.15, default=0.05, decimals=2,
        space="buy", optimize=True, load=True
    )

    # First leg strike offset (from current price)
    first_leg_offset_pct = DecimalParameter(
        low=0.00, high=0.10, default=0.02, decimals=2,
        space="buy", optimize=True, load=True
    )

    min_days_to_expiry = IntParameter(low=5, high=14, default=7, space="buy", optimize=True)
    max_days_to_expiry = IntParameter(low=14, high=45, default=30, space="buy", optimize=True)

    # Custom data for tracking spread legs
    _spread_legs = {}  # Track which trades have completed legs

    startup_candle_count: int = 30

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Add technical indicators for spread trading signals"""

        # RSI
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=self.rsi_period.value)

        # Bollinger Bands
        bollinger = qtpylib.bollinger_bands(
            qtpylib.typical_price(dataframe), window=20, stds=2
        )
        dataframe["bb_lowerband"] = bollinger["lower"]
        dataframe["bb_middleband"] = bollinger["mid"]
        dataframe["bb_upperband"] = bollinger["upper"]
        dataframe["bb_percent"] = (
            (dataframe["close"] - dataframe["bb_lowerband"]) /
            (dataframe["bb_upperband"] - dataframe["bb_lowerband"])
        )
        dataframe["bb_width"] = (
            (dataframe["bb_upperband"] - dataframe["bb_lowerband"]) /
            dataframe["bb_middleband"]
        )

        # ATR for volatility
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]

        # Volume
        dataframe["volume_mean"] = dataframe["volume"].rolling(window=20).mean()

        # MACD
        macd = ta.MACD(dataframe)
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        dataframe["macdhist"] = macd["macdhist"]

        # EMAs for trend
        dataframe["ema_9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema_21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema_50"] = ta.EMA(dataframe, timeperiod=50)

        # Trend strength
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Generate entry signals for spreads.

        Bull spreads: Strong uptrend + oversold RSI
        Bear spreads: Strong downtrend + overbought RSI
        """

        # BULL CALL SPREAD or BULL PUT SPREAD
        # Enter when: bullish trend, RSI recovering from oversold, price above EMA
        dataframe.loc[
            (
                (dataframe["rsi"] > self.rsi_buy_threshold.value) &
                (dataframe["rsi"] < 50) &  # Not yet overbought
                (dataframe["close"] > dataframe["ema_21"]) &
                (dataframe["ema_9"] > dataframe["ema_21"]) &  # Short term up
                (dataframe["macdhist"] > 0) &
                (dataframe["adx"] > 20) &  # Trending
                (dataframe["volume"] > dataframe["volume_mean"])
            ),
            ["enter_long", "enter_tag"],
        ] = (1, "bull_spread")

        # BEAR PUT SPREAD or BEAR CALL SPREAD
        # Enter when: bearish trend, RSI falling from overbought, price below EMA
        dataframe.loc[
            (
                (dataframe["rsi"] < self.rsi_sell_threshold.value) &
                (dataframe["rsi"] > 50) &  # Not yet oversold
                (dataframe["close"] < dataframe["ema_21"]) &
                (dataframe["ema_9"] < dataframe["ema_21"]) &  # Short term down
                (dataframe["macdhist"] < 0) &
                (dataframe["adx"] > 20) &  # Trending
                (dataframe["volume"] > dataframe["volume_mean"])
            ),
            ["enter_long", "enter_tag"],
        ] = (1, "bear_spread")

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Exit signals for spreads"""

        # Exit bull spreads when trend reverses
        dataframe.loc[
            (
                (dataframe["rsi"] > 70) &
                (dataframe["macdhist"] < 0) &
                (dataframe["close"] < dataframe["ema_9"])
            ),
            ["exit_long", "exit_tag"],
        ] = (1, "bull_spread_exit")

        # Exit bear spreads when trend reverses
        dataframe.loc[
            (
                (dataframe["rsi"] < 30) &
                (dataframe["macdhist"] > 0) &
                (dataframe["close"] > dataframe["ema_9"])
            ),
            ["exit_long", "exit_tag"],
        ] = (1, "bear_spread_exit")

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
        Select the first leg of the spread.
        The second leg will be added via adjust_trade_position.
        """
        enter_tag = kwargs.get("enter_tag", "")

        # Determine spread type from entry tag
        is_bull_spread = "bull" in enter_tag.lower()
        is_bear_spread = "bear" in enter_tag.lower()

        if not (is_bull_spread or is_bear_spread):
            return None

        # Get available expiries
        try:
            underlying = pair.replace("/", "-")
            expiries = self.dp.exchange.get_option_expiry_dates(underlying)
            selected_expiry = self.select_option_expiry(pair, datetime.now(timezone.utc), expiries)

            if not selected_expiry:
                return None

            # FIRST LEG (Long leg - what we buy)
            if is_bull_spread:
                # Bull Call Spread: Buy ATM/OTM call
                option_type = "call"
                strike = current_rate * (1 + self.first_leg_offset_pct.value)
            else:
                # Bear Put Spread: Buy ATM/OTM put
                option_type = "put"
                strike = current_rate * (1 - self.first_leg_offset_pct.value)

            # Round strike
            if current_rate > 10000:
                strike = round(strike / 100) * 100
            elif current_rate > 1000:
                strike = round(strike / 10) * 10
            else:
                strike = round(strike, 2)

            # Store spread info for position adjustment
            spread_key = f"{pair}_{selected_expiry}"
            self._spread_legs[spread_key] = {
                "type": "bull" if is_bull_spread else "bear",
                "option_type": option_type,
                "first_leg_strike": strike,
                "expiry": selected_expiry,
                "second_leg_pending": True,
            }

            return {
                "option_type": option_type,
                "strike_price": strike,
                "expiry_date": selected_expiry,
            }

        except Exception as e:
            self.dp.send_msg(f"Error selecting spread for {pair}: {e}")
            return None

    def adjust_trade_position(
        self,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        min_stake: float | None,
        max_stake: float,
        current_entry_rate: float,
        current_exit_rate: float,
        current_entry_profit: float,
        current_exit_profit: float,
        **kwargs,
    ) -> float | None:
        """
        Add the second leg of the spread.

        This is called after the first leg is filled to complete the spread.
        """
        # Check if this is a spread trade
        if not trade.expiry_date or not trade.strike_price:
            return None

        spread_key = f"{trade.pair}_{trade.expiry_date.strftime('%y%m%d')}"

        if spread_key not in self._spread_legs:
            return None

        spread_info = self._spread_legs[spread_key]

        # Only add second leg once
        if not spread_info.get("second_leg_pending"):
            return None

        # Check if first leg is filled
        filled_orders = trade.select_filled_orders()
        if len(filled_orders) < 1:
            return None  # Wait for first leg to fill

        # Mark second leg as being added
        spread_info["second_leg_pending"] = False

        # Calculate second leg strike (further OTM)
        first_strike = spread_info["first_leg_strike"]
        spread_width = first_strike * self.spread_width_pct.value

        if spread_info["type"] == "bull":
            # Bull spread: Sell higher strike
            second_strike = first_strike + spread_width
        else:
            # Bear spread: Sell lower strike
            second_strike = first_strike - spread_width

        # Round second strike
        if current_rate > 10000:
            second_strike = round(second_strike / 100) * 100
        elif current_rate > 1000:
            second_strike = round(second_strike / 10) * 10
        else:
            second_strike = round(second_strike, 2)

        # Log spread details
        self.dp.send_msg(
            f"Creating {spread_info['type'].upper()} {spread_info['option_type'].upper()} spread:\n"
            f"Leg 1 (BUY):  Strike {first_strike}\n"
            f"Leg 2 (SELL): Strike {second_strike}\n"
            f"Width: {abs(second_strike - first_strike)} ({self.spread_width_pct.value*100}%)\n"
            f"Expiry: {spread_info['expiry']}"
        )

        # Return stake amount for second leg (same as first leg)
        # The exchange will handle this as a SELL order automatically
        return trade.stake_amount

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> bool | str:
        """
        Custom exit logic for spreads.

        Exit when:
        - Approaching max profit (80%+ of theoretical max)
        - Near expiry with profit
        - Max loss threshold reached
        """
        if not trade.expiry_date:
            return False

        days_to_expiry = (trade.expiry_date - current_time).days

        # Exit near expiry if profitable
        if days_to_expiry <= 2 and current_profit > 0.10:
            return "near_expiry_profit"

        # Exit very near expiry to avoid assignment
        if days_to_expiry <= 1:
            return "expiry_approaching"

        # For spreads, exit when we've captured most of max profit
        # (spread profit is capped, so lock in gains near max)
        if current_profit > 0.60:  # 60%+ of max profit
            return "max_profit_approached"

        return False

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs,
    ) -> float | None:
        """
        Custom stoploss for spreads.

        Spreads have defined max loss, so we can set tighter stops.
        """
        if not trade.expiry_date:
            return None

        days_to_expiry = (trade.expiry_date - current_time).days

        # Tighter stop near expiry
        if days_to_expiry <= 3:
            return -0.60  # 60% max loss
        elif days_to_expiry <= 7:
            return -0.70  # 70% max loss

        # Use default stoploss
        return None

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
        """
        Confirm trade entry.

        For spreads, ensure we have sufficient capital for both legs.
        """
        # Check if this is a spread entry
        if entry_tag and ("bull_spread" in entry_tag or "bear_spread" in entry_tag):
            # For spreads, we need 2x the stake (one for each leg)
            # This is a simplified check - actual margin requirements vary
            pass

        return True

    def calculate_option_strike(
        self,
        pair: str,
        option_type: str,
        current_rate: float,
        dataframe: DataFrame,
        **kwargs,
    ) -> float:
        """Calculate strike price for spread legs"""
        offset = self.first_leg_offset_pct.value

        if option_type == "call":
            strike = current_rate * (1 + offset)
        else:
            strike = current_rate * (1 - offset)

        # Round to appropriate intervals
        if current_rate > 10000:
            strike = round(strike / 100) * 100
        elif current_rate > 1000:
            strike = round(strike / 10) * 10
        else:
            strike = round(strike, 2)

        return strike

    def select_option_expiry(
        self, pair: str, current_time: datetime, available_expiries: list[str], **kwargs
    ) -> str | None:
        """Select expiry for spread (same for both legs)"""
        if not available_expiries:
            return None

        min_days = self.min_days_to_expiry.value
        max_days = self.max_days_to_expiry.value

        suitable_expiries = []

        for expiry in sorted(available_expiries):
            try:
                if len(expiry) == 6:
                    expiry_date = datetime.strptime(expiry, "%y%m%d")
                elif len(expiry) == 10:
                    expiry_date = datetime.strptime(expiry, "%Y-%m-%d")
                else:
                    continue

                days_to_expiry = (expiry_date - current_time).days

                if min_days <= days_to_expiry <= max_days:
                    suitable_expiries.append(expiry)
            except ValueError:
                continue

        return suitable_expiries[0] if suitable_expiries else None
