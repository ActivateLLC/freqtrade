# Options Spreads Trading Guide for OKX

## Overview

Options spreads involve simultaneously trading multiple option contracts to create defined risk/reward profiles. This guide explains how to implement spreads on OKX using freqtrade.

## OKX Spread Support

### ✅ What OKX Supports

OKX **DOES support** options spreads through multiple approaches:

1. **Individual Leg Orders** (What we implemented)
   - Place separate buy/sell orders for each leg
   - Freqtrade uses `position_adjustment_enable` to add multiple legs
   - Each leg is tracked as a separate order within the same trade

2. **Spread Order Types** (Native OKX feature)
   - OKX supports native spread orders via their API
   - Can submit both legs as a single spread order
   - Better fills and reduced slippage

3. **Portfolio Margin Mode**
   - OKX calculates margin requirements for the net position
   - Spreads require less margin than naked options
   - Margin is based on max loss of the spread

### 🔧 OKX API Endpoints for Spreads

OKX provides these relevant endpoints:

```
POST /api/v5/trade/order          # Single leg order
POST /api/v5/trade/batch-orders   # Multiple legs simultaneously
GET  /api/v5/account/positions    # View spread positions
GET  /api/v5/public/opt-summary   # Greeks for option contracts
```

## Implementation Approaches

### Approach 1: Sequential Leg Orders (Current Implementation)

**How it works:**
1. First order creates the long leg (buy)
2. Position adjustment adds the short leg (sell)
3. Both orders tracked in the same Trade object

**Pros:**
- Simple implementation
- Uses existing freqtrade infrastructure
- Easy to debug and monitor

