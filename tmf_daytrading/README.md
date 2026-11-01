# TMF 微型臺指期 當沖量化交易系統 (ORB Pro V2)

專為臺灣期貨交易所 **微型臺指期 (TMF)** 設計的日內當沖量化交易與回測系統，支援永豐金證券 **Shioaji API** 模擬與正式實盤自動下單。

---

## 一、核心策略特色 (ORB Pro V2 翻修獲利版)

1. **現貨開盤區間突破 (09:00 ~ 09:15 Spot-Open ORB)**：
   - 避開 08:45~09:00 期貨單獨開盤、09:00 台股現貨開盤瞬間的洗盤假突破，以 **09:00 ~ 09:15（現貨開盤首 15 分鐘）** 的最高/最低價為當日多空分水嶺。
2. **法人雙重順勢濾網 (VWAP + EMA9/EMA21)**：
   - **做多條件**：向上突破 ORB 上界 + `收盤價 > VWAP（日內均量成本線）` + `EMA9 > EMA21`。
   - **做空條件**：向下跌破 ORB 下界 + `收盤價 < VWAP` + `EMA9 < EMA21`。
3. **三階梯動態保本鎖利與波段抱單 (3-Tier Profit Lock)**：
   - **初始停損**：90 點（適應台指近 5 萬點高基期波動）。
   - **一階保本鎖利**：浮動獲利達 **+80 點** 時，停損移至 **進場價 +30 點**（確保獲利單絕不變虧損單）。
   - **二階波段鎖利**：浮動獲利達 **+200 點** 時，停損上移至 **進場價 +120 點**。
   - **三階大波段移動停利**：浮動獲利達 **+400 點** 啟動移動停利（回撤 60 點平倉），否則抱至 **13:25 收盤前安全平倉**。
4. **早盤假突破反手機制 (Stop & Reverse, SAR)**：
   - 若 11:00 前首筆突破單觸發 90 點停損，且收盤價已反向越過 VWAP（確認早盤為假突破），立即反手進場順應真趨勢（每日最多反手 1 次）。
5. **13:25 收盤前雙重安全平倉保護**：
   - 提前於 **13:25:00** 平倉（搭配 13:26:00 系統時鐘強制保底），避開每月第三個週三結算日 13:30 停止撮合、台股現貨尾盤 5 分鐘集合競價甩尾與券商 13:30 當沖代沖銷撞單風險。

---

## 二、20 個交易日回測績效 (2026-09-30 ~ 2026-10-30)

- **獲利天數 / 虧損天數 / 觀望**：**12 勝 / 6 負 / 2 觀望（有交易日勝率 66.7%）**
- **單筆交易勝率 (Win Rate)**：**66.7%（21 筆中 14 勝 7 負）**
- **累積總獲利點數**：**+887 點**
- **扣除成本總淨損益 (含手續費、期交稅、滑價)**：**+NT$ 7,743 元 / 口**
- **獲利因子 (Profit Factor)**：**2.16**
- **最大區間回撤 (MDD)**：**NT$ 1,907 元**

![20日回測損益曲線](tmf_backtest_20days.png)

---

## 三、專案目錄結構

```text
tmf_daytrading/
├── .env.example                # 永豐金 Shioaji API 金鑰設定範本
├── config.py                   # 商品規格、API 設定與 ORB Pro 策略參數
├── main.py                     # 系統主入口 (支援 backtest / paper / live 模式)
├── run_backtest.py             # 單一交易日回測腳本
├── run_10days_backtest.py      # 前 10 個交易日回測腳本
├── run_20days_backtest.py      # 前 20 個交易日回測腳本
├── strategies/                 # 策略邏輯庫
│   ├── orb_strategy.py         # ORB Pro V2 主力策略 (含 VWAP/EMA、三階鎖利、SAR)
│   └── dual_ema_strategy.py    # 雙均線 + VWAP 順勢策略模板
├── backtester/                 # 回測核心引擎
│   ├── engine.py               # 1分K合成與逐根回測引擎
│   └── metrics.py              # 績效統計器 (含手續費、稅、滑價與 MDD 計算)
├── live/                       # 實盤/模擬下單模組
│   └── shioaji_trader.py       # 永豐 Shioaji 自動看盤、下單與 13:25 安全平倉機器人
└── data/
    └── tmf_20days_1m_kbars.csv # 20 個交易日 1分K 輕量快取檔 (克隆後可立即秒級回測)
```

---

## 四、快速安裝與使用指南

### 1. 安裝套件與設定金鑰
```bash
pip install -r requirements.txt
cp .env.example .env
# 編輯 .env 填入您的 SHIOAJI_API_KEY 與 SHIOAJI_SECRET_KEY
```

### 2. 執行歷史回測 (無需 API Key)
```bash
# 執行前 20 個交易日回測
python3 run_20days_backtest.py

# 執行前 10 個交易日回測
python3 run_10days_backtest.py
```

### 3. 開盤日啟動自動交易機器人 (建議 08:40 ~ 08:45 啟動)
```bash
# 模式 A：永豐 Shioaji 模擬環境試跑 (simulation=True，不下真單)
python3 main.py --mode paper --contract 202611 --qty 1

# 模式 B：永豐 Shioaji 正式實盤自動下單 (simulation=False，需設定 .env 內憑證路徑)
python3 main.py --mode live --contract 202611 --qty 1
```
