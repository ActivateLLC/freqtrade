# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
# --- Do not remove these imports ---
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
    # Hyperopt Parameters
    BooleanParameter,
    CategoricalParameter,
    DecimalParameter,
    IntParameter,
    RealParameter,
    # timeframe helpers
    timeframe_to_minutes,
    timeframe_to_next_date,
    timeframe_to_prev_date,
    # Strategy helper functions
    merge_informative_pair,
    stoploss_from_absolute,
    stoploss_from_open,
)

from freqtrade.enums import SignalDirection

# --------------------------------
# Add your lib to import here
import talib.abstract as ta
from technical import qtpylib


class OptionsStrategySample(IStrategy):
    """
    Sample Options Trading Strategy for freqtrade.

    This strategy demonstrates how to trade options using technical indicators.
    It uses RSI to determine market direction and automatically selects appropriate
    option contracts (calls or puts) based on the signals.

    Configuration requirements:
    - Set trading_mode to "options" in config.json
    - Configure OKX exchange credentials
    - Set pair_whitelist to underlying assets (e.g., ["BTC/USDT", "ETH/USDT"])

    How it works:
    1. Analyzes underlying asset using technical indicators
    2. Generates LONG signals when RSI is oversold (buy calls)
    3. Generates SHORT signals when RSI is overbought (buy puts)
    4. Automatically selects strike prices slightly OTM (5% offset)
    5. Targets expiries 7-30 days out for time decay management
    """

    # Strategy interface version
    INTERFACE_VERSION = 3

    # Options trading doesn't use traditional short positions
    # Instead, we buy puts (which profit from downward moves)
    can_short: bool = False

    # Minimal ROI for options (options can move quickly)
    minimal_roi = {
        "60": 0.10,  # 10% profit after 1 hour
        "30": 0.20,  # 20% profit after 30 minutes
        "0": 0.50,   # 50% profit target
    }

    # Stoploss for options (options can go to zero)
    # More aggressive stop loss due to theta decay
    stoploss = -0.50  # 50% loss

    # Trailing stoploss
    trailing_stop = True
    trailing_stop_positive = 0.20  # Start trailing at 20% profit
    trailing_stop_positive_offset = 0.30  # Trail by 10% (30-20)
    trailing_only_offset_is_reached = True

    # Timeframe for analysis
    timeframe = "15m"

    # Process only new candles
    process_only_new_candles = True

    # Exit signal configuration
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    # Hyperoptable parameters
    rsi_period = IntParameter(low=7, high=21, default=14, space="buy", optimize=True)
    rsi_buy_threshold = IntParameter(low=20, high=40, default=30, space="buy", optimize=True)
    rsi_sell_threshold = IntParameter(low=60, high=80, default=70, space="sell", optimize=True)

    # Options-specific parameters
    strike_offset_pct = DecimalParameter(
        low=0.02, high=0.10, default=0.05, decimals=2,
        space="buy", optimize=True, load=True
    )
    min_days_to_expiry = IntParameter(
        low=3, high=14, default=7, space="buy", optimize=True, load=True
    )
    max_days_to_expiry = IntParameter(
        low=14, high=60, default=30, space="buy", optimize=True, load=True
    )

    # Number of candles the strategy requires before producing valid signals
    startup_candle_count: int = 30

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Add technical indicators to the dataframe.

        For options trading, we analyze the underlying asset to determine
        market direction and volatility.
        """
        # RSI - Primary signal generator
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=self.rsi_period.value)

        # Bollinger Bands - for volatility assessment
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

        # ATR - for volatility measurement (important for options pricing)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)

        # Volume indicators
        dataframe["volume_mean"] = dataframe["volume"].rolling(window=20).mean()

        # MACD - for trend confirmation
        macd = ta.MACD(dataframe)
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        dataframe["macdhist"] = macd["macdhist"]

        # EMA - for trend direction
        dataframe["ema_20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema_50"] = ta.EMA(dataframe, timeperiod=50)

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Generate entry signals.

        For options:
        - enter_long = 1: Buy CALL options (bullish)
        - enter_short = 1: Buy PUT options (bearish)
        """
        # Buy CALL options when:
        # - RSI is oversold
        # - Price is near lower Bollinger Band
        # - MACD histogram is positive
        # - Volume is above average
        dataframe.loc[
            (
                (dataframe["rsi"] < self.rsi_buy_threshold.value) &
                (dataframe["bb_percent"] < 0.3) &
                (dataframe["macdhist"] > 0) &
                (dataframe["volume"] > dataframe["volume_mean"]) &
                (dataframe["close"] > dataframe["ema_20"])
            ),
            ["enter_long", "enter_tag"],
        ] = (1, "call_entry")

        # Buy PUT options when:
        # - RSI is overbought
        # - Price is near upper Bollinger Band
        # - MACD histogram is negative
        # - Volume is above average
        dataframe.loc[
            (
                (dataframe["rsi"] > self.rsi_sell_threshold.value) &
                (dataframe["bb_percent"] > 0.7) &
                (dataframe["macdhist"] < 0) &
                (dataframe["volume"] > dataframe["volume_mean"]) &
                (dataframe["close"] < dataframe["ema_20"])
            ),
            ["enter_long", "enter_tag"],  # Note: Still enter_long, but we'll buy puts via callback
        ] = (1, "put_entry")

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Generate exit signals.

        For options, we primarily rely on ROI and stoploss,
        but can add exit signals for specific conditions.
        """
        # Exit CALL options when RSI becomes overbought
        dataframe.loc[
            (
                (dataframe["rsi"] > 70) &
                (dataframe["bb_percent"] > 0.8)
            ),
            ["exit_long", "exit_tag"],
        ] = (1, "call_exit")

        # Exit PUT options when RSI becomes oversold
        dataframe.loc[
            (
                (dataframe["rsi"] < 30) &
                (dataframe["bb_percent"] < 0.2)
            ),
            ["exit_long", "exit_tag"],
        ] = (1, "put_exit")

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
        Select the specific option contract to trade.

        This method is called by the bot when an entry signal is generated
        in options trading mode.

        Returns:
            Dictionary with option contract details, or None to skip the trade.
        """
        # Get the latest candle data
        latest_candle = dataframe.iloc[-1]

        # Determine option type based on entry tag
        enter_tag = kwargs.get("enter_tag", "")

        if "call" in enter_tag.lower():
            option_type = "call"
            # For calls, select strike above current price
            strike_price = self.calculate_option_strike(
                pair, option_type, current_rate, dataframe
            )
        elif "put" in enter_tag.lower():
            option_type = "put"
            # For puts, select strike below current price
            strike_price = self.calculate_option_strike(
                pair, option_type, current_rate, dataframe
            )
        else:
            # Default based on signal direction
            option_type = "call" if signal_direction == SignalDirection.LONG else "put"
            strike_price = self.calculate_option_strike(
                pair, option_type, current_rate, dataframe
            )

        # Get available expiries
        try:
            underlying = pair.replace("/", "-")
            expiries = self.dp.exchange.get_option_expiry_dates(underlying)

            # Select expiry based on our parameters
            selected_expiry = self.select_option_expiry(
                pair, datetime.now(timezone.utc), expiries
            )

            if not selected_expiry:
                return None

            return {
                "option_type": option_type,
                "strike_price": strike_price,
                "expiry_date": selected_expiry,
            }
        except Exception as e:
            self.dp.send_msg(f"Error selecting option contract for {pair}: {e}")
            return None

    def calculate_option_strike(
        self,
        pair: str,
        option_type: str,
        current_rate: float,
        dataframe: DataFrame,
        **kwargs,
    ) -> float:
        """
        Calculate strike price based on current market price and strategy parameters.

        Overrides the default implementation to use optimizable parameters.
        """
        offset = self.strike_offset_pct.value

        if option_type == "call":
            # For calls, go slightly OTM (above current price)
            strike = current_rate * (1 + offset)
        else:
            # For puts, go slightly OTM (below current price)
            strike = current_rate * (1 - offset)

        # Round to reasonable strike intervals (e.g., nearest $100 for BTC)
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
        """
        Select expiry date within the configured min/max days range.
        """
        if not available_expiries:
            return None

        min_days = self.min_days_to_expiry.value
        max_days = self.max_days_to_expiry.value

        suitable_expiries = []

        for expiry in sorted(available_expiries):
            try:
                if len(expiry) == 6:  # YYMMDD format
                    expiry_date = datetime.strptime(expiry, "%y%m%d")
                elif len(expiry) == 10:  # YYYY-MM-DD format
                    expiry_date = datetime.strptime(expiry, "%Y-%m-%d")
                else:
                    continue

                days_to_expiry = (expiry_date - current_time).days

                if min_days <= days_to_expiry <= max_days:
                    suitable_expiries.append(expiry)
            except ValueError:
                continue

        # Return the nearest suitable expiry
        return suitable_expiries[0] if suitable_expiries else None

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
        Custom stoploss logic for options.

        Options can decay to zero, so we implement time-based adjustments
        to the stoploss as expiry approaches.
        """
        if trade.expiry_date:
            days_to_expiry = (trade.expiry_date - current_time).days

            # Tighten stoploss as expiry approaches
            if days_to_expiry <= 3:
                # Very tight stoploss close to expiry
                return -0.30
            elif days_to_expiry <= 7:
                # Moderate stoploss
                return -0.40

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
        Additional confirmation before entering a trade.

        For options, we can check IV levels, liquidity, etc.
        """
        # Add any additional checks here
        # For example, check if implied volatility is reasonable

        return True
