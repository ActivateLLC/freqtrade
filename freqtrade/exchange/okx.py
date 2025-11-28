import logging
from datetime import timedelta

import ccxt

from freqtrade.constants import BuySell
from freqtrade.enums import CandleType, MarginMode, PriceType, TradingMode
from freqtrade.exceptions import (
    DDosProtection,
    OperationalException,
    RetryableOrderError,
    TemporaryError,
)
from freqtrade.exchange import Exchange
from freqtrade.exchange.common import API_RETRY_COUNT, retrier
from freqtrade.exchange.exchange_types import CcxtOrder, FtHas
from freqtrade.misc import safe_value_fallback2
from freqtrade.util import dt_now, dt_ts


logger = logging.getLogger(__name__)


class Okx(Exchange):
    """Okx exchange class.

    Contains adjustments needed for Freqtrade to work with this exchange.
    """

    _ft_has: FtHas = {
        "ohlcv_candle_limit": 100,  # Warning, special case with data prior to X months
        "mark_ohlcv_timeframe": "4h",
        "funding_fee_timeframe": "8h",
        "stoploss_order_types": {"limit": "limit"},
        "stoploss_on_exchange": True,
        "trades_has_history": False,  # Endpoint doesn't have a "since" parameter
        "ws_enabled": True,
    }
    _ft_has_futures: FtHas = {
        "tickers_have_quoteVolume": False,
        "stop_price_type_field": "slTriggerPxType",
        "stop_price_type_value_mapping": {
            PriceType.LAST: "last",
            PriceType.MARK: "index",
            PriceType.INDEX: "mark",
        },
        "stoploss_blocks_assets": False,
        "ws_enabled": True,
    }

    _supported_trading_mode_margin_pairs: list[tuple[TradingMode, MarginMode]] = [
        (TradingMode.SPOT, MarginMode.NONE),
        # (TradingMode.MARGIN, MarginMode.CROSS),
        # (TradingMode.FUTURES, MarginMode.CROSS),
        (TradingMode.FUTURES, MarginMode.ISOLATED),
        (TradingMode.OPTIONS, MarginMode.NONE),
    ]

    net_only = True

    _ccxt_params: dict = {"options": {"brokerId": "ffb5405ad327SUDE"}}

    def ohlcv_candle_limit(
        self, timeframe: str, candle_type: CandleType, since_ms: int | None = None
    ) -> int:
        """
        Exchange ohlcv candle limit
        OKX has the following behaviour:
        * spot and futures:
            * 300 candles for regular candles
        * mark and premium-index:
            * 300 candles for up-to-date data
            * 100 candles for historic data
        * additional data:
            * 100 candles for additional candles
        :param timeframe: Timeframe to check
        :param candle_type: Candle-type
        :param since_ms: Starting timestamp
        :return: Candle limit as integer
        """
        if candle_type in (CandleType.FUTURES, CandleType.SPOT):
            return 300

        return super().ohlcv_candle_limit(timeframe, candle_type, since_ms)

    @retrier
    def additional_exchange_init(self) -> None:
        """
        Additional exchange initialization logic.
        .api will be available at this point.
        Must be overridden in child methods if required.
        """
        try:
            if self.trading_mode == TradingMode.FUTURES and not self._config["dry_run"]:
                accounts = self._api.fetch_accounts()
                self._log_exchange_response("fetch_accounts", accounts)
                if len(accounts) > 0:
                    self.net_only = accounts[0].get("info", {}).get("posMode") == "net_mode"
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Error in additional_exchange_init due to {e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    def _get_posSide(self, side: BuySell, reduceOnly: bool):
        if self.net_only:
            return "net"
        if not reduceOnly:
            # Enter
            return "long" if side == "buy" else "short"
        else:
            # Exit
            return "long" if side == "sell" else "short"

    def _get_params(
        self,
        side: BuySell,
        ordertype: str,
        leverage: float,
        reduceOnly: bool,
        time_in_force: str = "GTC",
    ) -> dict:
        params = super()._get_params(
            side=side,
            ordertype=ordertype,
            leverage=leverage,
            reduceOnly=reduceOnly,
            time_in_force=time_in_force,
        )
        if self.trading_mode == TradingMode.FUTURES and self.margin_mode:
            params["tdMode"] = self.margin_mode.value
            params["posSide"] = self._get_posSide(side, reduceOnly)
        return params

    def __fetch_leverage_already_set(self, pair: str, leverage: float, side: BuySell) -> bool:
        try:
            res_lev = self._api.fetch_leverage(
                symbol=pair,
                params={
                    "mgnMode": self.margin_mode.value,
                    "posSide": self._get_posSide(side, False),
                },
            )
            self._log_exchange_response("get_leverage", res_lev)
            already_set = all(float(x["lever"]) == leverage for x in res_lev["data"])
            return already_set

        except ccxt.BaseError:
            # Assume all errors as "not set yet"
            return False

    @retrier
    def _lev_prep(self, pair: str, leverage: float, side: BuySell, accept_fail: bool = False):
        if self.trading_mode != TradingMode.SPOT and self.margin_mode is not None:
            try:
                res = self._api.set_leverage(
                    leverage=leverage,
                    symbol=pair,
                    params={
                        "mgnMode": self.margin_mode.value,
                        "posSide": self._get_posSide(side, False),
                    },
                )
                self._log_exchange_response("set_leverage", res)

            except ccxt.DDoSProtection as e:
                raise DDosProtection(e) from e
            except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
                already_set = self.__fetch_leverage_already_set(pair, leverage, side)
                if not already_set:
                    raise TemporaryError(
                        f"Could not set leverage due to {e.__class__.__name__}. Message: {e}"
                    ) from e
            except ccxt.BaseError as e:
                raise OperationalException(e) from e

    def get_max_pair_stake_amount(self, pair: str, price: float, leverage: float = 1.0) -> float:
        if self.trading_mode == TradingMode.SPOT:
            return float("inf")  # Not actually inf, but this probably won't matter for SPOT

        if pair not in self._leverage_tiers:
            return float("inf")

        pair_tiers = self._leverage_tiers[pair]
        return pair_tiers[-1]["maxNotional"] / leverage

    def _get_stop_params(self, side: BuySell, ordertype: str, stop_price: float) -> dict:
        params = super()._get_stop_params(side, ordertype, stop_price)
        if self.trading_mode == TradingMode.FUTURES and self.margin_mode:
            params["tdMode"] = self.margin_mode.value
            params["posSide"] = self._get_posSide(side, True)
        return params

    def _convert_stop_order(self, pair: str, order_id: str, order: CcxtOrder) -> CcxtOrder:
        if (
            order.get("status", "open") == "closed"
            and (real_order_id := order.get("info", {}).get("ordId")) is not None
        ):
            # Once a order triggered, we fetch the regular followup order.
            order_reg = self.fetch_order(real_order_id, pair)
            self._log_exchange_response("fetch_stoploss_order1", order_reg)
            order_reg["id_stop"] = order_reg["id"]
            order_reg["id"] = order_id
            order_reg["type"] = "stoploss"
            order_reg["status_stop"] = "triggered"
            return order_reg
        order = self._order_contracts_to_amount(order)
        order["type"] = "stoploss"
        return order

    @retrier(retries=API_RETRY_COUNT)
    def fetch_stoploss_order(
        self, order_id: str, pair: str, params: dict | None = None
    ) -> CcxtOrder:
        if self._config["dry_run"]:
            return self.fetch_dry_run_order(order_id)

        try:
            params1 = {"stop": True}
            order_reg = self._api.fetch_order(order_id, pair, params=params1)
            self._log_exchange_response("fetch_stoploss_order", order_reg)
            return self._convert_stop_order(pair, order_id, order_reg)
        except (ccxt.OrderNotFound, ccxt.InvalidOrder):
            pass
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Could not get order due to {e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

        return self._fetch_stop_order_fallback(order_id, pair)

    def _fetch_stop_order_fallback(self, order_id: str, pair: str) -> CcxtOrder:
        params2 = {"stop": True, "ordType": "conditional"}
        for method in (
            self._api.fetch_open_orders,
            self._api.fetch_closed_orders,
            self._api.fetch_canceled_orders,
        ):
            try:
                orders = method(pair, params=params2)
                orders_f = [order for order in orders if order["id"] == order_id]
                if orders_f:
                    order = orders_f[0]
                    return self._convert_stop_order(pair, order_id, order)
            except (ccxt.OrderNotFound, ccxt.InvalidOrder):
                pass
            except ccxt.DDoSProtection as e:
                raise DDosProtection(e) from e
            except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
                raise TemporaryError(
                    f"Could not get order due to {e.__class__.__name__}. Message: {e}"
                ) from e
            except ccxt.BaseError as e:
                raise OperationalException(e) from e
        raise RetryableOrderError(f"StoplossOrder not found (pair: {pair} id: {order_id}).")

    def get_order_id_conditional(self, order: CcxtOrder) -> str:
        if order.get("type", "") == "stop":
            return safe_value_fallback2(order, order, "id_stop", "id")
        return order["id"]

    def cancel_stoploss_order(self, order_id: str, pair: str, params: dict | None = None) -> dict:
        params1 = {"stop": True}
        # 'ordType': 'conditional'
        #
        return self.cancel_order(
            order_id=order_id,
            pair=pair,
            params=params1,
        )

    def _fetch_orders_emulate(self, pair: str, since_ms: int) -> list[CcxtOrder]:
        orders = []

        orders = self._api.fetch_closed_orders(pair, since=since_ms)
        if since_ms < dt_ts(dt_now() - timedelta(days=6, hours=23)):
            # Regular fetch_closed_orders only returns 7 days of data.
            # Force usage of "archive" endpoint, which returns 3 months of data.
            params = {"method": "privateGetTradeOrdersHistoryArchive"}
            orders_hist = self._api.fetch_closed_orders(pair, since=since_ms, params=params)
            orders.extend(orders_hist)

        orders_open = self._api.fetch_open_orders(pair, since=since_ms)
        orders.extend(orders_open)
        return orders

    # Options Trading Methods for OKX

    @retrier
    def fetch_options_markets(self, base_currency: str = "BTC") -> list[dict]:
        """
        Fetch available options contracts for OKX.
        Uses OKX API endpoint: /api/v5/public/instruments?instType=OPTION&uly=BTC-USD
        :param base_currency: Base currency (e.g., 'BTC', 'ETH')
        :return: List of option contract details
        """
        try:
            # OKX uses format like 'BTC-USD' for underlying
            underlying = f"{base_currency}-USD"
            params = {"type": "option", "underlying": underlying}
            markets = self._api.fetch_markets(params=params)
            self._log_exchange_response("fetch_options_markets", markets)
            return markets
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Could not fetch options markets due to {e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    @retrier
    def fetch_option_chain(self, underlying: str, expiry_date: str | None = None) -> dict:
        """
        Fetch option chain for OKX.
        :param underlying: Underlying asset (e.g., 'BTC-USD')
        :param expiry_date: Expiry date in format 'YYMMDD' (optional)
        :return: Dictionary containing option chain data with strikes and prices
        """
        try:
            # Fetch all option instruments for the underlying
            params = {"type": "option", "underlying": underlying}
            if expiry_date:
                params["expiry"] = expiry_date

            markets = self._api.fetch_markets(params=params)
            self._log_exchange_response("fetch_option_chain", markets)

            # Group by expiry and strike
            option_chain = {}
            for market in markets:
                symbol = market.get("symbol", "")
                parsed = self.parse_option_symbol(symbol)
                expiry = parsed.get("expiry")
                strike = parsed.get("strike")
                option_type = parsed.get("option_type")

                if expiry not in option_chain:
                    option_chain[expiry] = {"calls": {}, "puts": {}}

                if option_type == "call":
                    option_chain[expiry]["calls"][strike] = market
                else:
                    option_chain[expiry]["puts"][strike] = market

            return option_chain
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Could not fetch option chain due to {e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    @retrier
    def fetch_greeks(self, symbol: str) -> dict[str, float]:
        """
        Fetch Greeks for an OKX option contract.
        Uses OKX API endpoint: /api/v5/public/opt-summary?instId=SYMBOL
        :param symbol: Option symbol (e.g., 'BTC-USD-250131-50000-C')
        :return: Dictionary with Greeks (delta, gamma, theta, vega)
        """
        try:
            # OKX provides Greeks through the opt-summary endpoint
            # This is a custom implementation using ccxt's publicGetPublicOptSummary
            params = {"instId": symbol}
            response = self._api.public_get_public_opt_summary(params)
            self._log_exchange_response("fetch_greeks", response)

            if response.get("code") == "0" and response.get("data"):
                data = response["data"][0] if response["data"] else {}
                return {
                    "delta": float(data.get("delta", 0)),
                    "gamma": float(data.get("gamma", 0)),
                    "theta": float(data.get("theta", 0)),
                    "vega": float(data.get("vega", 0)),
                    "iv": float(data.get("realVol", 0)),  # Implied volatility
                }
            return {"delta": 0, "gamma": 0, "theta": 0, "vega": 0, "iv": 0}
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            logger.warning(
                f"Could not fetch Greeks for {symbol} due to {e.__class__.__name__}. "
                f"Message: {e}. Returning zeros."
            )
            return {"delta": 0, "gamma": 0, "theta": 0, "vega": 0, "iv": 0}
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    @retrier
    def create_option_order(
        self,
        symbol: str,
        order_type: str,
        side: BuySell,
        amount: float,
        price: float | None = None,
        params: dict | None = None,
    ) -> dict:
        """
        Create an options order on OKX.
        :param symbol: Option symbol (e.g., 'BTC-USD-250131-50000-C')
        :param order_type: 'limit' or 'market'
        :param side: 'buy' or 'sell'
        :param amount: Number of contracts
        :param price: Limit price (for limit orders)
        :param params: Additional parameters
        :return: Order response from OKX
        """
        try:
            if params is None:
                params = {}

            # OKX options use tdMode=cash (options are cash-settled)
            params["tdMode"] = "cash"

            order = self._api.create_order(
                symbol=symbol, type=order_type, side=side, amount=amount, price=price, params=params
            )
            self._log_exchange_response("create_option_order", order)
            return order
        except ccxt.InsufficientFunds as e:
            raise RetryableOrderError(
                f"Insufficient funds to create option order on {self.name}. Message: {e}"
            ) from e
        except ccxt.InvalidOrder as e:
            raise OperationalException(
                f"Could not create option order on {self.name}. Message: {e}"
            ) from e
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Could not create option order due to {e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    @retrier
    def get_option_expiry_dates(self, underlying: str) -> list[str]:
        """
        Get available expiry dates for OKX options on an underlying asset.
        :param underlying: Underlying asset (e.g., 'BTC-USD')
        :return: List of expiry dates in YYMMDD format
        """
        try:
            params = {"type": "option", "underlying": underlying}
            markets = self._api.fetch_markets(params=params)
            self._log_exchange_response("get_option_expiry_dates", markets)

            # Extract unique expiry dates
            expiry_dates = set()
            for market in markets:
                symbol = market.get("symbol", "")
                try:
                    parsed = self.parse_option_symbol(symbol)
                    expiry_dates.add(parsed.get("expiry"))
                except (ValueError, IndexError):
                    continue

            return sorted(list(expiry_dates))
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Could not get expiry dates due to {e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    @retrier
    def create_batch_orders(self, orders: list[dict]) -> dict:
        """
        Create multiple orders simultaneously (useful for spreads).
        Uses OKX batch-orders endpoint for atomic execution.

        :param orders: List of order dictionaries, each containing:
            - symbol: Option symbol
            - type: Order type ('limit' or 'market')
            - side: 'buy' or 'sell'
            - amount: Number of contracts
            - price: Limit price (optional for market orders)
        :return: Batch order response from OKX
        """
        try:
            # Format orders for OKX batch endpoint
            okx_orders = []
            for order in orders:
                okx_order = {
                    "instId": order["symbol"],
                    "tdMode": "cash",  # Options use cash settlement
                    "side": order["side"],
                    "ordType": order.get("type", "limit"),
                    "sz": str(order["amount"]),
                }

                if order.get("price"):
                    okx_order["px"] = str(order["price"])

                okx_orders.append(okx_order)

            # Submit batch order
            response = self._api.private_post_trade_batch_orders({"orders": okx_orders})
            self._log_exchange_response("create_batch_orders", response)

            return response

        except ccxt.InsufficientFunds as e:
            raise RetryableOrderError(
                f"Insufficient funds for batch order on {self.name}. Message: {e}"
            ) from e
        except ccxt.InvalidOrder as e:
            raise OperationalException(
                f"Could not create batch order on {self.name}. Message: {e}"
            ) from e
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Could not create batch order due to {e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    @retrier
    def create_spread_order(
        self,
        leg1_symbol: str,
        leg1_side: BuySell,
        leg1_amount: float,
        leg1_price: float | None,
        leg2_symbol: str,
        leg2_side: BuySell,
        leg2_amount: float,
        leg2_price: float | None,
        order_type: str = "limit",
    ) -> dict:
        """
        Create a two-legged spread order with atomic execution.

        Both legs will be submitted simultaneously using OKX batch orders.
        This reduces execution risk compared to sequential orders.

        :param leg1_symbol: First leg option symbol
        :param leg1_side: 'buy' or 'sell'
        :param leg1_amount: Number of contracts for leg 1
        :param leg1_price: Limit price for leg 1
        :param leg2_symbol: Second leg option symbol
        :param leg2_side: 'buy' or 'sell'
        :param leg2_amount: Number of contracts for leg 2
        :param leg2_price: Limit price for leg 2
        :param order_type: 'limit' or 'market'
        :return: Batch order response
        """
        orders = [
            {
                "symbol": leg1_symbol,
                "side": leg1_side,
                "amount": leg1_amount,
                "price": leg1_price,
                "type": order_type,
            },
            {
                "symbol": leg2_symbol,
                "side": leg2_side,
                "amount": leg2_amount,
                "price": leg2_price,
                "type": order_type,
            },
        ]

        logger.info(
            f"Creating spread order: {leg1_side.upper()} {leg1_symbol} @ {leg1_price} | "
            f"{leg2_side.upper()} {leg2_symbol} @ {leg2_price}"
        )

        return self.create_batch_orders(orders)

    def cancel_batch_orders(self, order_ids: list[str], pair: str) -> dict:
        """
        Cancel multiple orders simultaneously.

        :param order_ids: List of order IDs to cancel
        :param pair: Trading pair
        :return: Cancel response from OKX
        """
        try:
            cancel_requests = [{"instId": pair, "ordId": oid} for oid in order_ids]

            response = self._api.private_post_trade_cancel_batch_orders(cancel_requests)
            self._log_exchange_response("cancel_batch_orders", response)

            return response

        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Could not cancel batch orders due to {e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e


class Myokx(Okx):
    """MyOkx exchange class.
    Minimal adjustment to disable futures trading for the EU subsidiary of Okx
    """

    _supported_trading_mode_margin_pairs: list[tuple[TradingMode, MarginMode]] = [
        (TradingMode.SPOT, MarginMode.NONE),
    ]


class Okxus(Okx):
    """Okxus exchange class.
    Minimal adjustment to disable futures trading for the US subsidiary of Okx
    """

    _supported_trading_mode_margin_pairs: list[tuple[TradingMode, MarginMode]] = [
        (TradingMode.SPOT, MarginMode.NONE),
    ]
