# RaTrade ALGO — Poori Website Hindi Document (Dobara Banane Ki Guide)

Website: https://ratrade-iwi2.onrender.com
Tech: **FastAPI (Python) + Sabkuch ek hi `static/index.html` me HTML/JS/CSS (Bootstrap 5 style UI)**

Yeh document uske liye hai ki bina phir se confusion ke, website ko shuru se (ya kisi aur server par) dobara banaya ja sake — kaunsi library kyu, data kahan se aata hai, har button ka kya use hai.

---

## 1. KUPACHI LIBRARIES (requirements.txt) AUR UNKA KAM

| Library | Kyu use hui |
|---|---|
| **fastapi** | Poora backend (REST API server) isi par bana hai. Har route `/api/...` FastAPI router hai. |
| **uvicorn** | FastAPI ko chalane wala server. Render par: `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| **pandas** + **numpy** | Candle/OHLC data, indicators (Supertrend, MACD, Bollinger) aur backtest maths ke liye. |
| **requests** + **httpx** | NSE/Dhan/broker ke HTTP calls. |
| **websockets** | Dhan ka live tick feed (WebSocket) LTP ke liye. |
| **reportlab** + **matplotlib** | Backtest ka PDF report download. |
| **backtrader, backtesting, vectorbt, openchart** | Alag-alag backtest engines (vectorbt vectorised runs ke liye). |
| **dhanhq** | Dhan broker SDK — auth, quote, order. |
| **pyotp** | Angel One (SmartAPI) ka TOTP 2FA generate karne ke liye. |
| **nsefin + nsepython** | NSE bhavcopy / official data download (daily F&O data DB me save hoti hai). |
| **yfinance** | Backup spot price (Stooq/Google chain ke saath). |
| **psycopg2-binary** | Agar Postgres (Render/Supabase free) use ho to DB `DATABASE_URL` par chal jaye; warna local `data/ratrade.db` (SQLite). |
| **python-multipart** | Form uploads ke liye FastAPI requirement. |

Frontend me koi build-step nahi — sirf **Bootstrap 5 (CDN)** style + vanilla JavaScript. Isliye deploy bilkul fast hai.

---

## 2. MAIN FILES KA STRUCTURE

```
main.py                    → app, CORS, gzip, saare routers, background workers
core/
  models/database.py       → SQLite/Postgres singleton (users, strategies, paper_trades, settings…)
  services/
    live_market_data.py    → spot price + option LTP (cache TTL 300s)
    free_data.py           → free price chain: NSE → Stooq → Google (yfinance)
    dhan_feed.py           → Dhan WebSocket live tick (LTP float32 packets parse)
    egress_proxy.py        → fixed-IP proxy (Dhan IP whitelist ke liye)
    contract_pricer.py     → option premium: broker quote → DB bhavcopy → Black-Scholes model
    backtest_engine.py     → ENGINE_VERSION="v3" — signals, strikes, SL/TP, returns
    indicator_engine.py    → Supertrend(10,3), MACD(12/26/9), Bollinger, RSI etc.
    auto_paper.py          → robot: active strategies par apne-aap paper trade lagana
    sl_monitor.py          → har 30s SL/TP check + market hours
routes/
  strategies.py    /api/strategies   → save/list/lots ±, live-toggle per strategy, backtest, history
  paper_trade.py   /api/trades      → paper place/close/SL-TP edit/history
  option_chain.py  /api/chain       → expiries, chain, live chain, place-trade
  broker.py        /api/broker      → Dhan/Fyers/Angel/Shoonya connect + LIVE place
  webhook.py       /api/webhook     → TradingView/email signals ingest
  email_alerts.py  /api/email-alerts
  dashboard.py, scanner.py, tvchart.py, websocket.py, keepalive.py, custom_code.py
