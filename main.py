import asyncio
import logging
import os
from dotenv import load_dotenv

from src.notifier import TelegramNotifier
from src.order_book import OrderBookManager
from src.trade_flow import TradeFlowAnalyzer
from src.binance_client import BinanceClient
from src.kline_manager import KlineManager

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def load_config() -> dict:
    return {
        "symbols":                      os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,TONUSDT,AVAXUSDT,LINKUSDT").split(","),
        "position_size_usdt":           float(os.getenv("POSITION_SIZE_USDT", "50")),
        "leverage":                     int(os.getenv("LEVERAGE", "20")),
        "max_concurrent_positions":     int(os.getenv("MAX_CONCURRENT_POSITIONS", "3")),
        "stop_loss_buffer_pct":         float(os.getenv("STOP_LOSS_BUFFER_PCT", "0.001")),
        "liquidity_scan_pct":           float(os.getenv("LIQUIDITY_SCAN_PCT", "0.02")),
        "liquidity_layers":             int(os.getenv("LIQUIDITY_LAYERS", "3")),
        "wall_local_ratio":             float(os.getenv("WALL_LOCAL_RATIO", "4.0")),
        "absorption_window_sec":        int(os.getenv("ABSORPTION_WINDOW_SEC", "300")),
        "exhaustion_window_sec":        int(os.getenv("EXHAUSTION_WINDOW_SEC", "60")),
        "absorption_volume_multiplier": float(os.getenv("ABSORPTION_VOLUME_MULTIPLIER", "1.8")),
        "exhaustion_volume_ratio":      float(os.getenv("EXHAUSTION_VOLUME_RATIO", "0.55")),
        "sweep_price_multiplier":       float(os.getenv("SWEEP_PRICE_MULTIPLIER", "5.0")),
        "sweep_volume_multiplier":      float(os.getenv("SWEEP_VOLUME_MULTIPLIER", "10.0")),
        "apex_delta_consecutive":       int(os.getenv("APEX_DELTA_CONSECUTIVE", "5")),
        "apex_min_hit_ratio":           float(os.getenv("APEX_MIN_HIT_RATIO", "0.8")),
        "daily_loss_limit_usdt":        float(os.getenv("DAILY_LOSS_LIMIT_USDT", "150")),
    }


async def _wall_scanner_loop(ob, symbol, interval=10):
    while True:
        upper, lower = await ob.scan_walls()
        logger.debug(
            f"[{symbol}] Walls — Upper: {[f'{p:.4f}({q:.1f})' for p,q in upper]} | "
            f"Lower: {[f'{p:.4f}({q:.1f})' for p,q in lower]}"
        )
        await asyncio.sleep(interval)


async def run_symbol(symbol, cfg, notifier, binance, position_semaphore):
    logger.info(f"[{symbol}] Starting pipeline")

    ob = OrderBookManager(
        symbol=symbol,
        scan_pct=cfg["liquidity_scan_pct"],
        layers=cfg["liquidity_layers"],
        wall_local_ratio=cfg["wall_local_ratio"],
    )

    km = KlineManager(symbol=symbol)

    analyzer = TradeFlowAnalyzer(
        symbol=symbol,
        config=cfg,
        order_book=ob,
        notifier=notifier,
        binance=binance,
        position_semaphore=position_semaphore,
        kline_manager=km,
    )

    await ob.fetch_snapshot()
    await ob.scan_walls()
    await km.fetch_history()   # 載入歷史 5m K 線 + 計算初始結構

    await asyncio.gather(
        ob.stream(),
        analyzer.stream_trades(),
        analyzer.stream_mark_price(),
        analyzer.poll_oi(),
        km.stream(),                          # 5m K 線 WebSocket
        _wall_scanner_loop(ob, symbol, interval=10),
    )


async def main():
    cfg    = load_config()
    symbols = cfg["symbols"]

    notifier = TelegramNotifier(
        token=os.getenv("TELEGRAM_BOT_TOKEN"),
        chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    )

    binance = BinanceClient(
        api_key=os.getenv("BINANCE_API_KEY"),
        secret_key=os.getenv("BINANCE_SECRET_KEY"),
        testnet=os.getenv("BINANCE_TESTNET", "false").lower() == "true",
    )

    position_semaphore = asyncio.Semaphore(cfg["max_concurrent_positions"])

    await notifier.send(
        f"🤖 <b>多幣流動性掃蕩機器人啟動 v3</b>\n"
        f"監控幣對：{', '.join(symbols)}\n"
        f"單倉大小：{cfg['position_size_usdt']} USDT × {cfg['leverage']}x\n"
        f"最大同時倉位：{cfg['max_concurrent_positions']}\n"
        f"每日最大虧損：{cfg['daily_loss_limit_usdt']} USDT\n"
        f"雙重止損保護：幣安掛單 + markPrice 監控 ✅"
    )

    tasks = [
        asyncio.create_task(
            run_symbol(symbol.strip().upper(), cfg, notifier, binance, position_semaphore)
        )
        for symbol in symbols
    ]

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
