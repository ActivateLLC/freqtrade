# Options Trading Bot for Freqtrade

This extension adds options trading capabilities to Freqtrade, with full support for OKX options markets.

## Features

- **Full Options Support**: Trade call and put options on major cryptocurrencies
- **OKX Integration**: Complete integration with OKX options API
- **Greeks Monitoring**: Track Delta, Gamma, Theta, Vega, and Rho
- **Smart Strike Selection**: Automated strike price selection based on technical analysis
- **Expiry Management**: Configurable expiry selection with min/max days
- **Strategy Callbacks**: Customizable option contract selection in strategies

## Quick Start

### 1. Configuration

Use the provided sample configuration as a starting point:

```bash
cp config_options_sample.json config_options.json
```

Edit `config_options.json` and update:
- `exchange.key`, `exchange.secret`, `exchange.password` with your OKX API credentials
- `stake_amount` to your desired position size
- `options_min_days_to_expiry` and `options_max_days_to_expiry` for expiry preferences

### 2. Run the Bot

```bash
freqtrade trade --config config_options.json --strategy OptionsStrategySample
```

## Configuration Parameters

### Required Settings

```json
{
    "trading_mode": "options",
    "exchange": {
        "name": "okx"
    }
}
```

### Options-Specific Parameters

- `options_min_days_to_expiry` (default: 7): Minimum days until expiry for option contracts
- `options_max_days_to_expiry` (default: 90): Maximum days until expiry for option contracts
- `options_strike_offset_pct` (default: 0.05): Percentage offset from current price for strike selection
- `options_expiry_preference`: Preferred expiry type ("weekly", "monthly", "quarterly")

## Strategy Development

### Basic Options Strategy

```python
from freqtrade.strategy import IStrategy
from freqtrade.enums import SignalDirection
from pandas import DataFrame

class MyOptionsStrategy(IStrategy):
    INTERFACE_VERSION = 3

    # Your indicator and signal logic
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Add your indicators
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Generate entry signals
        dataframe.loc[
            (your_conditions_here),
            'enter_long'
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Generate exit signals
        return dataframe

    # Options-specific callbacks
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
        Select the option contract to trade.

        Returns:
            {
                'option_type': 'call' or 'put',
                'strike_price': float,
                'expiry_date': 'YYMMDD' or 'YYYY-MM-DD'
            }
        """
        option_type = "call" if signal_direction == SignalDirection.LONG else "put"
        strike_price = self.calculate_option_strike(pair, option_type, current_rate, dataframe)

        # Get available expiries and select one
        underlying = pair.replace("/", "-")
        expiries = self.dp.exchange.get_option_expiry_dates(underlying)
        expiry = self.select_option_expiry(pair, datetime.now(), expiries)

        return {
            "option_type": option_type,
            "strike_price": strike_price,
            "expiry_date": expiry
        }
```

### Available Strategy Methods

#### Required Methods (from IStrategy)
- `populate_indicators()`: Add technical indicators
- `populate_entry_trend()`: Generate entry signals
- `populate_exit_trend()`: Generate exit signals

#### Options-Specific Methods
- `select_option_contract()`: Select which option contract to trade
- `calculate_option_strike()`: Calculate strike price (has default implementation)
- `select_option_expiry()`: Select expiry date (has default implementation)

### Accessing Option Data in Strategy

```python
def custom_exit(self, pair: str, trade: Trade, current_time: datetime,
                current_rate: float, current_profit: float, **kwargs) -> bool:
    # Access option-specific trade data
    if trade.option_type == "call":
        # Check Greeks
        if trade.delta and trade.delta < 0.3:
            return True  # Exit if delta dropped too low

    # Check time to expiry
    if trade.expiry_date:
        days_to_expiry = (trade.expiry_date - current_time).days
        if days_to_expiry < 3:
            return True  # Exit near expiry

    return False
```

## Trade Model Fields

When trading options, trades have additional fields:

- `option_type`: "call" or "put"
- `strike_price`: Strike price of the option
- `expiry_date`: Expiration date
- `implied_volatility`: IV at entry
- `option_premium`: Premium paid
- `delta`, `gamma`, `theta`, `vega`, `rho`: Greeks values

## Exchange Methods

The following exchange methods are available for options:

```python
# Fetch available option contracts
markets = exchange.fetch_options_markets("BTC")

# Get option chain (all strikes for expiry)
chain = exchange.fetch_option_chain("BTC-USD", "250131")

# Fetch Greeks for a specific option
greeks = exchange.fetch_greeks("BTC-USD-250131-50000-C")

# Get available expiry dates
expiries = exchange.get_option_expiry_dates("BTC-USD")

# Create option order
order = exchange.create_option_order(
    symbol="BTC-USD-250131-50000-C",
    order_type="limit",
    side="buy",
    amount=1,
    price=1500
)
```

## Risk Management for Options

Options trading has unique risks:

### Theta Decay
Options lose value over time. The `custom_stoploss` method in the sample strategy demonstrates time-based adjustments.

### Strike Selection
- **Calls**: Strike above current price = bullish bet
- **Puts**: Strike below current price = bearish bet
- **OTM (Out of The Money)**: Cheaper but lower probability
- **ATM (At The Money)**: Balanced risk/reward
- **ITM (In The Money)**: More expensive but higher delta

### Position Sizing
Options can expire worthless. Never risk more than you can afford to lose on a single contract.

## Example Strategies

### 1. RSI-Based Options Strategy (Provided)
- Buys calls when RSI is oversold
- Buys puts when RSI is overbought
- Uses Bollinger Bands for confirmation
- Targets 7-30 day expiries

### 2. Volatility Trading
```python
def select_option_contract(self, ...):
    # Trade based on IV changes
    if volatility_increasing:
        option_type = "call"  # Or "put" based on direction
        # Select shorter expiry to benefit from IV spike
        expiry = nearest_weekly_expiry
    return {...}
```

### 3. Delta Hedging
```python
def custom_exit(self, pair, trade, ...):
    # Exit when delta moves out of target range
    if trade.delta:
        if trade.option_type == "call" and trade.delta < 0.3:
            return True
        if trade.option_type == "put" and trade.delta > -0.3:
            return True
    return False
```

## Backtesting

Options backtesting has limitations:
- Historical options data may not be available
- Greeks are approximated
- Expiry handling requires special consideration

For now, focus on dry-run testing before live trading.

## Troubleshooting

### "OPTIONS not supported"
Ensure `trading_mode` is set to `"options"` in your config.

### "Exchange does not support options"
Currently only OKX is fully supported. Ensure `exchange.name` is `"okx"`.

### API Errors
- Verify your OKX API keys have options trading permissions
- Check that you have sufficient balance for margin requirements
- Ensure you're using the correct market symbols (e.g., "BTC-USD" not "BTC/USD")

## Advanced Topics

### Custom Strike Selection
Override `calculate_option_strike()` to implement your own strike selection logic:

```python
def calculate_option_strike(self, pair, option_type, current_rate, dataframe, **kwargs):
    # Use support/resistance levels
    if option_type == "call":
        resistance = dataframe['resistance'].iloc[-1]
        return resistance * 1.02  # Strike 2% above resistance
    else:
        support = dataframe['support'].iloc[-1]
        return support * 0.98  # Strike 2% below support
```

### Greeks-Based Position Management
```python
def adjust_trade_position(self, trade, order, **kwargs):
    # Fetch current Greeks
    greeks = self.exchange.fetch_greeks(trade.pair)

    # Adjust based on delta
    if greeks['delta'] < 0.2:
        # Delta too low, consider closing
        return None

    # Add to position if Greeks are favorable
    return stake_amount
```

## Support and Contributing

This is an experimental feature. Please report issues and contribute improvements!

- Test thoroughly in dry-run mode before live trading
- Start with small position sizes
- Monitor Greeks and time decay regularly
- Set appropriate stop losses

## License

This extension is part of Freqtrade and follows the same GPL-3.0 license.