static/index.html           → poora UI (3600+ lines, single page app)
data/ratrade.db             → DB (Render free par ephemeral — Postgres set karo)
```

---

## 3. LIVE PRICE KA DATA KAHA SE AATA HAI

1. **Dhan WebSocket** (`dhan_feed.py`) — `wss://api-feed.dhan.co` par subscribe karke binary packets se LTP (tick) aata hai. Re-connection backoff + 30s ping. Ye sabse accurate + sabse fast hai (token chahiye Connect page se).
2. **Free fallback chain** (`free_data.py`) — NSE → Stooq → Google price. In raste se spot nikaal ke 5 min (TTL 300s) cache. NSE cloud par IP block hota hai isliye chain kaam aati hai.
3. **Bhavcopy DB** (`bhavcopy_data` table) — roz NSE se F&O bhavcopy download hoke DB me save hoti hai (background worker `main.py` startup me). Market band hone par last close dikhta hai.
4. **Option premium (LTP)** — pehle Dhan token-quote (live), fail hone par DB me aaj ki bhavcopy, tab bhi fail ho to **Black-Scholes model** ka andaze (spot + IV + dte se premium calculate).

**Dhan "DH-905 Invalid IP"** aaye to **Brokers page → Egress Proxy** set karo (fixed-IP proxy ke through sab Dhan calls jaati hain — warna Dhan sirf registered IP par chalta hai).

---

## 4. OPTION CHAIN KA DATA KAISE AATA HAI

- `GET /api/chain/live/{symbol}` — iski priority:
  1. **Sirf local server par** (cloud nahi) NSE ka real chain direct.
  2. Cloud par: **aaj ki bhavcopy DB** ki fresh chain.
  3. Wo fail ho to **model chain**: spot - strike step nikaal ke ATM/ITM/OTM strikes list, har strike par premium Black-Scholes se + Dhan token quotes available ho to real LTP overlay.
- Expiry: `GET /api/chain/expiries/...` — Weekly/Monthly list, requested expiry DB se validate hoti hai.
- Strike step: NIFTY 50, BANKNIFTY 100 etc. (helpers `strike_step`).
- Premium sanity check: option premium spot se zyada/0-1 rupee ki jesi lottery-ticket ho to trade place block hoti hai.

---

## 5. BACKTEST ENGINE KAISE CHALTA HAI (v3)

- History data: bhavcopy DB (30+ din ka daily darta).
- Indicators pehle pura serei par calculate hote hain (`indicator_engine.py`), phir per-bar signal: jo se Supertrend diya + MACD line × signal line cross + user ke conditions — `_get_buy_signal`/`_get_sell_signal`.
- Trades strike se bante hain: **ATM/OTM1-6/ITM1-6**, weekly/monthly expiry chunna, premium bhavcopy ya Black-Scholes model se.
- Har trade par **SL/TP check** (`_check_sl_tp`) aur expiry par auto square-off (intraday ho to wahi din, positional ho to liya end date).
- Output: total trades, win %, net PnL, max drawdown, Sharpe — UI me "Saved BT" card par.
- **"purana engine" badge** — bottle se save hua result jin ka engine version `v3` alag hai, usay stale (unsure) mark kiya jata hai.
- **Monte Carlo (MC)**, **Walk-Forward (WF)**, **OPT (optimize)** — backtest result ki janch: future/multiple windows/kai-optimize me result milegi nahi.

---

## 6. PAPER TRADING + AUTO ROBOT (Signal Background)

- Har strategy SAVE hote hi ke apne-aap **paper trade** lag jaate hain (agar bazaar opened hai) — `auto_paper.py`.
- Background worker har 30 min (`run_once()`) khud check karega jo sabse naya signal (Supertrend/MACD re-run karke) lagaye — signal milega to trade `paper_trades` table me andaza, entry/exit, SL/TP, lot_size ke sath.
- `sl_monitor.py` har 30s: sabki open paper trades ki **SL hit / TP hit** chalta hai.
- Market Open check IST 9:15–15:30 (Mon–Fri) — band hone par "kisi naya din par lag jayega".

---

## 7. LIVE TRADING (REAL PAISA) SYSTEM

