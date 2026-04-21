import asyncio
import aiohttp
import websockets
import json
import logging
from typing import Dict, List, Tuple, Optional

import numpy as np

logger = logging.getLogger(__name__)


class OrderBookManager:
    """
    Maintains a real-time local order book via Binance WebSocket depthUpdate.

    Improvements over v1:
    - Local-neighbour wall detection (not global mean) → fewer false walls
    - Lock-free buffer during init
    - Reconnect with full re-snapshot
    """

    SNAPSHOT_URL = "https://fapi.binance.com/fapi/v1/depth"
    WS_URL       = "wss://fstream.binance.com/ws"

    def __init__(self, symbol: str, scan_pct: float = 0.02,
                 layers: int = 3, wall_local_ratio: float = 4.0):
        self.symbol          = symbol.upper()
        self.scan_pct        = scan_pct
        self.layers          = layers
        self.wall_local_ratio = wall_local_ratio

        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.last_update_id  = 0
        self.initialized     = False

        self.upper_walls: List[Tuple[float, float]] = []
        self.lower_walls: List[Tuple[float, float]] = []

        self._lock   = asyncio.Lock()
        self._buffer = []

    # ------------------------------------------------------------------ #
    #  Snapshot & init                                                      #
    # ------------------------------------------------------------------ #

    async def fetch_snapshot(self):
        async with aiohttp.ClientSession() as session:
            params = {"symbol": self.symbol, "limit": 1000}
            async with session.get(self.SNAPSHOT_URL, params=params) as resp:
                data = await resp.json()

        async with self._lock:
            self.bids = {float(p): float(q) for p, q in data["bids"]}
            self.asks = {float(p): float(q) for p, q in data["asks"]}
            self.last_update_id = data["lastUpdateId"]
            self.initialized = True

            for diff in self._buffer:
                self._apply_diff(diff)
            self._buffer.clear()

        logger.info(f"[{self.symbol}] Snapshot loaded. lastUpdateId={self.last_update_id}")

    def _apply_diff(self, data: dict):
        if data["u"] <= self.last_update_id:
            return
        for p, q in data["b"]:
            price, qty = float(p), float(q)
            if qty == 0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = qty
        for p, q in data["a"]:
            price, qty = float(p), float(q)
            if qty == 0:
                self.asks.pop(price, None)
            else:
                self.asks[price] = qty
        self.last_update_id = data["u"]

    # ------------------------------------------------------------------ #
    #  WebSocket stream                                                     #
    # ------------------------------------------------------------------ #

    async def stream(self):
        stream_name = f"{self.symbol.lower()}@depth@100ms"
        uri = f"{self.WS_URL}/{stream_name}"

        while True:
            try:
                async with websockets.connect(uri, ping_interval=20) as ws:
                    logger.info(f"[{self.symbol}] OrderBook WS connected")
                    async for raw in ws:
                        data = json.loads(raw)
                        async with self._lock:
                            if not self.initialized:
                                self._buffer.append(data)
                            else:
                                self._apply_diff(data)
            except Exception as e:
                logger.warning(f"[{self.symbol}] OrderBook WS error: {e}, reconnecting…")
                self.initialized = False
                await asyncio.sleep(3)
                await self.fetch_snapshot()

    # ------------------------------------------------------------------ #
    #  Wall scanning                                                        #
    # ------------------------------------------------------------------ #

    def get_mid_price(self) -> Optional[float]:
        if not self.bids or not self.asks:
            return None
        return (max(self.bids) + min(self.asks)) / 2

    async def scan_walls(self) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
        async with self._lock:
            mid = self.get_mid_price()
            if mid is None:
                return [], []

            upper_bound = mid * (1 + self.scan_pct)
            lower_bound = mid * (1 - self.scan_pct)

            asks_in_range = {p: q for p, q in self.asks.items()
                             if mid < p <= upper_bound}
            bids_in_range = {p: q for p, q in self.bids.items()
                             if lower_bound <= p < mid}

            upper_walls = self._find_walls(asks_in_range, side="ask")
            lower_walls = self._find_walls(bids_in_range, side="bid")

        self.upper_walls = upper_walls[:self.layers]
        self.lower_walls = lower_walls[:self.layers]
        return self.upper_walls, self.lower_walls

    def _find_walls(self, book_side: Dict[float, float], side: str) -> List[Tuple[float, float]]:
        """
        Wall = level whose qty is >= wall_local_ratio × mean of ±5 neighbouring levels.
        Much more precise than v1 global-mean approach.
        """
        if len(book_side) < 5:
            return []

        prices = sorted(book_side.keys(), reverse=(side == "bid"))
        walls  = []

        for i, price in enumerate(prices):
            neighbours = (
                prices[max(0, i - 5):i] +
                prices[i + 1: min(len(prices), i + 6)]
            )
            if not neighbours:
                continue
            local_mean = np.mean([book_side[p] for p in neighbours])
            if local_mean > 0 and book_side[price] / local_mean >= self.wall_local_ratio:
                walls.append((price, book_side[price]))

        return walls

    def get_nearest_walls(self) -> Tuple[Optional[Tuple], Optional[Tuple]]:
        upper = self.upper_walls[0] if self.upper_walls else None
        lower = self.lower_walls[0] if self.lower_walls else None
        return upper, lower
