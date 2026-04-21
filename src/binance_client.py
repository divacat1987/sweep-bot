import asyncio
import hashlib
import hmac
import logging
import time
import urllib.parse
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

LIVE_BASE = "https://fapi.binance.com"
TEST_BASE = "https://testnet.binancefuture.com"


class BinanceClient:
    def __init__(self, api_key: str, secret_key: str, testnet: bool = False):
        self.api_key    = api_key
        self.secret_key = secret_key
        self.base_url   = TEST_BASE if testnet else LIVE_BASE
        self.testnet    = testnet
        self._session: Optional[aiohttp.ClientSession] = None
        self.daily_pnl: float = 0.0
        self._pnl_reset_day: int = -1

    # ── Session ───────────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-MBX-APIKEY": self.api_key}
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Signature ─────────────────────────────────────────────────────

    def _sign(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        query = urllib.parse.urlencode(params)
        signature = hmac.new(
            self.secret_key.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params

    # ── Core request ──────────────────────────────────────────────────

    async def _request(self, method: str, path: str, params: dict = None,
                       signed: bool = True) -> Optional[dict]:
        session = await self._get_session()
        params  = params or {}
        if signed:
            params = self._sign(params)
        url = f"{self.base_url}{path}"
        try:
            if method == "GET":
                async with session.get(url, params=params) as resp:
                    return await self._handle(resp)
            elif method == "POST":
                async with session.post(url, params=params) as resp:
                    return await self._handle(resp)
            elif method == "DELETE":
                async with session.delete(url, params=params) as resp:
                    return await self._handle(resp)
        except aiohttp.ClientError as e:
            logger.error(f"[Binance] Request error {path}: {e}")
            return None

    async def _handle(self, resp) -> Optional[dict]:
        data = await resp.json()
        if resp.status != 200:
            logger.error(f"[Binance] API error {resp.status}: {data}")
            return None
        return data

    # ── Leverage & margin ─────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        result = await self._request("POST", "/fapi/v1/leverage", {
            "symbol": symbol, "leverage": leverage,
        })
        if result:
            logger.info(f"[Binance] Leverage set: {symbol} {leverage}x")
            return True
        return False

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> bool:
        await self._request("POST", "/fapi/v1/marginType", {
            "symbol": symbol, "marginType": margin_type,
        })
        logger.info(f"[Binance] Margin type set: {symbol} {margin_type}")
        return True

    # ── Orders ────────────────────────────────────────────────────────

    async def place_market_order(self, symbol: str, side: str,
                                  quantity: float) -> Optional[dict]:
        """進場市價單"""
        params = {
            "symbol":   symbol,
            "side":     side,
            "type":     "MARKET",
            "quantity": quantity,
        }
        result = await self._request("POST", "/fapi/v1/order", params)
        if result:
            logger.info(
                f"[Binance] Market order placed: {symbol} {side} {quantity} "
                f"orderId={result.get('orderId')}"
            )
        return result

    async def place_stop_market_order(self, symbol: str, side: str,
                                       stop_price: float, quantity: float,
                                       close_position: bool = True) -> Optional[dict]:
        """止損掛單 STOP_MARKET"""
        params = {
            "symbol":        symbol,
            "side":          side,
            "type":          "STOP_MARKET",
            "stopPrice":     round(stop_price, 6),
            "closePosition": "true" if close_position else "false",
        }
        if not close_position:
            params["quantity"] = quantity

        result = await self._request("POST", "/fapi/v1/order", params)
        if result:
            logger.info(
                f"[Binance] Stop-market placed: {symbol} {side} @ {stop_price} "
                f"orderId={result.get('orderId')}"
            )
        return result

    async def place_take_profit_market_order(self, symbol: str, side: str,
                                              stop_price: float,
                                              close_position: bool = True) -> Optional[dict]:
        """止盈掛單 TAKE_PROFIT_MARKET"""
        params = {
            "symbol":        symbol,
            "side":          side,
            "type":          "TAKE_PROFIT_MARKET",
            "stopPrice":     round(stop_price, 6),
            "closePosition": "true" if close_position else "false",
        }
        result = await self._request("POST", "/fapi/v1/order", params)
        if result:
            logger.info(
                f"[Binance] TP placed: {symbol} {side} @ {stop_price} "
                f"orderId={result.get('orderId')}"
            )
        return result

    async def close_position_market(self, symbol: str, side: str,
                                     quantity: float) -> Optional[dict]:
        """
        市價平倉（reduceOnly）。
        從幣安查實際倉位數量，避免 -2019 Margin insufficient 錯誤。
        side = 原始倉位方向 ('LONG' or 'SHORT')
        """
        actual_qty = await self._get_actual_position_qty(symbol)

        if actual_qty is None:
            logger.error(f"[Binance] Could not fetch position for {symbol}")
            return None

        if actual_qty == 0:
            logger.warning(f"[Binance] No open position for {symbol}, already closed?")
            return {"skipped": True, "reason": "no_position"}

        close_side = "SELL" if side == "LONG" else "BUY"
        params = {
            "symbol":     symbol,
            "side":       close_side,
            "type":       "MARKET",
            "quantity":   actual_qty,
            "reduceOnly": "true",
        }
        result = await self._request("POST", "/fapi/v1/order", params)
        if result:
            logger.info(
                f"[Binance] Position closed: {symbol} {close_side} "
                f"qty={actual_qty} orderId={result.get('orderId')}"
            )
        return result

    async def cancel_all_orders(self, symbol: str) -> bool:
        result = await self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
        if result is not None:
            logger.info(f"[Binance] All orders cancelled: {symbol}")
            return True
        return False

    async def _get_actual_position_qty(self, symbol: str) -> Optional[float]:
        """從幣安查實際倉位數量（絕對值）"""
        result = await self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        if result:
            for pos in result:
                if pos.get("symbol") == symbol:
                    amt = float(pos.get("positionAmt", 0))
                    return abs(amt)
        return None

    # ── Account ───────────────────────────────────────────────────────

    async def get_balance_usdt(self) -> Optional[float]:
        result = await self._request("GET", "/fapi/v2/balance")
        if result:
            for asset in result:
                if asset.get("asset") == "USDT":
                    return float(asset.get("availableBalance", 0))
        return None

    async def get_symbol_info(self, symbol: str) -> Optional[dict]:
        result = await self._request("GET", "/fapi/v1/exchangeInfo", signed=False)
        if result:
            for s in result.get("symbols", []):
                if s["symbol"] == symbol:
                    return s
        return None

    async def calc_quantity(self, symbol: str, usdt_size: float,
                             price: float, leverage: int) -> Optional[float]:
        """計算下單數量，符合幣安 stepSize 精度要求"""
        info     = await self.get_symbol_info(symbol)
        raw_qty  = (usdt_size * leverage) / price
        step_size = None

        if info:
            for f in info.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    step_size = float(f["stepSize"])
                    break

        if step_size:
            precision = len(str(step_size).rstrip("0").split(".")[-1])
            qty = round(raw_qty - (raw_qty % step_size), precision)
        else:
            qty = round(raw_qty, 3)

        return qty if qty > 0 else None

    # ── Daily P&L ─────────────────────────────────────────────────────

    def record_pnl(self, pnl_usdt: float):
        today = time.gmtime().tm_yday
        if today != self._pnl_reset_day:
            self.daily_pnl      = 0.0
            self._pnl_reset_day = today
        self.daily_pnl += pnl_usdt

    def is_daily_limit_hit(self, limit: float) -> bool:
        today = time.gmtime().tm_yday
        if today != self._pnl_reset_day:
            return False
        return self.daily_pnl <= -abs(limit)