- **Global LIVE kill-switch** (Brokers page "Enable LIVE"). OFF par koi real order nahi dega.
- **Per-strategy Live ON/OFF** (Strategy cards par naya button) — settings table me `strat_live_enabled_{id}`. OFF hui to usi strategy ka live order block ho jayega (backend `broker.py` aur frontend dono se).
- **`POST /api/broker/place-live`** — safety flow pehle:
  1. **dry_run=true** — koi order nahi bheja, sirf preview (broker, side, qty, andaza cost) aata hai Premiun/lot/margin/market files etc.
  2. Confirm me complete preview me user与会taraf dikhata hai.
  3. Fir `dry_run=false` — real MARKET order broker SDK (FyersV3/DhanLive/AngelLive/ShoonyaLive) se.
- Broker ki credentials `settings` table me `broker_dhan` etc. font me save hote hain; env vars `RATRADE_DHAN_CLIENT_ID / RATRADE_DHAN_ACCESS_TOKEN` se seed hote hain.
- Access token roz expire — **Refresh Tokens** button ya `main.py` tokencens keeper (6h worker) ne auto renew karta hai.
- Order me lot calculation: `lots × official_lot_size(symbol)` — `/api/lot-size/{symbol}` fail-closed gate hai.
- **Trade cutoff:** 3:00 PM se bad real entry nahi.

---

## 8. WEBSITE KHULI RAHE / SLEEP NA HO (Uptime Setup)

Render par free tier **15 min idle hone par service sleep** ho jati hai. Iske 3 bache hote hain:

1. **Self-ping worker** (`routes/keepalive.py` + `main.py`): app khud apne public URL par har **9 minute** `/api/keepalive/ping` marta hai (env `SELF_URL`/`RENDER_EXTERNAL_URL`).
2. **GitHub Actions** `.github/workflows/keep-alive.yml` — cron se repo se ping jari rakhna.
3. **UptimeRobot (external)** — `https://ratrade-iwi2.onrender.com/health` ya `/api/keepalive/ping` URL par **5 min** interval ka HTTP monitor set karo. `/health` GET aur HEAD dono ka jawab deta hai 200 OK. UptimeRobot free 50 monitors deta hai — yehi standby guard hai.
4. `/api/keepalive/info` endpoint me setup guide CLI me dikhati hai (cron-job.org me URL paste karne hota hai agar UptimeRobot se auratan bhi).

---

## 9. EK-EK BUTTON KA PDF(TAFSEEL) ME KAAM

### Navigation (upar wali Path row)
- **Dashboard** → `go('dashboard')` — portfolio + market ke rewards + sab open trades.
- **Trading Center** → saved strategy cards + trades table.
- **Option Chain** → live/chain.
- **Strategy Backtest** → backtest maker (leg builders) + Saved Strategies cards.
- **TV Chart** → TradingView-style chart custom data `/api/tvchart/data` se.
- **Brokers** → Dhan/Fyers/Angel/Shoonya connect + LIVE toggle + Webhook token.
- **Email Alerts** → email signals inbox/config.

### Dashboard page
- **Regime/Sentiment dropdown** — market mode chunna jo UI indicators me dikhata hai.
- **NSE F&O Top-5** — bullish/bearish top 5 symbol list (refresh auto).
- **Refresh (Opportunities)** — scanner ke latest opportunities fatch (`/api/scanner/...`).
- **Trade history row buttons**: `Chart` (TV chart opening ke sath us symbol), `SL/TP` (edit lb), `Paper` (paper me place), `Live` (place-live preview → confirm), `Delete`, `Exit` (close trade).
- **Open Positions me inline TP / SL inputs** — number badlo to `updateTP/updateSL` turant DB me save.