**Cons:**
- Leg execution risk (first leg fills, second doesn't)
- Potential price slippage between legs
- Requires monitoring of partial fills

**Example:** See `options_spread_strategy.py`

### Approach 2: Batch Orders (Better for Spreads)

For better execution, we can enhance the OKX integration to support batch orders:

```python
# In freqtrade/exchange/okx.py
def create_spread_order(
    self,
    leg1_symbol: str,
    leg1_side: str,
    leg2_symbol: str,
    leg2_side: str,
    amount: float,
    leg1_price: float | None = None,
    leg2_price: float | None = None,
) -> dict:
    """
    Create a spread order with two legs submitted simultaneously.
    Uses OKX batch-orders endpoint.
    """
    orders = [
        {
            "instId": leg1_symbol,
            "tdMode": "cash",
            "side": leg1_side,
            "ordType": "limit",
            "px": str(leg1_price),
            "sz": str(amount),
        },
        {
            "instId": leg2_symbol,
            "tdMode": "cash",
            "side": leg2_side,
            "ordType": "limit",
            "px": str(leg2_price),
            "sz": str(amount),
        }
    ]

    response = self._api.private_post_trade_batch_orders({"orders": orders})
    return response
```

## Spread Types Supported

### 1. Bull Call Spread (Bullish, Debit Spread)

**Structure:**
- **Buy** 1 call at lower strike (e.g., $50,000)
- **Sell** 1 call at higher strike (e.g., $52,000)

**When to use:** Moderately bullish on BTC

**Max Profit:** Strike difference - Net premium paid
**Max Loss:** Net premium paid

**Example:**
```
BTC at $50,000
Buy  1x BTC-USD-250228-50000-C @ $1,500
Sell 1x BTC-USD-250228-52000-C @ $800
Net cost: $700

Max profit: $2,000 - $700 = $1,300 (if BTC > $52,000)
Max loss: $700 (if BTC < $50,000)
Breakeven: $50,700
```

### 2. Bear Put Spread (Bearish, Debit Spread)

**Structure:**
- **Buy** 1 put at higher strike (e.g., $50,000)
- **Sell** 1 put at lower strike (e.g., $48,000)

**When to use:** Moderately bearish on BTC

**Max Profit:** Strike difference - Net premium paid
**Max Loss:** Net premium paid

**Example:**
```
BTC at $50,000
Buy  1x BTC-USD-250228-50000-P @ $1,400
Sell 1x BTC-USD-250228-48000-P @ $600
Net cost: $800

Max profit: $2,000 - $800 = $1,200 (if BTC < $48,000)
Max loss: $800 (if BTC > $50,000)
Breakeven: $49,200
```

### 3. Bull Put Spread (Bullish, Credit Spread)

**Structure:**
- **Sell** 1 put at higher strike (e.g., $50,000)
- **Buy** 1 put at lower strike (e.g., $48,000)

**When to use:** Moderately bullish, want to collect premium

**Max Profit:** Net premium received
**Max Loss:** Strike difference - Net premium received

**Example:**
```
BTC at $52,000
Sell 1x BTC-USD-250228-50000-P @ $800
Buy  1x BTC-USD-250228-48000-P @ $300
Net credit: $500

Max profit: $500 (if BTC > $50,000)
Max loss: $2,000 - $500 = $1,500 (if BTC < $48,000)
Breakeven: $49,500
```

### 4. Bear Call Spread (Bearish, Credit Spread)

**Structure:**
- **Sell** 1 call at lower strike (e.g., $50,000)
- **Buy** 1 call at higher strike (e.g., $52,000)

**When to use:** Moderately bearish, want to collect premium

**Max Profit:** Net premium received
**Max Loss:** Strike difference - Net premium received

**Example:**
```
BTC at $48,000
Sell 1x BTC-USD-250228-50000-C @ $600
Buy  1x BTC-USD-250228-52000-C @ $200
Net credit: $400

Max profit: $400 (if BTC < $50,000)
Max loss: $2,000 - $400 = $1,600 (if BTC > $52,000)
Breakeven: $50,400
```

## OKX Margin Requirements

### Portfolio Margin (Recommended for Spreads)

OKX uses portfolio margin for options, which provides several advantages:

1. **Reduced Margin:** Spreads require much less margin than naked options
2. **Net Risk Calculation:** Margin is based on max possible loss
3. **Cross-Currency Offsets:** Can offset positions across different underlyings

**Example Margin Calculation:**

Bull Call Spread:
```
Long  BTC-USD-250228-50000-C: Initial margin ~15% of notional
Short BTC-USD-250228-52000-C: Reduces margin requirement
Net margin: ~$700-1,000 (much less than naked call)
```

For exact margin requirements, check:
```python
# Query position risk
risk = exchange._api.private_get_account_position_risk({
    "instType": "OPTION"
})
```

## Configuration for Spread Trading

### config.json

```json
{
    "trading_mode": "options",
    "margin_mode": "",

    "max_open_trades": 5,
    "stake_amount": 200,

    "position_adjustment_enable": true,
    "max_entry_position_adjustment": 3,

    "options_min_days_to_expiry": 7,
    "options_max_days_to_expiry": 30,

    "exchange": {
        "name": "okx",
        "key": "YOUR_KEY",
        "secret": "YOUR_SECRET",
        "password": "YOUR_PASSWORD"
    }
}
```

### Key Settings for Spreads

- `position_adjustment_enable: true` - **REQUIRED** for multi-leg spreads
- `max_entry_position_adjustment: 1-3` - Number of additional legs (1 for vertical spreads, 3 for iron condors)
- `stake_amount` - Should be 2x normal since you need capital for both legs

## Using the Spread Strategy

### Basic Usage

```bash
freqtrade trade --config config_options.json --strategy OptionsSpreadStrategy
```

### Strategy Configuration

```python
class MySpreadStrategy(OptionsSpreadStrategy):
    # Choose spread type
    spread_type = "bull_call"  # or "bear_put", "bull_put", "bear_call"

    # Spread width (distance between strikes)
    spread_width_pct = 0.05  # 5% spread width

    # First leg offset from current price
    first_leg_offset_pct = 0.02  # 2% OTM

    # Expiry preferences
    min_days_to_expiry = 7
    max_days_to_expiry = 30
```

## Advanced Spread Strategies

### Iron Condor

An iron condor combines a bull put spread and a bear call spread:

```python
def adjust_trade_position(self, trade, ...):
    """
    Create iron condor with 4 legs:
    1. Buy OTM put (protection)
    2. Sell closer put (collect premium)
    3. Sell closer call (collect premium)
    4. Buy OTM call (protection)
    """
    # Implementation requires max_entry_position_adjustment = 3
    pass
```

### Calendar Spread

Different expiries, same strike:

```python
def select_option_contract(self, ...):
    # Near-term expiry for short leg
    short_expiry = "250131"  # 30 days

    # Far-term expiry for long leg (added via position adjustment)
    long_expiry = "250228"   # 60 days

    # Same strike for both
    strike = current_rate * 1.05
```

## Risk Management for Spreads

### Position Sizing

Since spreads have defined max loss:

```python
# Max loss = Strike difference - Net premium (for debits)
# Max loss = Strike difference - Net premium (for credits)

max_loss_per_spread = 1000  # $1,000 max loss
position_size = account_balance * 0.02 / max_loss_per_spread
```

### Greeks Management

Monitor combined Greeks for the spread:

```python
def custom_exit(self, pair, trade, ...):
    # Calculate net delta for the spread
    # (Approximate - would need to fetch both legs' Greeks)

    if trade.delta and abs(trade.delta) < 0.1:
        return "delta_neutral"  # Spread has minimal directional risk
```

### Time Decay (Theta)

Spreads have different theta profiles:

- **Debit Spreads:** Negative theta (lose value over time)
  - Exit before last week to expiry

- **Credit Spreads:** Positive theta (gain value over time)
  - Can hold closer to expiry to maximize theta gain

```python
def custom_stoploss(self, pair, trade, ...):
    if not trade.expiry_date:
        return None

    days_to_expiry = (trade.expiry_date - current_time).days

    # For debit spreads, exit early to avoid theta decay
    if is_debit_spread and days_to_expiry <= 5:
        return -0.50  # Tight stop

    # For credit spreads, can hold longer
    if is_credit_spread and days_to_expiry <= 2:
        return -0.70  # Looser stop
```

## Monitoring Spread Positions

### Via API

```python
# Get all option positions
positions = exchange._api.private_get_account_positions({
    "instType": "OPTION"
})

for pos in positions['data']:
    print(f"Symbol: {pos['instId']}")
    print(f"Size: {pos['pos']}")
    print(f"Entry: {pos['avgPx']}")
    print(f"P&L: {pos['upl']}")  # Unrealized P&L
```

### Via Freqtrade

```bash
# Check trade status
freqtrade show_trades --config config_options.json

# View profit
freqtrade show_profit --config config_options.json
```

## Troubleshooting

### Spread Not Completing (Second Leg Fails)

**Issue:** First leg fills but second leg doesn't

**Solutions:**
1. Check liquidity at second strike
2. Use wider limit orders
3. Implement retry logic in `adjust_trade_position`
4. Consider market orders for second leg

```python
def adjust_trade_position(self, ...):
    # Add retry with wider spread
    if retry_count < 3:
        # Widen limit price by 1% per retry
        adjusted_price = original_price * (1 + 0.01 * retry_count)
```

### Insufficient Margin

**Issue:** OKX rejects second leg due to margin

**Solutions:**
1. Increase account balance
2. Close other positions to free margin
3. Use smaller position sizes
4. Verify portfolio margin is enabled

### Spread Fills at Poor Price

**Issue:** Legs fill at unfavorable prices

**Solutions:**
1. Use limit orders instead of market
2. Implement `confirm_trade_entry` with price checks
3. Consider OKX batch orders for atomic execution
4. Check bid-ask spread before entry

## Next Steps: Native Spread Orders

For production use, consider implementing native OKX spread orders:

```python
# Enhancement to freqtrade/exchange/okx.py
@retrier
def create_option_spread_order(
    self,
    spread_type: str,  # "vertical_call", "vertical_put", etc.
    underlying: str,
    expiry: str,
    long_strike: float,
    short_strike: float,
    amount: float,
    max_spread_price: float,
) -> dict:
    """
    Create native spread order on OKX for atomic execution.
    Both legs fill simultaneously or not at all.
    """
    # Implementation using OKX spread order API
    pass
```

This would eliminate leg execution risk and provide better fills.

## Resources

- **OKX Options API Docs:** https://www.okx.com/docs-v5/en/#order-book-trading-trade
- **OKX Options Trading Guide:** https://www.okx.com/help/options-trading
- **OKX Margin Calculator:** https://www.okx.com/trade-market/info/option/margin-calculator

## Summary

✅ **OKX fully supports options spreads**
✅ **Current implementation works via position adjustment**
✅ **Sequential leg orders have some execution risk**
✅ **Native spread orders would be ideal enhancement**
✅ **Portfolio margin reduces capital requirements**

The provided `OptionsSpreadStrategy` demonstrates a working implementation. For production trading, consider adding native batch order support for atomic spread execution.
