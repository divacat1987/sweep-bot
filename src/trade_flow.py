import asyncio
import aiohttp
import websockets
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Deque, List, Tuple
from enum import Enum

import numpy as np

from src.binance_client import BinanceClient
from src.kline_manager import KlineManager

logger = logging.getLogger(__name__)

FEE_RATE       = 0.0005   # 0.05% taker
MIN_PROFIT_PCT = 0.0035   # 最小止盈距離 0.35%
MIN_RR         = 1.5      # 最小風報比


@dataclass
class SecondBar:
    timestamp:   float
    buy_qty:     float = 0.0
    sell_qty:    float = 0.0
    high:        float = 0.0
    low:         float = float("inf")
    open:        float = 0.0
    close:       float = 0.0
    trade_count: int   = 0

    @property
    def total_qty(self): return self.buy_qty + self.sell_qty
    @property
    def delta(self): return self.buy_qty - self.sell_qty
    @property
    def price_move(self): return abs(self.high - self.low)


class BotState(Enum):
    IDLE        = "IDLE"
    ABSORPTION  = "ABSORPTION"
    EXHAUSTION  = "EXHAUSTION"
    SWEEP_WATCH = "SWEEP_WATCH"
    APEX_WATCH  = "APEX_WATCH"
    IN_POSITION = "IN_POSITION"
    DAILY_LIMIT = "DAILY_LIMIT"


@dataclass
class Position:
    side:         str
    entry_price:  float
    stop_loss:    float
    target_price: float
    size_usdt:    float
    leverage:     int
    quantity:     float
    expected_net: float
    sl_order_id:  Optional[int] = None
    tp_order_id:  Optional[int] = None
    entry_time:   float = field(default_factory=time.time)