### Trading Center
- **Run Simulation** — sab saved strategies ek saath backtest.
- **Purge Symbol** — ek symbol ka poora data (strategies + trades + results) PERMANENT delete (confirm popup).
- **Slippage %** — backtest me shield/spread cost eseme realisticout.
- **Live/Paper/BT toggle** (LIVE / PAPER / BT) — kaunsa mode show karna hai.
- **Saved Strategies cards**: `Paper Trade` (paper order), `Live Trade` (dry-run preview → confirm → broker order), **Live ON/OFF** (per-strategy toggle green=ON), **Lots −/+** (lot size 1–200, qty hint "1 lot = 75" etc.), `Auto ON/OFF` (auto paper worker usi strategy ke liye), `Backtest Detail`, `Paper Detail`, `Delete`.
- **Open Trades / Closed Trades / Total P&L / Win Rate** — upar stats cards live count ke sath.

### Option Chain
- **Symbol + Expiry select, Load** (`cLoadChain`) — chain table; har strike par **BUY/SELL buttons** (CE/PE) jo paper trade open karte hain (strike auto-filled).
- Fresher LTP live-tick se, dim spot bhavcopy model se.

### Strategy Backtest
- **Templates**: Long Straddle, Short Straddle, Strangle, Bull/Bear Call/Put Spread, Iron Condor, Butterfly, Calendar — pre-filled legs.
- **Leg builder**: Option type CE/PE, BUY/SELL, strike selection ATM/OTM1..6/ITM1..6, weekly/monthly, Lots number.
- **Indicators checkboxes** (Supertrend, MACD, RSI, Bollinger…) + risk panel SL/TP/max trades.
- **Run Backtest** → result metrics + trade list.
- **Start/Save Strategy** → DB me strategy (id de, apne aap paper trade lagti hai).
- **Paper Trade / Live Trade (form)** — usi formed config se pahli leg place.
- **MC / WF / OPT** advanced checks, **PDF** report download (reportlab).
- **1M/3M/6M/1Y** date presets.
- Saved cards par (upar Trading Center wale) + **History** (purane saved results view/restore — max 10) aur **Paper Detail**.

### TV Chart
- **Load Chart** — `/api/tvchart/data` bhavcopy candles dilata hai; symbol/expiry dropdown.

### Brokers
- **Enable LIVE / Disable (live-toggle-btn)** — global kill-switch (on value `live_trading_enabled` setting). Connected real broker ho tabhi OFF hota hai (order safety).
- **Auto-Connect All / Refresh Tokens** — batch connect/renew.
- **Per-broker cards** (Dhan/Angel): `Setup` (credentials modal), `Connect`, `OAuth Login` (Dhan auth URL redirect), `Account` (funds/margin check).
- **Egress Proxy Save + Quick Test** — fixed-IP proxy (Dhan IP whitelist).
- **Webhook section**: **Copy Ingest URL**, token save (`saveWhToken`), **Test Webhook** — TradingView/email alerts yahan POST aur paper trade lag jati hai (token guard `x-email-token`).

### Email Alerts
- **Copy URL** — email parsing webhook URL.
- **Save Config** — kis email/signal se trade lagana etc.
- **Alerts list** — aaye signals + unke trades.

---

## 10. DOBARA BUILD/DEPLOY STEPS (CHECKLIST)

1. Python 3.11 + `pip install -r requirements.txt`
2. `uvicorn main:app --reload` (local) — UI: `http://localhost:8000`
3. DB pehla run `data/ratrade.db` me auto tables ban jaati hai (Postgres chahiye to `DATABASE_URL` env).
4. Render: GitHub repo connect karo, **Python** runtime, build `pip install -r requirements.txt`, start `uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1`, health check `/health` — deploy push par (**autoDeploy true**).
5. Broker token Connect page se, (Dhan IP issue → Egress Proxy), token expire daily — Refresh Tokens.
6. UptimeRobot 5-min monitor `/health` + self-ping worker already on.
7. UI changes sirf `static/index.html` me — koi build step nahi, browser cache me Ctrl+F5 (UI chip (UI <commit>) me badge dikhta hai current vya saval).

---
*Document kiya gaya: Oct 2026, current code commit `dfd729e` ke mutabik.*
