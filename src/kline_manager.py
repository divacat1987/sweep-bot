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
    Computes SMC Current Structure using STRUCT_LOOKBACK=12 bars (= 1 hour).
    Fib levels match Pine Script exactly:
      Fibo1 = 0.786, Fibo3 = 0.618
      OTE zone = 0.618 ~ 0.786

    Pine Script formula:
      direction=1 (bearish): fib = high - (range - range * fiboValue)
      direction=2 (bullish): fib = low  + (range - range * fiboValue)

    For BEARISH structure (direction=1):
        fib_0786 = high - range*(1-0.786)  ← upper OTE boundary
        fib_0618 = high - range*(1-0.618)  ← lower OTE boundary
        SHORT entry is in OTE if: fib_0618 <= price <= fib_0786

    For BULLISH structure (direction=2):
        fib_0786 = low + range*(1-0.786)   ← lower OTE boundary
        fib_0618 = low + range*(1-0.618)   ← upper OTE boundary
        LONG entry is in OTE if: fib_0786 <= price <= fib_0618
    """

    REST_URL = "https://fapi.binance.com/fapi/v1/klines"
    WS_URL   = "wss://fstream.binance.com/ws"

    INTERVAL        = "5m"
    LOOKBACK        = 100   # bars to maintain
    STRUCT_LOOKBACK = 12    # 12 根 5m K = 1 小時結構

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

        # Pine Script 公式：
        # 空頭(direction=1): fib = high - (range - range * fiboValue)
        # 多頭(direction=2): fib = low  + (range - range * fiboValue)
        # Fibo1 = 0.786, Fibo3 = 0.618
        # OTE 區間 = 0.618 ~ 0.786

        FIBO_786 = 0.786
        FIBO_618 = 0.618

        if direction == 2:
            # 多頭結構：從低點往上拉伸，回撤到 0.618~0.786 是 OTE
            fib_0786 = s_low + (s_range - s_range * FIBO_786)   # 較低
            fib_0618 = s_low + (s_range - s_range * FIBO_618)   # 較高
            fib_1000 = s_low                                      # 1.0 = 低點
        else:
            # 空頭結構：從高點往下拉伸，反彈到 0.618~0.786 是 OTE
            fib_0786 = s_high - (s_range - s_range * FIBO_786)  # 較高
            fib_0618 = s_high - (s_range - s_range * FIBO_618)  # 較低
            fib_1000 = s_high                                     # 1.0 = 高點

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
    #  OTE filter                                                           #
    # ------------------------------------------------------------------ #

    def is_in_ote(self, price: float, trade_side: str,
                  zone: str = "ote") -> Tuple[bool, str]:
        """
        zone = "ote"   → 0.618 ~ 0.786（耗竭期用，嚴格）
        zone = "sweep" → 0.618 ~ 1.0  （掃蕩期用，寬鬆）
        trade_side: "LONG" or "SHORT"
        """
        if self.structure is None:
            return False, "No structure data yet"

        s = self.structure

        if zone == "sweep":
            # 寬鬆區間：0.618 ~ 1.0（Fib 1.0 = 結構高/低點）
            if trade_side == "SHORT":
                lo = min(s.fib_0618, s.fib_1000)
                hi = max(s.fib_0618, s.fib_1000)
            else:
                lo = min(s.fib_0618, s.fib_1000)
                hi = max(s.fib_0618, s.fib_1000)
            in_zone = lo <= price <= hi
            label   = f"SWEEP [{lo:.4f}~{hi:.4f}] price={price:.4f} {'✅' if in_zone else '❌'}"
            return in_zone, label
        else:
            # 嚴格 OTE：0.618 ~ 0.786
            lo = min(s.fib_0618, s.fib_0786)
            hi = max(s.fib_0618, s.fib_0786)
            in_zone = lo <= price <= hi
            label   = f"OTE [{lo:.4f}~{hi:.4f}] price={price:.4f} {'✅' if in_zone else '❌'}"
            return in_zone, label

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
