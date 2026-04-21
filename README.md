# 🤖 多幣流動性掃蕩機器人 v2

監控 10 個 USDT-M Perpetual 幣對，自動識別流動性掃蕩並真實下單。

## 監控幣對
`BTCUSDT` `ETHUSDT` `SOLUSDT` `BNBUSDT` `XRPUSDT`  
`DOGEUSDT` `ADAUSDT` `TONUSDT` `AVAXUSDT` `LINKUSDT`

---

## 快速部署

### Step 1：Push 到 GitHub

```bash
git init
git add .
git commit -m "init: liquidity sweep bot v2"
git remote add origin https://github.com/你的帳號/liquidity-sweep-bot.git
git push -u origin main
```

### Step 2：Railway 部署

1. 去 https://railway.app → New Project → Deploy from GitHub repo
2. 選你剛 push 的 repo
3. Railway 會自動偵測 `railway.toml` 和 `requirements.txt`

### Step 3：設定環境變數

Railway Dashboard → 你的專案 → Variables → Add Variable

逐一填入以下變數（複製自 `.env.example`）：

| 變數名 | 值 |
|--------|-----|
| BINANCE_API_KEY | 你的 key |
| BINANCE_SECRET_KEY | 你的 secret |
| BINANCE_TESTNET | false |
| SYMBOLS | BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,TONUSDT,AVAXUSDT,LINKUSDT |
| POSITION_SIZE_USDT | 50 |
| LEVERAGE | 20 |
| MAX_CONCURRENT_POSITIONS | 3 |
| DAILY_LOSS_LIMIT_USDT | 150 |
| MAX_HOLD_SECONDS | 300 |
| STOP_LOSS_BUFFER_PCT | 0.001 |
| TELEGRAM_BOT_TOKEN | 你的 token |
| TELEGRAM_CHAT_ID | 你的 chat id |

設定完後 Railway 會自動重新部署。

### Step 4：Binance API 白名單 IP

Railway 的 IP 是動態的，需要：
1. Railway Dashboard → 你的服務 → Settings → Public Networking → 查看 Outbound IP
2. 或者在 Binance API 設定選「不限制 IP」（較方便，但安全性較低）

建議：Binance API 設定 → 取消 IP 限制，但只開啟以下權限：
- ✅ Enable Futures
- ❌ Enable Spot & Margin（不需要）
- ❌ Enable Withdrawals（絕對不開）

---

## 倉位升級指南

本金增加後只需在 Railway Variables 改兩個數字，**不需重新上傳代碼**：

| 本金 | POSITION_SIZE_USDT | MAX_CONCURRENT_POSITIONS |
|------|--------------------|--------------------------|
| 500U | 50 | 3 |
| 1000U | 100 | 3 |
| 2000U | 150 | 4 |
| 5000U | 200 | 5 |

---

## 狀態機

```
IDLE
 └─ 靠近流動性牆 + 量 1.8x + OI 遞增
    → ABSORPTION（5分鐘觀察）
       └─ 量萎縮至 55%
          → EXHAUSTION
             └─ 量爆 10x + 價格移動 5x
                → SWEEP_WATCH
                   └─ Delta 連續遞減 80%
                      → APEX_WATCH → 進場
                         └─ 止損 / 止盈 / 超時強平
                            → IDLE
```

---

## 檔案結構

```
main.py                  # 多幣對 orchestrator
src/
  binance_client.py      # 真實下單 / 槓桿 / 餘額 / P&L
  order_book.py          # 實時訂單簿 + 局部均值牆識別
  trade_flow.py          # 狀態機 + 進出場 + 風控
  notifier.py            # Telegram 通知
railway.toml             # Railway 部署設定
requirements.txt         # Python 依賴
.env.example             # 環境變數範本
```