class TradeFlowAnalyzer:
    """
    雙重保護架構：
    1. 進場時在幣安掛 STOP_MARKET + TAKE_PROFIT_MARKET（硬保護，斷線也執行）
    2. markPrice WebSocket 每秒監控（軟監控，負責通知 + P&L 記錄）
    """

    WS_URL       = "wss://fstream.binance.com/ws"
    OI_URL       = "https://fapi.binance.com/fapi/v1/openInterest"
    MARK_WS_URL  = "wss://fstream.binance.com/ws"

    def __init__(self, symbol, config, order_book, notifier, binance, position_semaphore,
                 kline_manager: KlineManager):
        self.symbol    = symbol.upper()
        self.cfg       = config
        self.ob        = order_book
        self.notifier  = notifier
        self.binance   = binance
        self.semaphore = position_semaphore
        self.km        = kline_manager   # 5m kline + SMC structure + OTE

        self.state    = BotState.IDLE
        self.position: Optional[Position] = None

        self._current_bar = None
        self._current_sec = 0
        self.bars = deque(maxlen=600)

        self.absorption_bars    = []
        self.exhaustion_bars    = []
        self.sweep_bars         = []
        self.apex_delta_history = []
        self.active_upper_wall  = None
        self.active_lower_wall  = None
        self.sweep_direction    = None
        self._absorption_mean      = 0.0
        self._exhaustion_mean_vol  = 0.0
        self._exhaustion_mean_move = 0.0
        self.oi_history = deque(maxlen=60)

        # markPrice 監控用
        self._mark_price: float = 0.0
        self._position_closed = False  # 防止雙重平倉

    # ------------------------------------------------------------------ #
    #  Streams                                                              #
    # ------------------------------------------------------------------ #

    async def stream_trades(self):
        uri = f"{self.WS_URL}/{self.symbol.lower()}@aggTrade"
        while True:
            try:
                async with websockets.connect(uri, ping_interval=20) as ws:
                    logger.info(f"[{self.symbol}] TradeFlow WS connected")
                    async for raw in ws:
                        await self._process_trade(json.loads(raw))
            except Exception as e:
                logger.warning(f"[{self.symbol}] TradeFlow WS error: {e}, reconnecting…")
                await asyncio.sleep(3)

    async def stream_mark_price(self):
        """
        獨立 markPrice stream，每秒更新一次。
        持倉中每秒檢查止損止盈是否被幣安掛單成交。
        """
        uri = f"{self.MARK_WS_URL}/{self.symbol.lower()}@markPrice@1s"
        while True:
            try:
                async with websockets.connect(uri, ping_interval=20) as ws:
                    logger.info(f"[{self.symbol}] MarkPrice WS connected")
                    async for raw in ws:
                        data = json.loads(raw)
                        self._mark_price = float(data.get("p", 0))
                        if self.state == BotState.IN_POSITION:
                            await self._check_position_status()
            except Exception as e:
                logger.warning(f"[{self.symbol}] MarkPrice WS error: {e}, reconnecting…")
                await asyncio.sleep(3)

    async def _process_trade(self, trade):
        ts_sec = trade["T"] // 1000
        price  = float(trade["p"])
        qty    = float(trade["q"])

        if ts_sec != self._current_sec:
            if self._current_bar is not None:
                await self._on_bar_close(self._current_bar)
            self._current_sec = ts_sec
            self._current_bar = SecondBar(
                timestamp=float(ts_sec), open=price, high=price, low=price, close=price
            )

        b = self._current_bar
        b.close = price
        b.high  = max(b.high, price)
        b.low   = min(b.low, price)
        b.trade_count += 1
        if trade["m"]: b.sell_qty += qty
        else:          b.buy_qty  += qty

    async def _on_bar_close(self, bar):
        self.bars.append(bar)
        if self.state != BotState.IN_POSITION:
            await self._run_state_machine(bar)

    # ------------------------------------------------------------------ #
    #  OI                                                                   #
    # ------------------------------------------------------------------ #

    async def poll_oi(self):
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    async with session.get(self.OI_URL, params={"symbol": self.symbol}) as r:
                        self.oi_history.append(float((await r.json())["openInterest"]))
                except Exception as e:
                    logger.warning(f"[{self.symbol}] OI error: {e}")
                await asyncio.sleep(3)

    def _oi_increasing(self, lookback=5):
        if len(self.oi_history) < lookback: return False
        r = list(self.oi_history)[-lookback:]
        return r[-1] > r[0]

    # ------------------------------------------------------------------ #
    #  State machine（只在非持倉狀態運行）                                  #
    # ------------------------------------------------------------------ #

    async def _run_state_machine(self, bar):
        if self.binance.is_daily_limit_hit(self.cfg["daily_loss_limit_usdt"]):
            if self.state != BotState.DAILY_LIMIT:
                self.state = BotState.DAILY_LIMIT
                await self.notifier.send(
                    f"🛑 <b>[{self.symbol}] 每日虧損上限達到</b>\n"
                    f"今日虧損：{abs(self.binance.daily_pnl):.2f} USDT\n明日 UTC 0:00 重置"
                )
            return

        s = self.state
        if   s == BotState.IDLE:        await self._check_absorption_start(bar)
        elif s == BotState.ABSORPTION:  await self._check_absorption(bar)
        elif s == BotState.EXHAUSTION:  await self._check_exhaustion(bar)
        elif s == BotState.SWEEP_WATCH: await self._check_sweep(bar)
        elif s == BotState.APEX_WATCH:  await self._check_apex(bar)

    # ------------------------------------------------------------------ #
    #  IDLE → ABSORPTION                                                    #
    # ------------------------------------------------------------------ #

    async def _check_absorption_start(self, bar):
        upper, lower = self.ob.get_nearest_walls()
        if not upper and not lower: return
        mid = bar.close
        near_upper = upper and abs(mid - upper[0]) / mid < 0.001
        near_lower = lower and abs(mid - lower[0]) / mid < 0.001
        if not near_upper and not near_lower: return
        if len(self.bars) < 20: return
        recent_mean = np.mean([b.total_qty for b in list(self.bars)[-20:]])
        if recent_mean == 0: return
        vol_ratio = bar.total_qty / recent_mean
        if vol_ratio >= self.cfg["absorption_volume_multiplier"] and self._oi_increasing():
            self.active_upper_wall = upper
            self.active_lower_wall = lower
            self.absorption_bars   = [bar]
            self.state             = BotState.ABSORPTION
            wall_price = upper[0] if near_upper else lower[0]
            direction  = "上方阻力" if near_upper else "下方支撐"
            await self.notifier.send(
                f"🔍 <b>[{self.symbol}] 吸收期確認</b>\n"
                f"方向：{direction} @ {wall_price:.4f}\n"
                f"量倍數：{vol_ratio:.1f}x\n"
                f"時間：{_fmt_time(bar.timestamp)}"
            )

    # ------------------------------------------------------------------ #
    #  ABSORPTION                                                           #
    # ------------------------------------------------------------------ #

    async def _check_absorption(self, bar):
        self.absorption_bars.append(bar)
        mid   = bar.close
        upper = self.active_upper_wall
        lower = self.active_lower_wall
        near  = (upper and abs(mid - upper[0]) / mid < 0.002) or \
                (lower and abs(mid - lower[0]) / mid < 0.002)
        if not near:
            self._reset(); return
        if len(self.absorption_bars) >= self.cfg["absorption_window_sec"]:
            self._absorption_mean = np.mean([b.total_qty for b in self.absorption_bars])
            self.exhaustion_bars  = []
            self.state            = BotState.EXHAUSTION
            await self.notifier.send(
                f"⚡️ <b>[{self.symbol}] 耗竭期確認</b>\n"
                f"吸收均量：{self._absorption_mean:.2f}/秒\n"
                f"時間：{_fmt_time(bar.timestamp)}"
            )

    # ------------------------------------------------------------------ #
    #  EXHAUSTION                                                           #
    # ------------------------------------------------------------------ #

    async def _check_exhaustion(self, bar):
        self.exhaustion_bars.append(bar)
        threshold = self.cfg["exhaustion_volume_ratio"]
        dry_count = sum(
            1 for b in self.exhaustion_bars[-10:]
            if self._absorption_mean > 0 and b.total_qty / self._absorption_mean < threshold
        )
        if dry_count >= 5:
            self._exhaustion_mean_vol  = np.mean([b.total_qty for b in self.exhaustion_bars])
            self._exhaustion_mean_move = np.mean([b.price_move for b in self.exhaustion_bars])
            self.state = BotState.SWEEP_WATCH
        if len(self.exhaustion_bars) > self.cfg["exhaustion_window_sec"] * 5:
            self._reset()

    # ------------------------------------------------------------------ #
    #  SWEEP_WATCH                                                          #
    # ------------------------------------------------------------------ #

    async def _check_sweep(self, bar):
        pt = bar.price_move > self._exhaustion_mean_move * self.cfg["sweep_price_multiplier"]
        vt = bar.total_qty  > self._exhaustion_mean_vol  * self.cfg["sweep_volume_multiplier"]
        if pt and vt:
            upper = self.active_upper_wall
            lower = self.active_lower_wall
            if upper and bar.high >= upper[0]:  self.sweep_direction = "UP"
            elif lower and bar.low <= lower[0]: self.sweep_direction = "DOWN"
            else: self.sweep_direction = "UP" if bar.close > bar.open else "DOWN"
            self.sweep_bars         = [bar]
            self.apex_delta_history = [abs(bar.delta)]
            self.state              = BotState.APEX_WATCH
            dcn = "向上掃蕩 🔼" if self.sweep_direction == "UP" else "向下掃蕩 🔽"
            await self.notifier.send(
                f"🚨 <b>[{self.symbol}] 掃蕩觸發！</b>\n"
                f"方向：{dcn}\n突破價：{bar.close:.4f}\n"
                f"量倍數：{bar.total_qty / self._exhaustion_mean_vol:.1f}x\n"
                f"時間：{_fmt_time(bar.timestamp)}"
            )

    # ------------------------------------------------------------------ #
    #  APEX_WATCH                                                           #
    # ------------------------------------------------------------------ #

    async def _check_apex(self, bar):
        self.sweep_bars.append(bar)
        self.apex_delta_history.append(abs(bar.delta))
        req = self.cfg["apex_delta_consecutive"]
        if len(self.apex_delta_history) >= req:
            last_n = self.apex_delta_history[-req:]
            hits   = sum(1 for i in range(len(last_n) - 1) if last_n[i] > last_n[i + 1])
            if hits / (req - 1) >= self.cfg["apex_min_hit_ratio"]:
                await self._enter_position(bar)

    # ------------------------------------------------------------------ #
    #  進場 — 市價單 + 幣安掛止損止盈單                                     #
    # ------------------------------------------------------------------ #

    async def _enter_position(self, bar):
        if self.semaphore.locked():
            self._reset(); return

        cfg = self.cfg
        ep  = bar.close
        buf = cfg["stop_loss_buffer_pct"]

        if self.sweep_direction == "UP":
            side       = "SHORT"
            stop_loss  = max(b.high for b in self.sweep_bars) * (1 + buf)
            wall_tp    = self.active_lower_wall[0] if self.active_lower_wall else None
            order_side = "SELL"
            sl_side    = "BUY"
            min_tp     = ep * (1 - MIN_PROFIT_PCT)
            target     = min(wall_tp, min_tp) if wall_tp else min_tp
        else:
            side       = "LONG"
            stop_loss  = min(b.low for b in self.sweep_bars) * (1 - buf)
            wall_tp    = self.active_upper_wall[0] if self.active_upper_wall else None
            order_side = "BUY"
            sl_side    = "SELL"
            min_tp     = ep * (1 + MIN_PROFIT_PCT)
            target     = max(wall_tp, min_tp) if wall_tp else min_tp

        sl_dist = abs(ep - stop_loss)
        tp_dist = abs(ep - target)

        if sl_dist == 0:
            self._reset(); return

        rr = tp_dist / sl_dist
        if rr < MIN_RR:
            await self.notifier.send(
                f"⚠️ <b>[{self.symbol}] 跳過進場</b>\n"
                f"風報比不足：1:{rr:.2f}（最低 1:{MIN_RR}）"
            )
            self._reset(); return

        notional     = cfg["position_size_usdt"] * cfg["leverage"]
        fee_total    = notional * FEE_RATE * 2
        gross_profit = tp_dist / ep * notional
        expected_net = gross_profit - fee_total

        if expected_net < 1.5:
            await self.notifier.send(
                f"⚠️ <b>[{self.symbol}] 跳過進場</b>\n"
                f"預期淨利不足：{expected_net:.2f} USDT（最低 1.5 USDT）"
            )
            self._reset(); return

        # ── OTE 過濾：進場價必須在 5m 結構的 Fib 0.618~0.786 區間內 ──
        in_ote, ote_reason = self.km.is_in_ote(ep, side)
        if not in_ote:
            ote_levels = self.km.get_ote_levels()
            ote_info = ""
            if ote_levels:
                dir_cn = "多頭" if ote_levels["direction"] == 2 else "空頭" if ote_levels["direction"] == 1 else "未定義"
                ote_info = (
                    f"結構方向：{dir_cn}\n"
                    f"結構高：{ote_levels['high']:.4f}\n"
                    f"結構低：{ote_levels['low']:.4f}\n"
                    f"OTE 區間：{min(ote_levels['fib_0618'], ote_levels['fib_0786']):.4f}"
                    f" ~ {max(ote_levels['fib_0618'], ote_levels['fib_0786']):.4f}"
                )
            await self.notifier.send(
                f"⚠️ <b>[{self.symbol}] 跳過進場（不在 OTE）</b>\n"
                f"進場價：{ep:.4f}\n"
                f"{ote_reason}\n"
                f"{ote_info}"
            )
            logger.info(f"[{self.symbol}] OTE filter: {ote_reason}")
            self._reset(); return

        await self.semaphore.acquire()
        try:
            await self.binance.set_margin_type(self.symbol, "ISOLATED")
            await self.binance.set_leverage(self.symbol, cfg["leverage"])

            qty = await self.binance.calc_quantity(
                self.symbol, cfg["position_size_usdt"], ep, cfg["leverage"]
            )
            if not qty:
                self.semaphore.release(); self._reset(); return

            # 1. 進場市價單
            order = await self.binance.place_market_order(self.symbol, order_side, qty)
            if not order:
                self.semaphore.release(); self._reset(); return

            # 2. 幣安掛止損單（硬保護）
            sl_order = await self.binance.place_stop_market_order(
                self.symbol, sl_side, stop_loss, qty, close_position=True
            )

            # 3. 幣安掛止盈單（硬保護）
            tp_order = await self.binance.place_take_profit_market_order(
                self.symbol, sl_side, target, close_position=True
            )

            self._position_closed = False
            self.position = Position(
                side=side, entry_price=ep, stop_loss=stop_loss,
                target_price=target, size_usdt=cfg["position_size_usdt"],
                leverage=cfg["leverage"], quantity=qty,
                expected_net=expected_net,
                sl_order_id=sl_order.get("orderId") if sl_order else None,
                tp_order_id=tp_order.get("orderId") if tp_order else None,
            )
            self.state = BotState.IN_POSITION

            side_cn = "做空 🔴" if side == "SHORT" else "做多 🟢"
            ote_levels = self.km.get_ote_levels()
            ote_str = ""
            if ote_levels:
                ote_str = (
                    f"OTE 區間：{min(ote_levels['fib_0618'], ote_levels['fib_0786']):.4f}"
                    f" ~ {max(ote_levels['fib_0618'], ote_levels['fib_0786']):.4f}\n"
                )
            await self.notifier.send(
                f"✅ <b>[{self.symbol}] 進場</b>\n"
                f"方向：{side_cn}\n"
                f"進場價：{ep:.4f}\n"
                f"止損：{stop_loss:.4f}（{sl_dist/ep*100:.3f}%）\n"
                f"目標：{target:.4f}（{tp_dist/ep*100:.3f}%）\n"
                f"風報比：1:{rr:.2f}\n"
                f"預期淨利：+{expected_net:.2f} USDT\n"
                f"{ote_str}"
                f"數量：{qty} ({cfg['position_size_usdt']} USDT × {cfg['leverage']}x)\n"
                f"⚠️ 幣安止損/止盈單已掛\n"
                f"時間：{_fmt_time(bar.timestamp)}"
            )
            logger.info(
                f"[{self.symbol}] IN_POSITION side={side} entry={ep:.4f} "
                f"sl={stop_loss:.4f} tp={target:.4f} rr={rr:.2f}"
            )

        except Exception as e:
            logger.error(f"[{self.symbol}] Entry exception: {e}")
            self.semaphore.release(); self._reset()

    # ------------------------------------------------------------------ #
    #  持倉監控 — markPrice WebSocket 每秒觸發                              #
    #  幣安掛單是硬保護，這裡是軟監控：                                      #
    #  - 查幣安實際倉位，若已被掛單成交則記錄 P&L 並通知                    #
    #  - 幣安掛單成交後記錄 P&L 並通知                                      #
    # ------------------------------------------------------------------ #

    async def _check_position_status(self):
        if self._position_closed:
            return

        pos      = self.position
        price    = self._mark_price
        hold_sec = int(time.time() - pos.entry_time)

        if pos.side == "SHORT":
            pnl_pct = (pos.entry_price - price) / pos.entry_price
            hit_sl  = price >= pos.stop_loss
            hit_tp  = price <= pos.target_price
        else:
            pnl_pct = (price - pos.entry_price) / pos.entry_price
            hit_sl  = price <= pos.stop_loss
            hit_tp  = price >= pos.target_price

        notional  = pos.size_usdt * pos.leverage
        fee_total = notional * FEE_RATE * 2
        net_pnl   = pnl_pct * notional - fee_total

        # 軟監控：價格觸及止損/止盈 → 確認幣安掛單已成交 → 記錄 P&L 並通知
        if hit_sl or hit_tp:
            actual_qty = await self.binance._get_actual_position_qty(self.symbol)
            if actual_qty == 0:
                reason = "❌ 止損" if hit_sl else "🏁 止盈"
                await self._record_close(pos, price, net_pnl, hold_sec, reason)
            else:
                logger.debug(f"[{self.symbol}] Waiting for exchange order to fill…")

    async def _record_close(self, pos, price, net_pnl, hold_sec, reason):
        """幣安掛單已成交，只記錄 P&L 和通知"""
        if self._position_closed:
            return
        self._position_closed = True

        self.binance.record_pnl(net_pnl)
        sign = "+" if net_pnl >= 0 else ""
        await self.notifier.send(
            f"{reason} <b>[{self.symbol}]</b>\n"
            f"出場價：{price:.4f}\n"
            f"淨盈虧：{sign}{net_pnl:.2f} USDT\n"
            f"今日累計：{self.binance.daily_pnl:+.2f} USDT\n"
            f"持倉：{hold_sec}秒"
        )
        logger.info(f"[{self.symbol}] {reason} @ {price:.4f} net={net_pnl:.2f}")
        self.semaphore.release()
        self._reset()

    # ------------------------------------------------------------------ #
    #  Reset                                                                #
    # ------------------------------------------------------------------ #

    def _reset(self):
        self.state                 = BotState.IDLE
        self.position              = None
        self._position_closed      = False
        self.absorption_bars       = []
        self.exhaustion_bars       = []
        self.sweep_bars            = []
        self.apex_delta_history    = []
        self.active_upper_wall     = None
        self.active_lower_wall     = None
        self.sweep_direction       = None
        self._absorption_mean      = 0.0
        self._exhaustion_mean_vol  = 0.0
        self._exhaustion_mean_move = 0.0


def _fmt_time(ts):
    import datetime
    return datetime.datetime.utcfromtimestamp(ts).strftime("%H:%M:%S UTC")
