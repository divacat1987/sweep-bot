import asyncio
import aiohttp
import websockets
import json
import logging
from collections import deque
from dataclasses import dataclass
from typing import Optional, Deque, Tuple

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Data                                                                 #
# ------------------------------------------------------------------ #

@dataclass
class Kline:
    open_time:  int
    open:       float
    high:       float
    low:        float
    close:      float
    volume:     float
    closed:     bool = False


@dataclass
class SMCStructure:
    """
    Current Market Structure from 5-min klines.
    Mirrors the Pine Script logic:
      direction=1 → bearish (last break was downward)
      direction=2 → bullish (last break was upward)
    """
    direction:      int    # 1=bearish, 2=bullish, 0=undefined
    structure_high: float
    structure_low:  float

    # OTE Fibonacci levels (0.618 ~ 0.786)
    fib_0618: float
    fib_0786: float

    # Wider zone boundary (1.0 = entry point of the swing)
    fib_1000: float


# ------------------------------------------------------------------ #
#  Manager                                                              #
# ------------------------------------------------------------------ #

class KlineManager:
    """
    Maintains a rolling window of 5-minute closed klines via WebSocket.
    Computes SMC Current Structure and OTE Fibonacci levels on each close.

    OTE zone = Fib 0.618 ~ 0.786 of the current structure swing.

    For a BEARISH structure (direction=1, high→low swing):
        fib_0618 = high - range * (1 - 0.618)  ← lower boundary
        fib_0786 = high - range * (1 - 0.786)  ← upper boundary
        Entry is in OTE if: fib_0618 <= price <= fib_0786

    For a BULLISH structure (direction=2, low→high swing):
        fib_0618 = low + range * (1 - 0.618)   ← upper boundary
        fib_0786 = low + range * (1 - 0.786)   ← lower boundary
        Entry is in OTE if: fib_0786 <= price <= fib_0618
    """

    REST_URL = "https://fapi.binance.com/fapi/v1/klines"
    WS_URL   = "wss://fstream.binance.com/ws"

    INTERVAL     = "5m"
    LOOKBACK     = 100   # bars to maintain
    STRUCT_LOOKBACK = 10  # bars for structure high/low detection

    def __init__(self, symbol: str):
        self.symbol   = symbol.upper()
        self.klines:  Deque[Kline] = deque(maxlen=self.LOOKBACK)
        self.structure: Optional[SMCStructure] = None
        self._current_kline: Optional[Kline] = None

    # ------------------------------------------------------------------ #
    #  Bootstrap                                                            #
    # ------------------------------------------------------------------ #

    async def fetch_history(self):
        """Fetch last N closed 5m klines from REST."""
        async with aiohttp.ClientSession() as session:
            params = {
                "symbol":   self.symbol,
                "interval": self.INTERVAL,
                "limit":    self.LOOKBACK,
            }
            async with session.get(self.REST_URL, params=params) as resp:
                data = await resp.json()

        for k in data[:-1]:  # exclude the currently open bar
            self.klines.append(Kline(
                open_time = k[0],
                open      = float(k[1]),
                high      = float(k[2]),
                low       = float(k[3]),
                close     = float(k[4]),
                volume    = float(k[5]),
                closed    = True,
            ))

        self._update_structure()
        logger.info(
            f"[{self.symbol}] KlineManager: loaded {len(self.klines)} bars, "
            f"structure={self.structure}"
        )

    # ------------------------------------------------------------------ #
    #  WebSocket stream                                                     #
    # ------------------------------------------------------------------ #

    async def stream(self):
        stream_name = f"{self.symbol.lower()}@kline_{self.INTERVAL}"
        uri = f"{self.WS_URL}/{stream_name}"

        while True:
            try:
                async with websockets.connect(uri, ping_interval=20) as ws:
                    logger.info(f"[{self.symbol}] Kline WS connected ({self.INTERVAL})")
                    async for raw in ws:
                        data = json.loads(raw)
                        k    = data["k"]
                        kline = Kline(
                            open_time = k["t"],
                            open      = float(k["o"]),
                            high      = float(k["h"]),
                            low       = float(k["l"]),
                            close     = float(k["c"]),
                            volume    = float(k["v"]),
                            closed    = k["x"],
                        )
                        self._current_kline = kline

                        if kline.closed:
                            self.klines.append(kline)
                            self._update_structure()
                            logger.debug(
                                f"[{self.symbol}] Kline closed: "
                                f"H={kline.high} L={kline.low} C={kline.close} "
                                f"→ struct={self.structure}"
                            )
            except Exception as e:
                logger.warning(f"[{self.symbol}] Kline WS error: {e}, reconnecting…")
                await asyncio.sleep(3)

    # ------------------------------------------------------------------ #
    #  SMC Structure calculation                                            #
    # ------------------------------------------------------------------ #

    def _update_structure(self):
        """
        Detect current market structure from recent closed klines.
        Mirrors Pine Script logic with STRUCT_LOOKBACK=10 bars.
        """
        bars = list(self.klines)
        n    = len(bars)
        if n < self.STRUCT_LOOKBACK + 2:
            return

        lb = self.STRUCT_LOOKBACK

        # Find swing high and swing low within lookback
        recent   = bars[-lb:]
        s_high   = max(k.high  for k in recent)
        s_low    = min(k.low   for k in recent)
        hi_idx   = max(range(len(recent)), key=lambda i: recent[i].high)
        lo_idx   = min(range(len(recent)), key=lambda i: recent[i].low)

        # Determine direction: which came last, swing high or swing low
        if hi_idx > lo_idx:
            direction = 2   # bullish: low formed first, then high
        elif lo_idx > hi_idx:
            direction = 1   # bearish: high formed first, then low
        else:
            direction = 0

        s_range = s_high - s_low
        if s_range == 0:
            return

        if direction == 2:
            # Bullish: fib from low up to high, retracement zone
            fib_0618 = s_low + s_range * (1 - 0.618)   # 0.382 level from low
            fib_0786 = s_low + s_range * (1 - 0.786)   # 0.214 level from low
            fib_1000 = s_low                             # 1.0 = the low itself
        else:
            # Bearish: fib from high down to low, retracement zone
            fib_0618 = s_high - s_range * (1 - 0.618)  # 0.382 level from high
            fib_0786 = s_high - s_range * (1 - 0.786)  # 0.214 level from high
            fib_1000 = s_high                            # 1.0 = the high itself

        self.structure = SMCStructure(
            direction      = direction,
            structure_high = s_high,
            structure_low  = s_low,
            fib_0618       = fib_0618,
            fib_0786       = fib_0786,
            fib_1000       = fib_1000,
        )

    # ------------------------------------------------------------------ #
    #  OTE filter                                                           #
    # ------------------------------------------------------------------ #

    def is_in_ote(self, price: float, trade_side: str) -> Tuple[bool, str]:
        """
        Check if price is within OTE zone (Fib 0.618 ~ 0.786).

        trade_side: "LONG" or "SHORT"

        For SHORT (bearish retracement into OTE):
            Structure should be bearish (direction=1) or neutral
            Price should be in upper retracement zone (near 0.618~0.786 from high)

        For LONG (bullish retracement into OTE):
            Structure should be bullish (direction=2) or neutral
            Price should be in lower retracement zone (near 0.618~0.786 from low)

        Returns (is_in_ote, reason_string)
        """
        if self.structure is None:
            return False, "No structure data yet"

        s = self.structure

        if trade_side == "SHORT":
            # bearish OTE: price pulled back up into 0.618~0.786 from high
            lo = min(s.fib_0618, s.fib_0786)
            hi = max(s.fib_0618, s.fib_0786)
            in_zone = lo <= price <= hi
            reason = (
                f"SHORT OTE [{lo:.4f} ~ {hi:.4f}] "
                f"price={price:.4f} "
                f"{'✅' if in_zone else '❌'}"
            )
            return in_zone, reason

        else:  # LONG
            # bullish OTE: price pulled back down into 0.618~0.786 from low
            lo = min(s.fib_0618, s.fib_0786)
            hi = max(s.fib_0618, s.fib_0786)
            in_zone = lo <= price <= hi
            reason = (
                f"LONG OTE [{lo:.4f} ~ {hi:.4f}] "
                f"price={price:.4f} "
                f"{'✅' if in_zone else '❌'}"
            )
            return in_zone, reason

    def get_ote_levels(self) -> Optional[dict]:
        """Return current OTE levels for display."""
        if not self.structure:
            return None
        s = self.structure
        return {
            "direction":  s.direction,
            "high":       s.structure_high,
            "low":        s.structure_low,
            "fib_0618":   s.fib_0618,
            "fib_0786":   s.fib_0786,
            "fib_1000":   s.fib_1000,
        }
