import math
import time
import datetime
from typing import List, Dict
from core.services.indicator_engine import IndicatorEngine
from core.services.transaction_costs import TransactionCosts
from core.models.database import Database
from utils.helpers import get_strike_step, get_lot_size, align_strike_price


# Engine version: backtest LOGIC badalne par bump karo (gates/pricer/warmup).
# Saved-result hash me shamil hai - purana saved result naye logic par
# dobara nahi dikhega, ek fresh run hoga (stale "kuch nahi hua" khatm).
# v2: confluence votes (13 ids) + dynaboll ATR + squeeze[i-1] + min_bars 5
# v3 (LOCKED - user decision): ye wahi PURANA v2 behavior hai, hamesha ke
#     liye baseline. Koi bhi "realism"/fills/costs/cutoff change yahan na
#     gholna - backtest results 17-19-Oct wale OLD behavior par hi rehna
#     hain. Logic badalna ho to alag engine file banao, isko na chheno.
# v5: stale-pending drop, 1-bar signal->execution, per-session entry delay,
#     in-range signal gate, exchange-true expiry calendar (NSE Tue / BSE Thu
#     / monthly last-Tue) applied to DTE + expiry square-off, causal trailing
#     stop (prior-bar best), "Lock" NameError fixed, merged metrics now keep
#     option-selling risk metrics, daily-marks Sharpe, unrounded Profit
#     Factor. Any saved v4 result does NOT carry in (fresh re-run by design).
# v6 (REALITY / paper-parity): (1) sirf COMPLETED candles - chal rahi candle
#     (forming bar) signal+exit se hat gayi; (2) SHARED SL/TP resolver
#     (backtest == paper == live: pct -> rs -> leg -> Rs1500/3000 default);
#     (3) intrabar option band DELTA-based (vega-free) - pehle BS re-price
#     se range 3-8x bada tha (unreal target hits); (4) us-din ka REAL
#     implied IV DB prints se (model IV se premium/SL/TP levels real market
#     ke qareeb); (5) latency cost entry par + global SLIPPAGE_PCT mutation
#     hata (paper/live fills beech me badal rahe the); (6) strike selection
#     LEG se (paper jaisa) - pehle engine "otm/2" default tha.
# v7 (STRICT FILLS - 80% BT vs 30% paper gap): (1) target sirf COMPLETED
#     candle ke CLOSE par book hota hai - wick/high-low touch par nahi
#     (paper monitor bhi bad-tick/spike touch par exit nahi karta); SL
#     worst-case extreme par hi rehta hai (koi optimism nahi);
#     (2) EXIT-side latency cost (stop/market/EOD exits, TP limit exempt);
#     (3) audit counters metrics.realism me (tp_wick_touches_ignored etc).
#     advanced.tp_fill_mode="touch" se purana v6 fill behavior wapas.
ENGINE_VERSION = "v7"


class BacktestEngine:
    def __init__(self, is_live: bool = False):
        self.indicators = IndicatorEngine()
        self.initial_capital = 1000000.0
        self.ohlc_cache = {}
        self.premium_cache = {}
        self.bt_symbol = ""
        self.bt_expiry = ""
        self.implied_volatility = None  # None = us din ka REAL IV (model IV banned)
        self.is_live = is_live  # Unified switch: False=backtest, True=live/paper
        self._cur_bar_time = ""
        self._skip_db = False  # True => synthetic-only fast path, no DB roundtrips
        self._cur_close = 0.0
        # Premium-provenance sets (also reset per run in _reset()).
        self._tick_prem_keys = set()
        self._db_prem_keys = set()
        self._fake_prem_keys = set()
        self._slippage_pct = 0.5  # Default dynamic slippage % for option selling
        # ---- v7 realism knobs (default = strict / real-market) ----
        # TP fill mode: "close" = target tabhi book hota hai jab COMPLETED
        # candle ka close level ke paar ho (wick/high-low touch par nahi).
        # Paper/live ka monitor bhi bad-tick/spike touch par exit nahi karta
        # (scripts.auto_trade confirm_secs re-quote) - isliye default strict.
        # "touch" = legacy v6 behavior (wick par target book).
        self._tp_fill_mode = "close"
        # Exit-side latency: stop/market exits trigger ke baad broker tak
        # pahunchne me jo drift hota hai. Default 0.0 = paper parity (paper
        # observed quote par close karta hai, koi extra drift nahi). Explicit
        # `advanced.latency_slippage_exit_pct` set karo to charge ho. TP ek
        # resting LIMIT hai (level par fill) - uspar latency kabhi nahi.
        self._latency_pct_exit = 0.0
        # Audit counters (metrics.realism me jaate hain).
        self._n_tp_confirmed = 0
        self._n_tp_unconfirmed = 0
        self._n_exit_latency = 0

    def run(self, historical, symbol, start_date, end_date, ind_list, entry_conditions,
            exit_conditions, legs, advanced_options, risk_management, is_live: bool = None) -> dict:
        if is_live is not None:
            self.is_live = is_live
        self._reset()
        # ---- REALITY: chal rahi (running) candle IGNORE ----
        # Intraday feed ka aakhri bar abhi bhi ban raha hota hai (Yahoo/Fyers
        # current candle). Us par signal = live candle par trade = look-ahead
        # (backtest me aapne bhi nahi hona chahiye). Sirf COMPLETED candles
        # par signal/exit evaluate hota hai - paper worker bhi wahi rule.
        try:
            from utils.helpers import drop_forming_bars as _dfb
            _tf_bars = str((advanced_options or {}).get("timeframe") or "5m")
            _hist2 = _dfb(historical, _tf_bars)
            if len(_hist2) != len(historical):
                try:
                    self._dropped_forming = len(historical) - len(_hist2)
                except Exception:
                    self._dropped_forming = 0
                historical = _hist2
        except Exception:
            pass
        self.premium_cache = {}
        self.bt_symbol = symbol
        # Execution-resolution diagnostics (added to metrics): intraday bars
        # are priced per bar; daily bars cannot model a 10:05 entry.
        try:
            self._used_intraday_bars = any(bool((h or {}).get("bar_time")) for h in (historical or []))
        except Exception:
            self._used_intraday_bars = False
        self._trade_mode = str(advanced_options.get("trade_mode") or "").lower()
        self._advanced = advanced_options
        # REAL option tick data mode (user rule 24-11): '1m_chain' / 'tick' /
        # 'real' par premium recorded ticks se lo, Black-Scholes nahi.
        self._use_tick_data = str(advanced_options.get("data_mode") or "").lower() in (
            "1m_chain", "1m", "tick", "ticks", "real", "real_option", "option_chain")
        self.bt_expiry = str(legs[0].get("expiry_date", "") if legs else "")[:10]
        self._explicit_expiry = bool(self.bt_expiry)
        # Premium sirf real recorded tick / 1m-chain se. IV sirf user-explicit
        # ya us din ke real option prints se — koi model IV/model premium nahi.
        self.implied_volatility = advanced_options.get("implied_volatility") or None
        # Quantman: slippage override (user enters estimated slippage % like 0.05, 0.1)
        # NOTE: class attribute TransactionCosts.SLIPPAGE_PCT ko AB mutate NAHI
        # karte - backtest ka global slippage paper/live ke fills ko bhi badal
        # deta tha (race: backtest chalte hue paper me 10x tighter fills).
        # Har backtest call site apna slippage_pct explicitly pass karta hai.
        if advanced_options.get("slippage_pct") is not None:
            try:
                self._slippage_pct = float(advanced_options.get("slippage_pct"))
            except Exception:
                self._slippage_pct = 0.5
        elif advanced_options.get("slippage") is not None:
            try:
                self._slippage_pct = float(advanced_options.get("slippage"))
            except Exception:
                self._slippage_pct = 0.5
        else:
            self._slippage_pct = 0.5
        # Quant wiring: expiry type, trailing, momentum, entry/exit times
        self._expiry_hint = advanced_options.get("expiry", "weekly")
        # Check legs for expiry override
        if legs and any(l.get("expiry")=="monthly" for l in legs):
            self._expiry_hint = "monthly"
        use_trailing = advanced_options.get("trailing_sl", False) or advanced_options.get("trailing", "") != "Lock"
        momentum = int(advanced_options.get("momentum", 0) or 0)
        entry_time = advanced_options.get("entry_time", "09:35")
        exit_time = advanced_options.get("exit_time", "15:14")
        early_exit_time = advanced_options.get("early_exit_time", "15:10")
        self._entry_time = entry_time
        self._exit_time = exit_time
        self._early_exit_time = early_exit_time
        trade_mode = str(advanced_options.get("trade_mode", advanced_options.get("trade_type", "positional")) or "positional").lower()
        if trade_mode not in ("intraday", "positional", "btst"):
            trade_mode = "positional"
        self._eq_intraday = (trade_mode == "intraday")
        # Holding period: an explicit advanced.max_holding_bars always wins.
        # Intraday default = NO artificial 25-min time exit: paper holds until
        # SL/TP or the strategy/EOD square-off (15:15), so a 5-bar cap made the
        # backtest book many small "time" exits paper never takes (same
        # strategy, different exit distribution = different win rate).
        try:
            max_holding = int(advanced_options.get("max_holding_bars", 0) or 0)
        except Exception:
            max_holding = 0
        if max_holding <= 0:
            if trade_mode == "intraday":
                max_holding = 10 ** 9  # EOD square-off closes it
            elif trade_mode == "btst":
                max_holding = 5
            else:
                max_holding = 20
        # BTST holds overnight: increase max_holding for BTST
        if trade_mode == "btst":
            max_holding = max(max_holding, 5)
        # RULEBOOK (16-11-2026): intraday me koi intermediate candle-completion
        # time exit nahi - sirf 15:15 square-off. Explicit `max_holding_bars`
        # (UI default 20) ko intraday me ignore karo, warna theta decay capture
        # nahi hota aur paper se exit distribution alag ho jata hai.
        # Opt-in: advanced.allow_intraday_time_exit = true.
        if trade_mode == "intraday" and not advanced_options.get("allow_intraday_time_exit"):
            max_holding = 10 ** 9
        # Daily caps (user rule 2026-11 = unconstrained):
        #   -1 / missing  -> UNLIMITED (new default)
        #    0            -> legacy explicit "zero trades" (tests / diagnostics)
        #   >0            -> explicit simulation cap
        try:
            _mt_raw = risk_management.get("max_trades_per_day", -1)
            max_trades_day = -1 if _mt_raw in (None, "") else int(_mt_raw)
        except Exception:
            max_trades_day = -1
        _unlimited_trades = max_trades_day < 0
        daily_loss_limit = float(risk_management.get("daily_loss_limit", 0) or 0)
        # Position sizing: यूजर मैन्युअल lots सेट करता है — ऑटो नहीं (user demand).
        # Risk amount केवल R-multiple display के लिए SL सेटिंग से निकाला जाएगा।
        # Strategy-wise MTM Stop Loss and Target (overall portfolio level).
        # ONLY explicit strategy_* keys: daily_stop_loss/take_profit are the
        # PER-TRADE SL/TP (sltp_premium_level) and are already enforced by the
        # SL/TP block below + paper's SL monitor. Old fallback applied them a
        # second time as a portfolio MTM kill-switch, closing trades at
        # -600/-900 that paper never closed (extra exit = win-rate gap).
        strategy_sl = float(risk_management.get("strategy_stop_loss", 0) or 0)
        strategy_tp = float(risk_management.get("strategy_take_profit", 0) or 0)
        # Strategy MTM SL/TP PERCENT mode ('2%' ya strategy_stop_loss_pct=2):
        # % ka matlab initial capital ka percent -> Rs threshold ban jata hai.
        try:
            _ssp = float(risk_management.get("strategy_stop_loss_pct", 0) or 0)
            if _ssp > 0 and _ssp <= 100 and self.initial_capital > 0:
                strategy_sl = self.initial_capital * _ssp / 100.0
        except Exception:
            pass
        try:
            _stp = float(risk_management.get("strategy_take_profit_pct", 0) or 0)
            if _stp > 0 and _stp <= 100 and self.initial_capital > 0:
                strategy_tp = self.initial_capital * _stp / 100.0
        except Exception:
            pass
        # Min entry premium: skip SELL entries cheaper than this (far-OTM lottery
        # tickets like Rs2-3 have tiny max profit but huge adverse-move loss).
        vix_max = float(risk_management.get("vix_max", 0) or 0)
        vix_data = advanced_options.get("vix_data") or {}
        if not vix_data:
            try:
                import json as _vj, os as _vo
                _vp = os.path.join(os.path.dirname(__file__), "..", "..", "data", "india_vix.json")
                if os.path.exists(_vp):
                    vix_data = _vj.load(open(_vp))
            except Exception:
                vix_data = {}
        # Production VIX fallback: fetch latest India VIX from NSE if static
        # file is missing/stale (Render free tier = no persistent data dir).
        _vix_latest = None
        if vix_max > 0 and not vix_data:
            try:
                import urllib.request as _ur, json as _vjson
                _ur_headers = {"User-Agent": "Mozilla/5.0"}
                # NSE India VIX endpoint
                for _url in ["https://www.nseindia.com/api/allIndices?index=INDIA%20VIX",
                             "https://www.nseindia.com/api/option-chain-indices?symbol=INDIAVIX"]:
                    try:
                        _req = _ur.Request(_url, headers=_ur_headers)
                        with _ur.urlopen(_req, timeout=5) as _r:
                            _vj = _vjson.loads(_r.read().decode())
                            if "data" in _vj:
                                for _d in _vj["data"]:
                                    if str(_d.get("index", "")).upper() == "INDIA VIX":
                                        _vix_latest = float(_d.get("last", 0) or 0)
                                        break
                            if "records" in _vj and "underlyingValue" in _vj["records"]:
                                _vix_latest = float(_vj["records"]["underlyingValue"] or 0)
                            break
                    except Exception:
                        continue
            except Exception:
                pass
        # Yahoo VIX history path completely REMOVED (user request): VIX values
        # yahan sirf NSE official archive se aate hain (INDIAVIX - ind_close_all).
        # Na mile to filter no-op (never fabricated 15.0).
        if vix_max > 0 and not vix_data:
            try:
                from core.services.historical_fetcher import _fetch_nseindex_historical as _fnx
                _vrows = _fnx("INDIAVIX", start_date, end_date) or []
                for _b in _vrows:
                    _vd = str(_b.get("trade_date") or "")[:10]
                    _vc = float(_b.get("close_price") or 0)
                    if _vd and _vc > 0:
                        vix_data[_vd] = _vc
                if not _vix_latest and vix_data:
                    _vix_latest = float(sorted(vix_data.items())[-1][1])
            except Exception:
                pass
        # Past-only VIX lookup table (carry-forward). Backtest ke har date
        # par us date tak ka LAST KNOWN VIX use hota hai - koi FUTURE value
        # (jo backtest end ke baad aaye) nahi. Missing data -> filter pass-through
        # (kabhi fake 15.0 inject nahi karte) + 'vix_no_data' counter.
        _vix_dates = sorted(vix_data.keys()) if vix_data else []
        def _vix_asof(d):
            if not _vix_dates:
                return 0.0
            import bisect as _bi
            try:
                i = _bi.bisect_right(_vix_dates, str(d)[:10]) - 1
            except Exception:
                return 0.0
            if i < 0:
                return 0.0
            return float(vix_data[_vix_dates[i]])

        # Neutral default VIX if nothing available (won't trigger filter)
        if _vix_latest is None:
            _vix_latest = 0.0
        # Min entry premium: skip SELL entries cheaper than this (far-OTM lottery
        # tickets like Rs2-3 have tiny max profit but huge adverse-move loss).
        min_premium = float(advanced_options.get("min_entry_premium", 0) or 0)
        # Entry delay: wait N bars after entry_time for trend confirmation (Quantman-style)
        # e.g., 3-6 bars = 15-30 min after 09:35
        entry_delay_bars = int(advanced_options.get("entry_delay_bars", 0) or 0)
        # Signal reversal exit: if trend reverses against position, exit immediately
        reversal_exit = bool(advanced_options.get("reversal_exit", True))
        # Early mandatory exit: close by 15:00-15:10 instead of 15:14
        early_exit_time = advanced_options.get("early_exit_time", "15:10")
        signal_delay = int(advanced_options.get("signal_delay_bars", 0) or 0)
        # ---- ENTRY LATENCY (paper fill delay), in BARS ----
        # Paper order signal ke baad broker tak jaata hai aur ~1-5 min baad
        # fill hota hai. Backtest me signal bar ke CLOSE par entry hoti hoti
        # to backtest imaginary fill dikhata hai. Rate:
        #   advanced.latency_delay_bars (int, default 1, max 5)
        #     0 = signal bar par hi (NOT paper-realistic)
        #     1 = next bar = 5m timeframe me ~5 min fill delay (paper parity)
        #     2+ = slow fill / wide window
        # Signal bar se N bars baad bar ke open par fill hota hai.
        try:
            _lb = int(advanced_options.get("latency_delay_bars", 1) or 1)
        except Exception:
            _lb = 1
        self._entry_latency_bars = max(0, min(5, _lb))
        # Latency in seconds (only in backtest mode) - unified is_live switch
        latency = TransactionCosts.latency_delay(self.is_live)
        # Latency COST: signal -> broker -> fill ke beech ka price drift.
        # Signal NEXT bar ke open par fill hota hai (1-bar delay pehle se
        # hai), par us window me jo hota hai uska cost backtest me count
        # nahi hota tha (live me broker latency + spread dono lagte hain).
        # advanced.latency_slippage_pct (default 0.05% per entry).
        try:
            self._latency_pct = float(advanced_options.get("latency_slippage_pct", 0.05) or 0)
        except Exception:
            self._latency_pct = 0.05
        # Exit-side latency % (default 0.0 = paper parity; explicit set karo
        # to stop/market exits slip karte hain). TP limit level par hi fill.
        try:
            _lx = advanced_options.get("latency_slippage_exit_pct", None)
            self._latency_pct_exit = 0.0 if _lx is None else float(_lx or 0)
        except Exception:
            self._latency_pct_exit = 0.0
        # Strict target confirmation (v7 default). "touch" = legacy.
        try:
            _tpm = str(advanced_options.get("tp_fill_mode") or "close").lower()
        except Exception:
            _tpm = "close"
        self._tp_fill_mode = _tpm if _tpm in ("close", "touch") else "close"
        # Purane counters reset (engine instance reuse ho sakta hai).
        self._n_tp_confirmed = 0
        self._n_tp_unconfirmed = 0
        self._n_exit_latency = 0
        self._latency_s = float(latency or 0)
        leg = legs[0] if legs else {"option_type": "CE", "lots": 1, "transaction": "buy"}
        option_type = leg.get("option_type", "CE")
        # EQ legs: qty = share count (lots field), NOT lots x F&O lot-size.
        # Mixing them minted crore-rupee fantasy P&L (entry qty 10 vs exit 2500).
        if str(option_type).upper() == "EQ":
            qty = max(1, int(leg.get("lots", 1) or 1))
        else:
            qty = int(leg.get("lots", 1)) * get_lot_size(symbol)
        txn_type = leg.get("transaction", "buy").lower()
        leg_sl, leg_tp, sl_unit, tp_unit = self._resolve_sltp(leg, risk_management)
        self._sl_unit = sl_unit
        self._tp_unit = tp_unit
        self._sl_amt = leg_sl
        self._tp_amt = leg_tp
        # Strike selection: LEG first (paper/live bhi leg se hi strike choose
        # karte hain), advanced_options fallback. Pehle engine default
        # "otm/2" tha jabki paper "atm/0" use karta tha - dono ALAG strikes
        # par trade karte the (same strategy, different trade set = different
        # win rate).
        _leg0 = leg if isinstance(leg, dict) else {}
        strike_sel = str((_leg0.get("strike_selection")
                          or advanced_options.get("strike_selection")
                          or "atm")).lower()
        if strike_sel not in ("atm", "otm", "itm", "delta"):
            strike_sel = "atm"
        delta_target = advanced_options.get("delta_target")
        try:
            otm_dist = int(_leg0.get("otm_distance",
                                     advanced_options.get("otm_distance", 0)) or 0)
        except Exception:
            otm_dist = 0
        # trade_mode set above (intraday/positional/btst); reused here

        closes = [h["close_price"] for h in historical]
        highs = [h["high_price"] for h in historical]
        lows = [h["low_price"] for h in historical]
        pre_calc = self._pre_calc(historical, closes, highs, lows, ind_list)
        # ---- STRICT TREND GATE (rulebook 16-11-2026) ----
        # ADX(14) > 22 + VWAP(20) direction filter. `advanced.strict_trend`
        # se ON (backtest + paper + live - same engine, same gate).
        self._strict_gate = None
        try:
            _st_on = str(advanced_options.get("strict_trend") or "").lower() in (
                "1", "true", "yes", "on")
            if _st_on:
                _adx = self.indicators.calculate_adx(historical, 14) or {}
                # "Institutional VWAP" = SESSION-anchored (har naye din par
                # reset) - 5m bars par cumulative VWAP meaningless hota hai.
                _v_vals = []
                try:
                    from core.services.strategy_rulebook import session_vwap as _svwap
                    _v_vals = list(_svwap(historical) or [])
                except Exception:
                    _v_vals = []
                if not _v_vals:
                    _vw = self.indicators.calculate_vwap(historical, 20, 2.0) or {}
                    _v_vals = list((_vw or {}).get("vwap", []) or [])
                _a_vals = list((_adx or {}).get("adx", []) or [])
                if _a_vals or _v_vals:
                    self._strict_gate = (_a_vals, _v_vals)
        except Exception:
            self._strict_gate = None
        # Store indicator modes for bullish/bearish filtering
        self.ind_modes = {}
        for ind in ind_list:
            iid = ind.get("id","") if isinstance(ind, dict) else ind
            mode = ind.get("params",{}).get("mode","both") if isinstance(ind, dict) else "both"
            self.ind_modes[iid] = str(mode).lower()

        entries, exits, entry_bars = [], [], []
        pending_entry = None
        pending_exit = None
        pending_entry_signal_time = None  # Signal bar time (candle close)
        pending_auto_buy = None
        daily_trades = 0
        # Diagnostics (spec §13): evaluated/generated/rejected + coverage.
        # Kabhi inflate nahi karte - sirf gintee, signal logic untouched.
        self._n_eval = 0
        self._n_sig = 0
        self._n_sig_buy = 0
        self._n_sig_sell = 0
        self._rej = {}
        self._cov_days = set()
        self._cov_bars = 0
        self._cov_bt = 0
        try:
            self._ind_ids = [(d.get("id", "") if isinstance(d, dict) else str(d or ""))
                              for d in (ind_list or [])]
        except Exception:
            self._ind_ids = []
        daily_pnl = 0.0
        kill_switch_on = False
        last_date = ""
        # Warmup gate: pehle 15 bars skip hote the -> 15-bar series me sirf
        # 1 bar evaluate hota tha (aakhri bar me signal banta hi nahi), isliye
        # HAR chhoti-window strategy me 0 trades aata tha. Abstain rule
        # (na-compute-hua indicator vote nahi deta) ke saath 5 bars kaafi
        # hain -jaldi bars par kuch computable nahi to signal nahi banta,
        # fabrication nahi hota, par evaluable bars 1 se ~10 ho jate hain.
        min_bars = min(5, max(1, len(historical) - 2))
        legs = legs if legs else [{"option_type": "CE", "lots": 1, "transaction": "buy"}]
        is_spread = len(legs) > 1

        session_start_idx = min_bars  # bar index where the current session started
        for i in range(min_bars, len(historical)):
            cur = historical[i]
            try:
                self._cur_close = float(cur.get("close_price", 0) or 0)
            except Exception:
                pass
            # Actual execution-bar clock time (intraday bars carry bar_time;
            # daily bars have none -> session-time fallback in enter/close).
            try:
                self._cur_bar_time = str(cur.get("bar_time") or "")
            except Exception:
                self._cur_bar_time = ""
            cur_date = cur["trade_date"]
            nxt = historical[i + 1] if i + 1 < len(historical) else None
            is_last = nxt is None or nxt["trade_date"] != cur_date
            # Roll option expiry forward: a cached expiry on/before today collapses
            # Black-Scholes time to ~1 day, so premiums -> ~0 and SL/TP never hit.
            # Positional holds are NOT rolled while a position is open into expiry:
            # it is squared off on expiry day instead (see expiry block below).
            exp_date = str(self.bt_expiry)[:10] if self.bt_expiry else ""
            try:
                if exp_date and exp_date < cur_date:
                    if trade_mode != "intraday" and len(entries) > len(exits):
                        pass  # held into expiry: square-off block handles it
                    else:
                        self.bt_expiry = ""
                        exp_date = ""
            except Exception:
                pass

            if cur_date != last_date:
                daily_trades = 0
                daily_pnl = 0.0
                kill_switch_on = False
                last_date = cur_date
                session_start_idx = i

            if kill_switch_on:
                continue

            has_open = len(entries) > len(exits)
            # Warmup bars (dated before start_date) feed indicators only -
            # entries strictly inside the requested range, so trade dates
            # can never leak outside [start_date, end_date].
            try:
                _in_range = str(cur_date) >= str(start_date)[:10]
            except Exception:
                _in_range = True
            can_enter = not has_open and (_unlimited_trades or daily_trades < max_trades_day) and _in_range
            # Backtest entry clock window (paper parity): intraday entries
            # only at/after advanced.entry_time and before exit_time/15:00.
            # Before this the engine entered on 09:25/09:30 signals while
            # paper's worker waited for 09:35+ -> different first trade.
            if can_enter and trade_mode == "intraday" and self._cur_bar_time:
                try:
                    _bt = str(self._cur_bar_time)[:5]
                    _bh, _bm = _bt.split(":")
                    _nowm = int(_bh) * 60 + int(_bm)
                    _et = str(getattr(self, "_entry_time", "09:35") or "09:35")[:5]
                    _eh, _em = _et.split(":")
                    _startm = int(_eh) * 60 + int(_em)
                    _xt = str(getattr(self, "_exit_time", "15:14") or "15:14")[:5]
                    _xh, _xm = _xt.split(":")
                    _exitm = min(15 * 60, int(_xh) * 60 + int(_xm))
                    if _nowm < _startm or _nowm >= _exitm:
                        can_enter = False
                except Exception:
                    pass

            # Execute pending entry from N bars after the signal bar's signal
            # (entry latency = paper's broker fill delay; no look-ahead).
            # A pending order is VALID ONLY at the (pending+N)th bar - after
            # that it is dropped forever (never lingers into another session).
            _elb = int(getattr(self, "_entry_latency_bars", 1) or 1)
            if (pending_entry is not None
                    and (i - pending_entry) > _elb):
                pending_entry = None
                pending_auto_buy = None
                pending_entry_signal_time = None
            if (pending_entry is not None and can_enter
                    and (i - pending_entry) >= _elb):
                exec_idx = i
                exec_bar = historical[exec_idx] if exec_idx < len(historical) else cur
                spot = float(exec_bar.get("open_price", 0) or exec_bar.get("close_price", 0))
                auto_signal_exec = bool(advanced_options.get("auto_signal") or (legs and legs[0].get("transaction","").lower()=="auto"))
                if auto_signal_exec:
                    # Choose CE Buy on bullish (buy_sig) else PE Buy
                    is_buy = pending_auto_buy if pending_auto_buy is not None else True
                    auto_leg = {"option_type": "CE" if is_buy else "PE", "transaction": "buy", "lots": int(legs[0].get("lots",1) if legs else 1), "strike_selection": strike_sel, "otm_distance": otm_dist}
                    trade = self._enter_single(cur_date, spot, symbol, auto_leg, strike_sel, delta_target, otm_dist)
                    # Preserve auto choice for display
                    trade["auto_choice"] = "CE-BUY" if is_buy else "PE-BUY"
                    trade["signal_time"] = pending_entry_signal_time or trade.get("time", "")
                elif is_spread:
                    trade = self._enter_spread(cur_date, spot, symbol, legs, strike_sel, delta_target, otm_dist)
                    if trade is None:
                        pending_entry = None
                        pending_auto_buy = None
                        continue
                    trade["signal_time"] = pending_entry_signal_time or trade.get("time", "")
                else:
                    trade = self._enter_single(cur_date, spot, symbol, legs[0], strike_sel, delta_target, otm_dist)
                    if trade is None:
                        continue
                    trade["signal_time"] = pending_entry_signal_time or trade.get("time", "")
                if trade is None:
                    continue
                # Min-premium guard: Rs20 se saste entries (BUY or SELL) kabhi
                # nahi - signal-time estimate chook sakta hai, isliye ACTUAL
                # entry premium par final check. Skip without using the day's
                # trade quota so a better setup can still enter later.
                # (Spreads exempt: net credit/debit premium compare nahi hota.)
                if (min_premium > 0 and not is_spread
                        and float(trade.get("price", 0) or 0) < min_premium):
                    pending_entry = None
                    pending_auto_buy = None
                    try:
                        # Dynamic label: threshold ke saath (pehle hamesha
                        # 'min_premium_lt_20' likha tha chahe ₹15 ho ya ₹20 -
                        # diagnostics aur filter rule match nahi hote the).
                        _key = f"min_premium_lt_{int(min_premium)}"
                        self._rej[_key] = self._rej.get(_key, 0) + 1
                    except Exception:
                        pass
                    continue
                entries.append(trade)
                entry_bars.append(i)
                daily_trades += 1
                # ---- Per-trade risk amount (for R-multiple) ----
                try:
                    _sl_unit = getattr(self, "_sl_unit", "rs")
                    _sl_amt = leg_sl if leg_sl else 0
                    _qty = int(trade.get("quantity", 0) or 0)
                    if _sl_unit == "pct" and _sl_amt > 0:
                        _risk_rs = (_sl_amt / 100.0) * float(trade.get("price", 0)) * _qty
                    elif str(_sl_unit).lower() in ("pt", "points") and _sl_amt > 0:
                        # User rule 24-11: Risk Rs = sl_points × Qty (qty already
                        # = lots × lot_size). Pehle sirf sl_points dikhata tha
                        # (-30.00) bina qty multiply kiye -> galat R-multiple.
                        _risk_rs = float(_sl_amt) * _qty
                    else:
                        _risk_rs = float(_sl_amt)
                    trade["risk_amount_rs"] = round(_risk_rs, 2)
                except Exception:
                    trade["risk_amount_rs"] = 0.0
                pending_entry = None
                pending_auto_buy = None
                pending_entry_signal_time = None  # Consumed

            # No signal/SL/TP exits on the entry bar of INTRADAY (minute)
            # bars (0-hold round-trips at one timestamp are look-ahead
            # fantasy). Daily bars have no clock: entry at open, SL/TP range
            # and EOD exit all apply to the same date honestly (DB parity).
            try:
                just_entered = (bool(entry_bars) and entry_bars[-1] == i
                                and bool(cur.get("bar_time")))
            except Exception:
                just_entered = False
            has_open = len(entries) > len(exits)
            if has_open and pending_exit is not None and not just_entered:
                entry = entries[len(exits)]
                exec_idx = i
                exec_bar = historical[exec_idx] if exec_idx < len(historical) else cur
                spot = float(exec_bar.get("open_price", 0) or exec_bar.get("close_price", 0))
                exit_prem = self._exit_premium(cur_date, spot, float(entry["strike"]), option_type)
                # Apply transaction costs with is_live flag
                exit_prem = TransactionCosts.apply_fill_slippage(exit_prem, "SELL" if txn_type == "buy" else "BUY", self.is_live, slippage_pct=float(getattr(self, "_slippage_pct", 0.5) or 0.5))
                self._close_position(entries, exits, entry, exit_prem, pending_exit, cur_date, qty, txn_type,
                                     trigger=f"signal:{pending_exit}")
                daily_pnl += exits[-1]["pnl"]
                if (daily_loss_limit > 0 and daily_pnl <= -daily_loss_limit) or (strategy_sl > 0 and daily_pnl <= -strategy_sl) or (strategy_tp > 0 and daily_pnl >= strategy_tp):
                    kill_switch_on = True
                pending_exit = None

            has_open = len(entries) > len(exits) and not just_entered
            # SL/TP checked on EVERY bar first (daily bars: each bar is day-close,
            # so skipping SL on is_last meant intraday SL never triggered).
            if has_open:
                entry = entries[len(exits)]
                if not entry.get("is_spread") and (leg_sl > 0 or leg_tp > 0):
                    hit = self._check_sl_tp(cur, entry, option_type, txn_type, leg_sl, leg_tp, qty, entry_idx=len(exits))
                    if hit:
                        # SL हिट होने पर मार्केट स्पाइक के चाले एक्स्ट्रा 1% स्लिपेज (विशेषकर SELL पोजीशन पर)
                        _fill = hit.get("fill", hit["level"])
                        # PARITY FIX (rulebook 16-11-2026): SL fill bar ke
                        # EXTREME par nahi - level ke paas (stop-market order
                        # ~level par fill hota hai). Pehle BT har stop par
                        # ~2x zyada loss book karta tha (avg -50 pts vs 25 pt
                        # SL) jabki paper/live level par katte the - isi se
                        # bada 'Paper-BT Δ' ban raha tha. Ab teenon ka fill
                        # rule SAME hai (level ±2% cap).
                        if str(hit.get("reason")) == "stoploss":
                            try:
                                from core.services.strategy_rulebook import sl_fill_cap as _capf
                                _sl_pts = None
                                try:
                                    _sl_pts = abs(float(entry.get("price") or 0)
                                                  - float(hit.get("level") or 0))
                                except Exception:
                                    _sl_pts = None
                                _cap_px = _capf(float(hit.get("level") or 0),
                                                txn_type == "buy", 2.0, _sl_pts)
                                if _cap_px > 0:
                                    if txn_type == "buy":
                                        # BUY ke exit me hum SELL karte hain -
                                        # neeche ka price worse hai -> fill
                                        # cap se NEECHE nahi ja sakta.
                                        _fill = max(float(_fill), _cap_px)
                                    else:
                                        # SELL ke exit me hum BUY karte hain -
                                        # upar ka price worse hai.
                                        _fill = min(float(_fill), _cap_px)
                            except Exception:
                                pass
                        # Parity: paper close_trade applies the SAME shared
                        # slippage once. Old code added +1% extra on SELL SL
                        # exits, so backtest lost more than paper on identical
                        # trades (win-rate unaffected, PnL gap inflated).
                        sl_exit = TransactionCosts.apply_fill_slippage(
                            _fill, "SELL" if txn_type == "buy" else "BUY", self.is_live,
                            slippage_pct=float(getattr(self, "_slippage_pct", 0.5) or 0.5))
                        _reason = hit["reason"]
                        if _reason == "stoploss" and getattr(self, "_last_sl_trailed", False):
                            # Trailing ne SL entry ke paar le jaya tha — ye
                            # 'stoploss' nahi, 'trailing_lock' exit hai (sahi label).
                            _reason = "trailing_lock"
                        _trig = f"{'SL' if hit['reason'] == 'stoploss' else 'TP'}@{float(hit['level']):.2f}"
                        self._close_position(entries, exits, entry, sl_exit, _reason, cur_date, qty, txn_type,
                                             trigger=_trig)
                        daily_pnl += exits[-1]["pnl"]
                        if daily_loss_limit > 0 and daily_pnl <= -daily_loss_limit:
                            kill_switch_on = True

            # Strategy-wise MTM Stop Loss / Target — unrealized MTM of ALL open
            # positions + realized daily_pnl. Limit hit => square-off everything.
            has_open = len(entries) > len(exits) and not just_entered
            if has_open and (strategy_sl > 0 or strategy_tp > 0):
                try:
                    open_unreal = sum(self._open_unrealized(entries[j], cur, option_type, txn_type, qty) for j in range(len(exits), len(entries)))
                except Exception:
                    open_unreal = 0.0
                total_mtm = daily_pnl + open_unreal
                if (strategy_sl > 0 and total_mtm <= -strategy_sl) or (strategy_tp > 0 and total_mtm >= strategy_tp):
                    # Trigger label shows ACTUAL values (spec §9): e.g.
                    # MTM -751<=-300/sym - kabhi overall-SL se confuse na ho.
                    try:
                        _lim = f"-{strategy_sl:.0f}" if total_mtm <= 0 else f"+{strategy_tp:.0f}"
                        _trig_mtm = f"MTM {total_mtm:.0f} vs {_lim}/sym"
                    except Exception:
                        _trig_mtm = "strategy_sl_tp"
                    # Close all open positions simultaneously
                    while len(entries) > len(exits):
                        entry = entries[len(exits)]
                        if entry.get("is_spread"):
                            self._close_spread(entries, exits, entry, "strategy_sl_tp", cur_date, trigger=_trig_mtm)
                        else:
                            exit_prem = self._close_premium(cur_date, float(cur["close_price"]), float(entry["strike"]), option_type)
                            exit_prem = TransactionCosts.apply_fill_slippage(exit_prem, "SELL" if txn_type == "buy" else "BUY", self.is_live, slippage_pct=float(getattr(self, "_slippage_pct", 0.5) or 0.5))
                            self._close_position(entries, exits, entry, exit_prem, "strategy_sl_tp", cur_date, qty, txn_type,
                                                 trigger=_trig_mtm)
                        daily_pnl += exits[-1]["pnl"]
                    kill_switch_on = True

            # Intraday square-off, same SESSION as entry (never next-day):
            # minute bars -> last bar of the day; daily bars -> the entry
            # bar's own close (one bar IS one session). Stale multi-day
            # intraday holds are also flushed (entry_date < today).
            # NOTE: entry-bar EOD is ALLOWED (no just_entered gate): fill is
            # at open, exit at close - no lookahead, and paper also force-
            # closes same evening (intraday_auto/strategy_exit). Gating it
            # carried last-bar entries overnight (gap risk paper never has).
            has_open_eod = len(entries) > len(exits)
            is_early_exit = False
            if trade_mode == "intraday" and has_open_eod:
                try:
                    _ed = str(entries[len(exits)].get("date", "") or "")
                except Exception:
                    _ed = ""
                if cur.get("bar_time"):
                    # User rule 24-11-2026: intraday square-off EXACTLY 15:15
                    # (never 15:25). Bar start time >= 15:15 par close karo;
                    # 15:15 ki bar itself last ho to bhi yahin trigger hoga.
                    # 15:15 se pehle wali bars par nahi nikalo.
                    try:
                        _bth = str(cur.get("bar_time"))[:5]
                        _bh, _bm = _bth.split(":")
                        _bar_min = int(_bh) * 60 + int(_bm)
                        is_early_exit = _bar_min >= 15 * 60 + 15 or is_last
                    except Exception:
                        is_early_exit = bool(is_last)
                else:
                    is_early_exit = bool(is_last and _ed and _ed <= cur_date)
            if has_open_eod and trade_mode == "intraday" and is_early_exit:
                entry = entries[len(exits)]
                if entry.get("is_spread"):
                    self._close_spread(entries, exits, entry, "intraday", cur_date, trigger="EOD")
                else:
                    exit_prem = self._close_premium(cur_date, float(cur["close_price"]), float(entry["strike"]), option_type)
                    exit_prem = TransactionCosts.apply_fill_slippage(exit_prem, "SELL" if txn_type == "buy" else "BUY", self.is_live, slippage_pct=float(getattr(self, "_slippage_pct", 0.5) or 0.5))
                    self._close_position(entries, exits, entry, exit_prem, "intraday", cur_date, qty, txn_type,
                                         trigger="EOD")
                daily_pnl += exits[-1]["pnl"]
                if (daily_loss_limit > 0 and daily_pnl <= -daily_loss_limit) or (strategy_sl > 0 and daily_pnl <= -strategy_sl) or (strategy_tp > 0 and daily_pnl >= strategy_tp):
                    kill_switch_on = True

            # Expiry-day square-off for positional carries (NO roll-over):
            # if the open position is held on/after its contract expiry,
            # exit at the day's close instead of shifting to the next expiry.
            has_open = len(entries) > len(exits) and not just_entered
            if has_open and trade_mode != "intraday" and exp_date and cur_date >= exp_date and is_last:
                entry = entries[len(exits)]
                if entry.get("is_spread"):
                    self._close_spread(entries, exits, entry, "expiry_squareoff", cur_date, trigger="expiry")
                else:
                    exit_prem = self._close_premium(cur_date, float(cur["close_price"]), float(entry["strike"]), option_type)
                    exit_prem = TransactionCosts.apply_fill_slippage(exit_prem, "SELL" if txn_type == "buy" else "BUY", self.is_live, slippage_pct=float(getattr(self, "_slippage_pct", 0.5) or 0.5))
                    self._close_position(entries, exits, entry, exit_prem, "expiry_squareoff", cur_date, qty, txn_type,
                                         trigger="expiry")
                daily_pnl += exits[-1]["pnl"]
                if (daily_loss_limit > 0 and daily_pnl <= -daily_loss_limit) or (strategy_sl > 0 and daily_pnl <= -strategy_sl) or (strategy_tp > 0 and daily_pnl >= strategy_tp):
                    kill_switch_on = True
                self.bt_expiry = ""
                exp_date = ""

            if nxt is not None:
                bars_held = 0
                if len(entries) > len(exits):
                    eidx = len(exits)
                    bars_held = i - (entry_bars[eidx] if eidx < len(entry_bars) else i)
                time_exit = bars_held >= max_holding
                # Generate signals using CLOSED bar (i-1) - NO LOOK-AHEAD
                # Signal generated at bar i-1, execution at bar i
                buy_sig = self._get_buy_signal(i, pre_calc, historical, entry_conditions)
                sell_sig = self._get_sell_signal(i, pre_calc, historical, exit_conditions)
                # Diagnostics counters only (logic untouched)
                try:
                    if _in_range:
                        self._n_eval += 1
                        self._cov_days.add(str(cur_date)[:10])
                        if buy_sig or sell_sig:
                            self._n_sig += 1
                        if buy_sig:
                            self._n_sig_buy = self._n_sig_buy + 1 if hasattr(self, "_n_sig_buy") else 1
                        if sell_sig:
                            self._n_sig_sell = self._n_sig_sell + 1 if hasattr(self, "_n_sig_sell") else 1
                except Exception:
                    pass
                # For spreads (multi-leg like Bear Call Spread), allow entry on either signal to avoid 0 trades when single indicator rare
                # Auto-signal mode: if advanced auto_signal, pick CE on buy_sig, PE on sell_sig
                auto_signal = bool(advanced_options.get("auto_signal") or (legs and legs[0].get("transaction","").lower()=="auto"))
                if auto_signal:
                    # For AUTO, entry on any signal; leg type chosen dynamically at execution
                    entry_sig = buy_sig or sell_sig
                    if not entry_sig and not pre_calc:
                        entry_sig = True
                    exit_sig = time_exit
                elif is_spread:
                    entry_sig = buy_sig or sell_sig
                    if not entry_sig and not pre_calc:
                        entry_sig = True
                    exit_sig = time_exit
                else:
                    # Direction-aware: CE Buy = bullish (buy_sig), PE Buy = bearish (sell_sig)
                    # CE Sell = bearish, PE Sell = bullish
                    # Signal-based EXIT only when the user wrote explicit
                    # exit_conditions. Paper holds until SL/TP/EOD (its monitor
                    # has no opposite-indicator exit), so an empty conditions
                    # list must NOT create a backtest-only exit.
                    _exit_on_signal = bool(exit_conditions)
                    if option_type == "PE":
                        entry_sig = sell_sig if txn_type == "buy" else buy_sig
                        exit_sig = (buy_sig if txn_type == "buy" else sell_sig) if _exit_on_signal else False
                    else:
                        entry_sig = buy_sig if txn_type == "buy" else sell_sig
                        exit_sig = (sell_sig if txn_type == "buy" else buy_sig) if _exit_on_signal else False
                # Market Regime filter: when selected, only enter in matching regime
                if "market_regime" in (pre_calc or {}):
                    try:
                        mr = pre_calc["market_regime"]
                        rg = mr.get("regime", []) if isinstance(mr, dict) else []
                        _cur_regime = rg[i] if i < len(rg) else "sideways"
                        # PE Sell / CE Buy enter on buy_sig -> need bullish regime
                        if (option_type == "PE" and txn_type == "sell") or (option_type == "CE" and txn_type == "buy"):
                            if _cur_regime == "bearish":
                                entry_sig = False
                        # CE Sell / PE Buy enter on sell_sig -> need bearish regime
                        if (option_type == "CE" and txn_type == "sell") or (option_type == "PE" and txn_type == "buy"):
                            if _cur_regime == "bullish":
                                entry_sig = False
                    except Exception:
                        pass
                # ---- STRICT TREND GATE (rulebook 16-11-2026) ----
                # ADX(14) strictly > 22 (sideways/choppy me entry nahi) +
                # VWAP direction (CE: close > VWAP, PE: close < VWAP).
                # `advanced.strict_trend = True` se ON. Signal bar (i) ki
                # CLOSED values par evaluate - look-ahead nahi (execution
                # bar i+1 par hoti hai).
                if entry_sig and self._strict_gate is not None:
                    try:
                        from core.services.strategy_rulebook import strict_trend_ok as _stg
                        _g = self._strict_gate
                        _adx = _g[0][i] if i < len(_g[0]) else None
                        _vwap = _g[1][i] if i < len(_g[1]) else None
                        _close = float(cur.get("close_price", 0) or 0)
                        if not _stg(_adx, _close, _vwap, option_type,
                                    "BUY" if txn_type == "buy" else "SELL"):
                            entry_sig = False
                            try:
                                self._rej["strict_trend_adx_vwap"] = (
                                    self._rej.get("strict_trend_adx_vwap", 0) + 1)
                            except Exception:
                                pass
                    except Exception:
                        pass
                has_open = len(entries) > len(exits)
                can_enter = not has_open and (_unlimited_trades or daily_trades < max_trades_day) and _in_range
                # ---- RULEBOOK re-entry policy (16-11-2026) ----
                # "No Re-entry" / "RE Next Day" = SL (ya TP) ke baad SAME DAY
                # dobara entry nahi. Pehle ye field sirf UI/saved config me
                # thi - engine ignore karta tha, isliye ek hi din me 5 SL ke
                # baad 5 naye trades (re-entry churn) ho rahe the.
                if can_enter and exits:
                    try:
                        _pol = getattr(self, "_advanced", {}) or {}

                        def _same_day_blocked(_p):
                            _p = str(_p or "").strip().lower()
                            if not _p:
                                return False
                            return _p.startswith("no") or ("next day" in _p)

                        _lrsn = str(exits[-1].get("reason") or "").lower()
                        _ldate = str(exits[-1].get("date") or "")[:10]
                        if _ldate and _ldate == str(cur_date)[:10]:
                            if _lrsn in ("stoploss", "trailing_lock") and _same_day_blocked(_pol.get("reentry_on_sl")):
                                can_enter = False
                            elif _lrsn == "target" and _same_day_blocked(_pol.get("reentry_on_tp")):
                                can_enter = False
                            if not can_enter:
                                self._rej["reentry_block"] = (
                                    self._rej.get("reentry_block", 0) + 1)
                    except Exception:
                        pass
                # Entry delay: wait N bars after the SESSION opens (per-day), not
                # since the start of the whole backtest window.
                if entry_delay_bars > 0:
                    bars_since_open = i - session_start_idx
                    if bars_since_open < entry_delay_bars:
                        can_enter = False
                # Min premium filter: skip SELL entries with premium below threshold
                if can_enter and min_premium > 0:
                    # REAL premium only (lakehouse tick/chain -> DB print).
                    # No Black-Scholes estimate: if real premium is unknown,
                    # do not reject the signal on a fabricated number.
                    try:
                        spot_est = float(cur.get("open_price", 0) or cur.get("close_price", 0))
                        strike_est = float(legs[0].get("strike", 0) or 0)
                        opt_type = legs[0].get("option_type", option_type)
                        est_prem = self._entry_premium(str(cur_date)[:10], spot_est, strike_est, opt_type)
                        if est_prem is not None and float(est_prem) < min_premium:
                            can_enter = False
                            _key = f"min_premium_lt_{int(min_premium)}"
                            self._rej[_key] = self._rej.get(_key, 0) + 1
                    except Exception:
                        pass
                # VIX filter: skip entry on high-volatility days (VIX > threshold)
                if can_enter and vix_max > 0:
                    try:
                        _vix = float(vix_data.get(str(cur_date)[:10], 0) or 0)
                        # Fallback: carry-forward last known PAST value (never
                        # a future-dated number - no look-ahead). Missing
                        # pura history -> pass-through + count.
                        if _vix == 0:
                            _vix = _vix_asof(str(cur_date)[:10])
                        if _vix == 0:
                            self._rej["vix_no_data"] = self._rej.get("vix_no_data", 0) + 1
                        if _vix > 0 and _vix > vix_max:
                            can_enter = False
                            self._rej["vix_filter"] = self._rej.get("vix_filter", 0) + 1
                    except Exception:
                        pass
                try:
                    if entry_sig and not can_enter and _in_range:
                        if has_open:
                            self._rej["position_already_open"] = self._rej.get("position_already_open", 0) + 1
                        elif (not _unlimited_trades) and daily_trades >= max_trades_day:
                            self._rej["max_trades_day"] = self._rej.get("max_trades_day", 0) + 1
                        else:
                            self._rej["entry_gates"] = self._rej.get("entry_gates", 0) + 1
                except Exception:
                    pass
                if entry_sig and can_enter:
                    # Signal is on bar i's close; execution happens at bar i+1's
                    # open. One pending order per signal, consumed on the next bar
                    # (dropped if that bar cannot trade).
                    pending_entry = i
                    pending_entry_signal_time = (str(cur.get("bar_time", "")) or str(cur.get("trade_date", ""))) if not (self.is_live or is_spread or auto_signal) else None
                    if auto_signal:
                        pending_auto_buy = buy_sig
                if has_open and pending_exit is None and (exit_sig or time_exit):
                    pending_exit = "condition" if exit_sig else "time"

        # Expiry-day square-off on the LAST bar only (never a future price -
        # _square_off_expiry no-ops for any other cur_date).
        if not self.is_live and historical:
            self._square_off_expiry(historical, entries, exits, symbol, option_type, txn_type, qty, risk_management,
                                    cur_date=historical[-1]["trade_date"])

        while len(exits) < len(entries):
            idx = len(exits)
            entry = entries[idx]
            last = historical[-1]
            if entry.get("is_spread"):
                self._close_spread(entries, exits, entry, "end_of_period", last["trade_date"])
            else:
                exit_prem = self._close_premium(last["trade_date"], float(last["close_price"]), float(entry["strike"]), option_type)
                exit_prem = TransactionCosts.apply_fill_slippage(exit_prem, "SELL" if txn_type == "buy" else "BUY", self.is_live, slippage_pct=float(getattr(self, "_slippage_pct", 0.5) or 0.5))
                self._close_position(entries, exits, entry, exit_prem, "end_of_period", last["trade_date"], qty, txn_type,
                                     trigger="end_of_period")

        # Quantman: restore slippage (global mutation removed - see run())
        # Coverage proof (spec §1/§13): requested vs actual data window.
        try:
            _present = sorted({str(h.get("trade_date", ""))[:10] for h in (historical or []) if h.get("trade_date")})
            _bt = sum(1 for h in (historical or []) if h.get("bar_time"))
            import datetime as _cdt
            _s = _cdt.datetime.strptime(str(start_date)[:10], "%Y-%m-%d").date()
            _e = _cdt.datetime.strptime(str(end_date)[:10], "%Y-%m-%d").date()
            _req, _d = [], _s
            while _d <= _e:
                if _d.weekday() < 5:
                    _req.append(_d.strftime("%Y-%m-%d"))
                _d += _cdt.timedelta(days=1)
            _have = set(_present)
            _miss = [d for d in _req if d not in _have]
            _inrng = [d for d in _present if str(start_date)[:10] <= d <= str(end_date)[:10]]
            self._cov = {
                "requested_start": str(start_date)[:10], "requested_end": str(end_date)[:10],
                "data_start": _inrng[0] if _inrng else "", "data_end": _inrng[-1] if _inrng else "",
                "trading_days": len(_req), "days_with_data": len([d for d in _req if d in _have]),
                "candles": len(historical or []), "intraday_candles": _bt,
                "missing_days_count": len(_miss), "missing_days": _miss[:25],
            }
        except Exception:
            try:
                self._cov = {"trading_days": 0, "days_with_data": 0, "candles": len(historical or []),
                             "missing_days_count": 0, "missing_days": []}
            except Exception:
                self._cov = {}
        return self._build_result(symbol, start_date, end_date, entries, exits, historical=historical)

    def _pre_calc(self, historical, closes, highs, lows, ind_list):
        result = {}
        for ind in ind_list:
            iid = ind.get("id", "") if isinstance(ind, dict) else ind
            params = ind.get("params", {}) if isinstance(ind, dict) else {}
            if iid == "supertrend":
                result["supertrend"] = self.indicators.calculate_supertrend(historical, params.get("period", 10), float(params.get("multiplier", 3)))
            elif iid == "macd":
                result["macd"] = self.indicators.calculate_macd(closes, params.get("fast", 12), params.get("slow", 26), params.get("signal", 9))
            elif iid == "rsi":
                result["rsi"] = self.indicators.calculate_rsi(closes, params.get("period", 14))
            elif iid == "adx":
                try:
                    result["adx"] = self.indicators.calculate_adx(
                        historical, int(params.get("period", 14) or 14))
                    result["adx_thr"] = float(params.get("threshold", 25) or 25)
                except Exception:
                    result["adx"] = {}
            elif iid == "ema":
                result["ema"] = self.indicators.calculate_ema(closes, params.get("period", 50))
            elif iid == "predicted_moving_average":
                result["pma"] = self.indicators.calculate_predicted_ma(closes, params.get("lookback", 20))
            elif iid == "predicted_neural_index":
                result["pni"] = self.indicators.calculate_predicted_ma(closes, params.get("lookback", 14))
            elif iid == "neural_network":
                result["neural"] = self.indicators.calculate_neural_network(closes, highs, lows, params.get("period", 14))
            elif iid == "volume_indicator":
                result["volume"] = self.indicators.calculate_volume_indicator(historical, params.get("period", 20))
            elif iid == "open_interest":
                result["oi"] = self.indicators.calculate_oi_indicator(historical, params.get("period", 20))
            elif iid == "kama":
                result["kama"] = self.indicators.calculate_kama(closes, params.get("fast_period", 10), params.get("slow_period", 30))
            elif iid == "hmm_regime":
                result["hmm_regime"] = self.indicators.calculate_hmm_regime(closes, params.get("n_components", 3))
            elif iid == "dynamic_bollinger":
                result["dynamic_boll"] = self.indicators.calculate_dynamic_bollinger(closes, highs, lows, params.get("period", 20), params.get("lookforward", 5))
            elif iid == "ml_rsi":
                result["ml_rsi"] = self.indicators.calculate_ml_rsi(closes, params.get("period", 14))
            elif iid == "ml_signal_filter":
                result["ml_signal"] = self.indicators.calculate_ml_signal_filter(closes, params.get("fast_ema", 9), params.get("slow_ema", 21), params.get("rsi_period", 14), params.get("ml_weight", 0.3))
            elif iid == "vwap":
                try:
                    result["vwap"] = self.indicators.calculate_vwap(historical, params.get("period", 20), float(params.get("multiplier", 2.0)))
                except Exception:
                    result["vwap"] = {}
            elif iid == "heikin_ashi" or iid == "heikin_bullish" or iid == "heikin_bearish":
                try:
                    result["heikin"] = self.indicators.calculate_heikin_ashi(historical)
                    result.setdefault("heikin_ids", set()).add(iid)
                except Exception:
                    result["heikin"] = {}
            elif iid == "range_breakout":
                try:
                    result["rangebo"] = self.indicators.calculate_range_breakout(historical, int(params.get("period", 20) or 20))
                except Exception:
                    result["rangebo"] = {}
            elif iid == "utbot":
                try:
                    result["utbot"] = self.indicators.calculate_utbot(
                        historical, float(params.get("keyvalue", params.get("key_value", 3.0))),
                        int(params.get("atrperiod", params.get("atr_period", 10))))
                except Exception:
                    result["utbot"] = {"stop": [], "pos": []}
            elif iid == "robot_confluence":
                # Call/Put Robot confluence: VWAP + EMA-fast/slow + RSI + Volume
                # breakout combined with AND logic (params all tunable from UI).
                try:
                    result["robot_conf"] = self._calc_robot_confluence(historical, closes, params)
                except Exception:
                    result["robot_conf"] = {"buy": [], "sell": []}
            elif iid == "ema_cross":
                try:
                    result["ema_cross"] = self.indicators.calculate_ema_cross(closes, int(params.get("fast", 9)), int(params.get("slow", 21)))
                except Exception:
                    result["ema_cross"] = {"signal": []}
            elif iid == "bb_squeeze":
                try:
                    result["bb_squeeze"] = self.indicators.calculate_bollinger_squeeze(historical, int(params.get("period", 20)), float(params.get("mult", 2.0)))
                except Exception:
                    result["bb_squeeze"] = {"signal": []}
            elif iid == "comboA_ema_st_rsi":
                try:
                    result["comboA"] = self._calc_comboA_ema_st_rsi(historical, closes, highs, lows, params)
                except Exception:
                    result["comboA"] = {"buy": [], "sell": []}
            elif iid == "comboB_bb_vol":
                try:
                    result["comboB"] = self._calc_comboB_bb_vol(historical, closes, highs, lows, params)
                except Exception:
                    result["comboB"] = {"buy": [], "sell": []}
            elif iid == "opp_combo":
                # Dashboard Opportunity Combo: SuperTrend + MACD + EMA + Volume
                # scoring (same rules as scanner opportunity), min_score gate.
                try:
                    result["opp_combo"] = self._calc_opp_combo(historical, closes, params)
                except Exception:
                    result["opp_combo"] = {"buy": [], "sell": []}
            elif iid == "combo_rsi_vwap_pcr":
                # RSI Momentum + VWAP anchor + SuperTrend + PCR + Volume.
                # Dashboard opportunity + backtest dono me same calc.
                try:
                    result["combo_rvp"] = self._calc_combo_rsi_vwap_pcr(historical, closes, params)
                except Exception:
                    result["combo_rvp"] = {"buy": [], "sell": []}
            elif iid == "market_regime":
                try:
                    result["market_regime"] = self.indicators.calculate_market_regime(
                        historical, closes, params)
                except Exception:
                    result["market_regime"] = {"buy": [], "sell": [], "regime": []}
        return result

    def _calc_opp_combo(self, historical, closes, params):
        """Dashboard Opportunity Combo (scanner parity): SuperTrend breakout 30 /
        above 10, MACD cross 30 / above 10, high volume 20, EMA trend 20.
        Signal when side score >= min_score (default 50).
        Returns {'buy':[...], 'sell':[...]} bool lists."""
        ms = int(params.get("min_score", 50) or 50)
        n = len(closes)
        buy = [False] * n
        sell = [False] * n
        try:
            st = self.indicators.calculate_supertrend(historical, 10, 3.0) or []
            md = self.indicators.calculate_macd(closes, 12, 26, 9) or {}
            m = md.get("macd", []) if isinstance(md, dict) else []
            s = md.get("signal", []) if isinstance(md, dict) else []
            ep = 200 if n >= 200 else 50 if n >= 50 else 20
            ema = self.indicators.calculate_ema(closes, ep) or []
            vols = [float(h.get("volume", 0) or 0) for h in historical]
            for i in range(max(21, 2), n):
                try:
                    c = closes[i]
                    pc = closes[i - 1]
                    vsma = sum(vols[max(0, i - 20):i]) / max(1, min(20, i))
                    bs = 0
                    ss = 0
                    if i < len(st) and st[i]:
                        if c > st[i]:
                            prev_st = st[i - 1] if i - 1 < len(st) else 0
                            bs += 30 if (prev_st and pc <= prev_st) else 10
                        elif c < st[i]:
                            prev_st = st[i - 1] if i - 1 < len(st) else 0
                            ss += 30 if (prev_st and pc >= prev_st) else 10
                    mv = m[i] if i < len(m) else 0
                    sv = s[i] if i < len(s) else 0
                    pmv = m[i - 1] if i - 1 < len(m) else 0
                    psv = s[i - 1] if i - 1 < len(s) else 0
                    if mv and sv:
                        if mv > sv:
                            bs += 30 if (pmv and psv and pmv <= psv) else 10
                        elif mv < sv:
                            ss += 30 if (pmv and psv and pmv >= psv) else 10
                    if vsma > 0:
                        if vols[i] > vsma * 1.5:
                            bs += 20
                        if vols[i] > vsma * 1.2:
                            ss += 20
                    ev = ema[i] if i < len(ema) else 0
                    if ev and c > ev:
                        bs += 20
                    elif ev and c < ev:
                        ss += 20
                    buy[i] = bs >= ms
                    sell[i] = ss >= ms
                except Exception:
                    continue
        except Exception:
            pass
        return {"buy": buy, "sell": sell}

    def _historical_pcr_map(self, symbol, historical):
        """Ek grouped query me poori window ka date->PCR (put OI / call OI).
        OI na ho to {} (calc 1.0 neutral leta hai)."""
        out = {}
        try:
            if getattr(self, "_skip_db", False):
                return out
            if not historical:
                return out
            _s = str((historical[0] or {}).get("trade_date", ""))[:10]
            _e = str((historical[-1] or {}).get("trade_date", ""))[:10]
            if not _s or not _e:
                return out
            from core.models.database import Database
            rows = Database.get_instance().fetch_all(
                "SELECT trade_date, option_type, SUM(COALESCE(oi,0)) as s "
                "FROM bhavcopy_data WHERE symbol=? AND option_type IN ('CE','PE') "
                "AND trade_date BETWEEN ? AND ? GROUP BY trade_date, option_type",
                [symbol, _s, _e])
            _per = {}
            for r in rows or []:
                try:
                    _per.setdefault(str(r.get("trade_date", ""))[:10], {})[str(r.get("option_type") or "")] = float(r.get("s") or 0)
                except Exception:
                    continue
            for _d, _v in _per.items():
                try:
                    _ce = float((_v or {}).get("CE", 0) or 0)
                    _pe = float((_v or {}).get("PE", 0) or 0)
                    if _ce > 0:
                        out[_d] = round(_pe / _ce, 2)
                except Exception:
                    continue
        except Exception:
            pass
        return out

    def _calc_combo_rsi_vwap_pcr(self, historical, closes, params):
        """RSI+VWAP+ST+PCR combo (dashboard parity): PCR map ek query me."""
        try:
            _pcr = self._historical_pcr_map(getattr(self, "bt_symbol", ""), historical)
        except Exception:
            _pcr = {}
        try:
            return self.indicators.calculate_rsi_vwap_pcr_combo(
                historical, closes, params or {}, _pcr)
        except Exception:
            return {"buy": [], "sell": []}

    def _calc_comboA_ema_st_rsi(self, historical, closes, highs, lows, params):
        """Combo A: EMA9/21 + SuperTrend + RSI Momentum (user spec).
        CE Buy: close > 9EMA & 21EMA, 9 crossed above 21, ST Green, RSI 55-65.
        PE Buy: close < 9EMA & 21EMA, 9 crossed below 21, ST Red, RSI <45."""
        ef = int(params.get("ema_fast", 9) or 9); es = int(params.get("ema_slow", 21) or 21)
        sp = int(params.get("st_period", 10) or 10); sm = float(params.get("st_mult", 3.0) or 3.0)
        rp = int(params.get("rsi_period", 14) or 14)
        n = len(closes)
        buy = [False]*n; sell = [False]*n
        try:
            ema_f = self.indicators.calculate_ema(closes, ef) or []
            ema_s = self.indicators.calculate_ema(closes, es) or []
            st = self.indicators.calculate_supertrend(historical, sp, sm) or []
            rsi = self.indicators.calculate_rsi(closes, rp) or []
            for i in range(max(ef, es, sp, rp)+1, n):
                c = closes[i]; efv = ema_f[i] if i < len(ema_f) else None; esv = ema_s[i] if i < len(ema_s) else None
                stv = st[i] if i < len(st) else None; rsi_v = rsi[i] if i < len(rsi) else None
                if efv is None or esv is None or stv is None or rsi_v is None:
                    continue
                prev_ef = ema_f[i-1] if i-1 < len(ema_f) else None; prev_es = ema_s[i-1] if i-1 < len(ema_s) else None
                cross_up = prev_ef is not None and prev_es is not None and prev_ef <= prev_es and efv > esv
                cross_dn = prev_ef is not None and prev_es is not None and prev_ef >= prev_es and efv < esv
                st_green = c > stv; st_red = c < stv
                if c > efv and c > esv and cross_up and st_green and 55 <= rsi_v <= 65:
                    buy[i] = True
                if c < efv and c < esv and cross_dn and st_red and rsi_v < 45:
                    sell[i] = True
        except Exception:
            pass
        return {"buy": buy, "sell": sell}

    def _calc_comboB_bb_vol(self, historical, closes, highs, lows, params):
        """Combo B: Bollinger Squeeze + Volume Spike breakout."""
        per = int(params.get("period", 20) or 20); mult = float(params.get("mult", 2.0) or 2.0)
        n = len(closes)
        buy = [False]*n; sell = [False]*n
        try:
            bb = self.indicators.calculate_bollinger_squeeze(historical, per, mult) or {}
            vols = [float(d.get("volume", 0) or 0) for d in historical]
            up = bb.get("upper", []) or []; lo = bb.get("lower", []) or []
            sq = bb.get("squeeze", []) or []; vs = bb.get("vol_spike", []) or []
            for i in range(per+1, n):
                # Squeeze pichhli bar + breakout/volume is bar (calc parity -
                # breakout bar apni window chaudi karke squeeze khatm kar deta hai)
                if i <= 0 or not sq[i-1] or not vs[i]:
                    continue
                c = closes[i]
                if c > (up[i] or 0) and up[i]:
                    buy[i] = True
                if c < (lo[i] or 0) and lo[i]:
                    sell[i] = True
        except Exception:
            pass
        return {"buy": buy, "sell": sell}

    def _calc_robot_confluence(self, historical, closes, params):
        """BUY CALL = close>VWAP + green candle + EMAfast>EMAslow (not flat)
        + RSI>rsi_buy and rising + volume>vol_mult*avg.
        BUY PUT = mirror. Returns {'buy':[...], 'sell':[...]} bool lists."""
        ef = max(2, int(params.get("ema_fast", 9) or 9))
        es = max(3, int(params.get("ema_slow", 21) or 21))
        rp = max(2, int(params.get("rsi_period", 14) or 14))
        rbuy = float(params.get("rsi_buy", 65) or 65)
        rsell = float(params.get("rsi_sell", 35) or 35)
        vp = max(2, int(params.get("vol_period", 20) or 20))
        vm = float(params.get("vol_mult", 1.5) or 1.5)
        wp = max(2, int(params.get("vwap_period", 20) or 20))
        req_candle = int(params.get("require_candle", 1) or 0)
        ema_f = self.indicators.calculate_ema(closes, ef) or []
        ema_s = self.indicators.calculate_ema(closes, es) or []
        rsi = self.indicators.calculate_rsi(closes, rp) or []
        try:
            vw = self.indicators.calculate_vwap(historical, wp, 2.0).get("vwap", [])
        except Exception:
            vw = []
        vols = [float(h.get("volume", 0) or 0) for h in historical]
        n = len(closes)
        buy = [False] * n
        sell = [False] * n
        start = max(ef, es, rp, vp, wp) + 1
        for i in range(max(start, 1), n):
            try:
                h = historical[i]
                o = float(h.get("open_price", 0) or 0)
                c = float(h.get("close_price", 0) or 0)
                if c <= 0:
                    continue
                green = c > o
                red = c < o
                efv = ema_f[i] if i < len(ema_f) else None
                esv = ema_s[i] if i < len(ema_s) else None
                rsi_v = rsi[i] if i < len(rsi) else None
                rsi_p = rsi[i - 1] if i - 1 >= 0 and i - 1 < len(rsi) else None
                vwap_v = vw[i] if i < len(vw) else None
                if efv is None or esv is None or rsi_v is None or vwap_v is None:
                    continue
                # Clear trend, not flat: EMA separation > 0.05% of close
                sep = abs(efv - esv) / c
                trend_up = efv > esv and sep > 0.0005
                trend_dn = efv < esv and sep > 0.0005
                vavg = sum(vols[max(0, i - vp):i]) / max(min(vp, i), 1)
                vol_ok = vavg > 0 and vols[i] > vm * vavg
                if ((not req_candle or green) and trend_up
                        and rsi_v > rbuy and rsi_p is not None and rsi_v >= rsi_p
                        and c > vwap_v and vol_ok):
                    buy[i] = True
                if ((not req_candle or red) and trend_dn
                        and rsi_v < rsell and rsi_p is not None and rsi_v <= rsi_p
                        and c < vwap_v and vol_ok):
                    sell[i] = True
            except Exception:
                continue
        return {"buy": buy, "sell": sell}

    def _get_indicator_value(self, indicator, i, close, historical, pre_calc):
        val = close
        if indicator == "open":
            val = float(historical[i].get("open_price", close))
        elif indicator == "high":
            val = float(historical[i].get("high_price", close))
        elif indicator == "low":
            val = float(historical[i].get("low_price", close))
        elif indicator == "rsi" and "rsi" in pre_calc:
            _v = pre_calc["rsi"][i] if i < len(pre_calc["rsi"]) else None
            if _v is not None:
                val = float(_v)
        elif indicator == "ema" and "ema" in pre_calc:
            _v = pre_calc["ema"][i] if i < len(pre_calc["ema"]) else None
            if _v is not None:
                val = float(_v)
        elif indicator == "kama" and "kama" in pre_calc:
            _v = pre_calc["kama"][i] if i < len(pre_calc["kama"]) else None
            if _v is not None:
                val = float(_v)
        elif indicator == "predicted_moving_average" and "pma" in pre_calc:
            val = pre_calc["pma"].get("pma", {}).get(i, close)
        elif indicator == "ai_sentiment" and "ai_sentiment" in pre_calc:
            v = pre_calc["ai_sentiment"].get("asi", {}).get(i)
            val = v["sentiment"] if v else 0
        elif indicator == "ai_volatility_range" and "ai_volatility" in pre_calc:
            r = pre_calc["ai_volatility"].get("vol_regime", {}).get(i, "normal")
            val = 80 if r == "high" else (50 if r == "normal" else 20)
        elif indicator == "ai_trend_score" and "ai_trend" in pre_calc:
            val = pre_calc["ai_trend"].get("scores", {}).get(i, 0)
        return val

    def _get_buy_signal(self, i, pre_calc, historical, entry_conditions):
        """Generate buy signal using closed bar to avoid look-ahead bias.
        Signals are based on bar i (just closed); execution occurs on next candle i+1 open.
        When user defines entry_conditions, those take priority (AND logic).
        When no custom conditions, use selected indicators (OR logic).
        """
        close = historical[i]["close_price"]
        prev_close = historical[i - 1]["close_price"] if i > 0 else close
        effective_idx = i

        # PRIORITY 1: Custom entry conditions (AND logic - all must match)
        if entry_conditions:
            cond_met = True
            for c in entry_conditions:
                op = c.get("operator", "")
                val = float(c.get("value", 0) or 0)
                ind = c.get("indicator", "close")
                if not op:
                    continue
                cur_v = self._get_indicator_value(ind, effective_idx, close, historical, pre_calc)
                prev_v = self._get_indicator_value(ind, max(0, effective_idx - 1), prev_close, historical, pre_calc)
                if op == "greater_than":
                    cond_met = cond_met and cur_v > val
                elif op == "less_than":
                    cond_met = cond_met and cur_v < val
                elif op == "crosses_above":
                    cond_met = cond_met and cur_v > val and prev_v <= val
                elif op == "crosses_below":
                    cond_met = cond_met and cur_v < val and prev_v >= val
            return cond_met

        # PRIORITY 2: Selected indicator-based signals - NOW requires composite Score >=80 (like dashboard 4-part)
        # Each indicator contributes to score, need at least 80 to enter (was OR logic causing 12% win)
        modes = getattr(self, "ind_modes", {})
        if not pre_calc:
            return True
        # New combo gates (standalone, before generic score)
        if "ema_cross" in pre_calc:
            ec = pre_calc["ema_cross"]
            if modes.get("ema_cross","both") != "bearish" and effective_idx < len(ec.get("cross_up", [])) and ec["cross_up"][effective_idx]:
                # Also need SuperTrend green + RSI 55-65 (combo A CE Buy)
                # but _get_buy_signal is generic BUY; combo signal already validated in pairs
                # Here just gate EMA cross; SuperTrend/RSI checked via their own ids if selected
                return True
            # For PE Buy (sell side) the cross_down will be handled in _get_sell_signal
        if "bb_squeeze" in pre_calc:
            bs = pre_calc["bb_squeeze"]
            if modes.get("bb_squeeze","both") != "bearish" and effective_idx < len(bs.get("signal", [])) and bs["signal"][effective_idx] == 1:
                return True
        if "comboA" in pre_calc:
            ca = pre_calc["comboA"]
            if modes.get("comboA_ema_st_rsi","both") != "bearish" and effective_idx < len(ca.get("buy", [])) and ca["buy"][effective_idx]:
                return True
        if "comboB" in pre_calc:
            cb = pre_calc["comboB"]
            if modes.get("comboB_bb_vol","both") != "bearish" and effective_idx < len(cb.get("buy", [])) and cb["buy"][effective_idx]:
                return True
        # Dashboard Opportunity Combo: standalone gate (its own min_score)
        if "opp_combo" in pre_calc:
            oc = pre_calc["opp_combo"]
            bl = oc.get("buy", []) if isinstance(oc, dict) else []
            if modes.get("opp_combo", "both") != "bearish" and effective_idx < len(bl) and bl[effective_idx]:
                return True
        # Market Regime FILTER (not a signal): run() me bullish/bearish gate
        # entry_sig ko block karta hai. Yahan vote/standalone trigger nahi -
        # warna filter select karne par trades BADH jate hain (majority math
        # badal jata tha).
        # RSI+VWAP+ST+PCR combo: standalone gate (paancho shartein calc me).
        if "combo_rvp" in pre_calc:
            rc = pre_calc["combo_rvp"]
            bl = rc.get("buy", []) if isinstance(rc, dict) else []
            if modes.get("combo_rsi_vwap_pcr", "both") != "bearish" and effective_idx < len(bl) and bl[effective_idx]:
                return True
        # CONFLUENCE for the 7 UI indicators: every selected one votes,
        # majority must agree. SuperTrend alone no longer fires entries by
        # itself - this is why RSI/volume/period changes now move results.
        # (Combos/EMA/VWAP/KAMA/etc below stay standalone: advanced use.)
        # ABSTAIN rule (spec §6 fix): na-compute-hua indicator VETO nahi karta,
        # vote hi nahi deta. Pehle missing value 0/999 bankar No-vote deti thi
        # (chhoti series par ST akela kabhi pass nahi hota tha = 0 trades bug).
        try:
            _cv = []
            _ch = historical[effective_idx]
            _cc = _ch["close_price"]
            if ("supertrend" in pre_calc and effective_idx < len(pre_calc["supertrend"])
                    and pre_calc["supertrend"][effective_idx] is not None):
                _cv.append(modes.get("supertrend", "both") != "bearish"
                           and _cc > pre_calc["supertrend"][effective_idx])
            if "macd" in pre_calc:
                _mm = pre_calc["macd"]
                _mv = _mm["macd"][effective_idx] if effective_idx < len(_mm["macd"]) else None
                _sv = _mm["signal"][effective_idx] if effective_idx < len(_mm["signal"]) else None
                _pm = _mm["macd"][effective_idx - 1] if effective_idx > 0 and effective_idx - 1 < len(_mm["macd"]) else None
                _ps = _mm["signal"][effective_idx - 1] if effective_idx > 0 and effective_idx - 1 < len(_mm["signal"]) else None
                if _mv is not None and _sv is not None and _pm is not None and _ps is not None:
                    _cv.append(modes.get("macd", "both") != "bearish" and _mv > _sv and _pm <= _ps)
            if ("rsi" in pre_calc and effective_idx < len(pre_calc["rsi"])
                    and pre_calc["rsi"][effective_idx] is not None):
                _cv.append(modes.get("rsi", "both") != "bearish" and pre_calc["rsi"][effective_idx] < 45)
            if "volume" in pre_calc:
                _vs = pre_calc["volume"].get("signal", [])
                if effective_idx < len(_vs) and _vs[effective_idx] is not None:
                    _cv.append(modes.get("volume_indicator", "both") != "bearish"
                               and _vs[effective_idx] == 1)
            if "dynamic_boll" in pre_calc:
                _db = pre_calc["dynamic_boll"]
                _lo = _db.get("lower", [])
                if effective_idx < len(_lo) and _lo[effective_idx] is not None:
                    _cv.append(_cc < _lo[effective_idx])
            if "neural" in pre_calc:
                _ns = pre_calc["neural"].get("signal", [])
                if effective_idx < len(_ns) and _ns[effective_idx] is not None:
                    _cv.append(modes.get("neural_network", "both") != "bearish"
                               and _ns[effective_idx] == 1)
            if "adx" in pre_calc and isinstance(pre_calc["adx"], dict):
                _ad = pre_calc["adx"]
                _av = _ad.get("adx", [])
                _pd = _ad.get("plus_di", [])
                _md = _ad.get("minus_di", [])
                _thr = 25
                try:
                    _thr = float(pre_calc.get("adx_thr", 25) or 25)
                except Exception:
                    pass
                _avv = _av[effective_idx] if effective_idx < len(_av) else None
                _pdv = _pd[effective_idx] if effective_idx < len(_pd) else None
                _mdv = _md[effective_idx] if effective_idx < len(_md) else None
                if _avv is not None and _pdv is not None and _mdv is not None:
                    _cv.append(modes.get("adx", "both") != "bearish"
                               and _avv >= _thr and _pdv > _mdv)
            # Market Regime vote NAHI (pure filter - upar run() gate dekho)
            # Extended votes: har supported indicator vote deta hai (abstain =
            # no vote). Pehle ye 13 ids _cv me the hi nahi aur neeche score
            # gate (score<30 -> False) unhe akela select karne par kabhi fire
            # nahi hone deta tha (ema/vwap/kama/hmm/ml/oi/heikin/rangebo/
            # robot/utbot/pma/pni = hamesha 0 signals = "indicator kaam nahi
            # kar raha"). Single select -> majority-of-1 se fire karega.
            if "ema" in pre_calc and effective_idx < len(pre_calc["ema"]) \
                    and pre_calc["ema"][effective_idx] is not None:
                _cv.append(modes.get("ema", "both") != "bearish"
                           and _cc > pre_calc["ema"][effective_idx])
            if "vwap" in pre_calc:
                _vw = pre_calc["vwap"]
                _vv = (_vw.get("vwap", []) if isinstance(_vw, dict) else [])
                if effective_idx < len(_vv) and _vv[effective_idx] is not None:
                    _cv.append(modes.get("vwap", "both") != "bearish"
                               and _cc > _vv[effective_idx])
            if "kama" in pre_calc and effective_idx < len(pre_calc["kama"]) \
                    and pre_calc["kama"][effective_idx] is not None:
                _cv.append(modes.get("kama", "both") != "bearish"
                           and _cc > pre_calc["kama"][effective_idx])
            if "hmm_regime" in pre_calc:
                _sq = pre_calc["hmm_regime"].get("state_sequence", [])
                if effective_idx < len(_sq) and _sq[effective_idx] is not None:
                    _cv.append(modes.get("hmm_regime", "both") != "bearish"
                               and _sq[effective_idx] == "Bullish")
            if "ml_rsi" in pre_calc:
                _ms = pre_calc["ml_rsi"].get("signal", [])
                if effective_idx < len(_ms) and _ms[effective_idx] is not None:
                    _cv.append(modes.get("ml_rsi", "both") != "bearish"
                               and _ms[effective_idx] == 1)
            if "ml_signal" in pre_calc:
                _mp = pre_calc["ml_signal"].get("probability", [])
                if effective_idx < len(_mp) and _mp[effective_idx] is not None:
                    _cv.append(modes.get("ml_signal_filter", "both") != "bearish"
                               and _mp[effective_idx] > 0.60)
            if "oi" in pre_calc:
                _os = pre_calc["oi"].get("signal", [])
                if effective_idx < len(_os) and _os[effective_idx] is not None:
                    _cv.append(modes.get("open_interest", "both") != "bearish"
                               and _os[effective_idx] == 1)
            if "heikin" in pre_calc:
                _hs = pre_calc["heikin"].get("signal", [])
                _ha_ids = pre_calc.get("heikin_ids", {"heikin_ashi"})
                _use_bull = ("heikin_bullish" in _ha_ids) or ("heikin_ashi" in _ha_ids)
                _blk = modes.get("heikin_bullish", modes.get("heikin_ashi", "both"))
                if effective_idx < len(_hs) and _hs[effective_idx] is not None:
                    _cv.append(_use_bull and _blk != "bearish"
                               and _hs[effective_idx] == 1)
            if "rangebo" in pre_calc:
                _rs = pre_calc["rangebo"].get("signal", [])
                if effective_idx < len(_rs) and _rs[effective_idx] is not None:
                    _cv.append(modes.get("range_breakout", "both") != "bearish"
                               and _rs[effective_idx] == 1)
            if "robot_conf" in pre_calc:
                _rb = pre_calc["robot_conf"].get("buy", [])
                if effective_idx < len(_rb) and _rb[effective_idx] is not None:
                    _cv.append(modes.get("robot_confluence", "both") != "bearish"
                               and bool(_rb[effective_idx]))
            if "utbot" in pre_calc:
                _up = pre_calc["utbot"].get("pos", [])
                if 0 < effective_idx < len(_up) and _up[effective_idx] is not None:
                    _cv.append(modes.get("utbot", "both") != "bearish"
                               and _up[effective_idx] == 1 and _up[effective_idx - 1] != 1)
            if "pma" in pre_calc:
                _pv = pre_calc["pma"].get("pma", [])
                if effective_idx < len(_pv) and _pv[effective_idx] is not None:
                    _cv.append(modes.get("predicted_moving_average", "both") != "bearish"
                               and _cc > _pv[effective_idx])
            if "pni" in pre_calc:
                _pn = pre_calc["pni"].get("pma", [])
                if effective_idx < len(_pn) and _pn[effective_idx] is not None:
                    _cv.append(modes.get("predicted_neural_index", "both") != "bearish"
                               and _cc > _pn[effective_idx])
            if _cv:
                import math as _math
                _need = 1 if len(_cv) == 1 else max(2, _math.ceil(len(_cv) / 2))
                if sum(1 for _v in _cv if _v) >= _need:
                    return True
                return False
        except Exception:
            pass
        # Calculate composite score for this bar (missing values: no _max
        # inflation, no crash - abstain rule yahan bhi).
        _score = 0
        _max = 0
        # Supertrend bullish +30
        if ("supertrend" in pre_calc and effective_idx < len(pre_calc["supertrend"])
                and pre_calc["supertrend"][effective_idx] is not None):
            _max += 30
            if historical[effective_idx]["close_price"] > pre_calc["supertrend"][effective_idx]:
                _score += 30
        # EMA trend +20
        if "ema" in pre_calc and effective_idx < len(pre_calc["ema"]) and pre_calc["ema"][effective_idx] is not None:
            _max += 20
            if historical[effective_idx]["close_price"] > pre_calc["ema"][effective_idx]:
                _score += 20
        # RSI <45 bullish +20, RSI >55 bearish handled in sell
        if ("rsi" in pre_calc and effective_idx < len(pre_calc["rsi"])
                and pre_calc["rsi"][effective_idx] is not None):
            _max += 20
            if pre_calc["rsi"][effective_idx] < 45:
                _score += 20
        # VWAP +10
        if "vwap" in pre_calc:
            vw = pre_calc["vwap"]
            vwap_vals = vw.get("vwap", []) if isinstance(vw, dict) else []
            _max += 10
            if effective_idx < len(vwap_vals) and vwap_vals[effective_idx] is not None and historical[effective_idx]["close_price"] > vwap_vals[effective_idx]:
                _score += 10
        # Require at least 60% of max score
        if _max>0 and _score < _max*0.6:  # e.g. 48/80
            return False
        # Also require at least 2 indicators to agree
        if _score < 30:
            return False
        if ("supertrend" in pre_calc and effective_idx < len(pre_calc["supertrend"])
                and pre_calc["supertrend"][effective_idx] is not None):
            if modes.get("supertrend","both") != "bearish" and historical[effective_idx]["close_price"] > pre_calc["supertrend"][effective_idx]:
                return True
        if "macd" in pre_calc:
            m = pre_calc["macd"]
            mv = m["macd"][effective_idx] if effective_idx < len(m["macd"]) and m["macd"][effective_idx] is not None else 0
            sv = m["signal"][effective_idx] if effective_idx < len(m["signal"]) and m["signal"][effective_idx] is not None else 0
            pm = m["macd"][effective_idx - 1] if effective_idx > 0 and effective_idx - 1 < len(m["macd"]) and m["macd"][effective_idx - 1] is not None else 0
            ps = m["signal"][effective_idx - 1] if effective_idx > 0 and effective_idx - 1 < len(m["signal"]) and m["signal"][effective_idx - 1] is not None else 0
            if modes.get("macd","both") != "bearish" and mv > sv and pm <= ps:
                return True
        if ("rsi" in pre_calc and effective_idx < len(pre_calc["rsi"])
                and pre_calc["rsi"][effective_idx] is not None):
            if modes.get("rsi","both") != "bearish" and pre_calc["rsi"][effective_idx] < 45:
                return True
        if "ema" in pre_calc and effective_idx < len(pre_calc["ema"]) and pre_calc["ema"][effective_idx] is not None:
            if modes.get("ema","both") != "bearish" and historical[effective_idx]["close_price"] > pre_calc["ema"][effective_idx]:
                return True
        if "kama" in pre_calc and effective_idx < len(pre_calc["kama"]):
            kama_v = pre_calc["kama"][effective_idx]
            if modes.get("kama","both") != "bearish" and kama_v is not None and historical[effective_idx]["close_price"] > kama_v:
                return True
        if "hmm_regime" in pre_calc:
            seq = pre_calc["hmm_regime"].get("state_sequence", [])
            if seq and effective_idx < len(seq) and seq[effective_idx] == "Bullish":
                return True
        if "dynamic_boll" in pre_calc:
            db = pre_calc["dynamic_boll"]
            low = db.get("lower", [])
            if effective_idx < len(low) and low[effective_idx] is not None and historical[effective_idx]["close_price"] < low[effective_idx]:
                return True
        if "ml_rsi" in pre_calc:
            ml = pre_calc["ml_rsi"]
            sig = ml.get("signal", [])
            if effective_idx < len(sig) and sig[effective_idx] == 1:
                return True
        if "ml_signal" in pre_calc:
            prob = pre_calc["ml_signal"].get("probability", [])
            if (effective_idx < len(prob) and prob[effective_idx] is not None
                    and prob[effective_idx] > 0.60):
                return True
        # VWAP support
        if "vwap" in pre_calc:
            vw = pre_calc["vwap"]
            vwap_vals = vw.get("vwap", []) if isinstance(vw, dict) else []
            if modes.get("vwap","both") != "bearish" and effective_idx < len(vwap_vals) and vwap_vals[effective_idx] is not None and historical[effective_idx]["close_price"] > vwap_vals[effective_idx]:
                return True
        if "neural" in pre_calc:
            sig = pre_calc["neural"].get("signal", [])
            if modes.get("neural_network","both") != "bearish" and effective_idx < len(sig) and sig[effective_idx] == 1:
                return True
        if "volume" in pre_calc:
            sig = pre_calc["volume"].get("signal", [])
            if modes.get("volume_indicator","both") != "bearish" and effective_idx < len(sig) and sig[effective_idx] == 1:
                return True
        if "oi" in pre_calc:
            sig = pre_calc["oi"].get("signal", [])
            if modes.get("open_interest","both") != "bearish" and effective_idx < len(sig) and sig[effective_idx] == 1:
                return True
        if "heikin" in pre_calc:
            sig = pre_calc["heikin"].get("signal", [])
            ha_ids = pre_calc.get("heikin_ids", {"heikin_ashi"})
            use_bull = ("heikin_bullish" in ha_ids) or ("heikin_ashi" in ha_ids)
            blk = modes.get("heikin_bullish", modes.get("heikin_ashi", "both"))
            if use_bull and blk != "bearish" and effective_idx < len(sig) and sig[effective_idx] == 1:
                return True
        if "rangebo" in pre_calc:
            sig = pre_calc["rangebo"].get("signal", [])
            if modes.get("range_breakout","both") != "bearish" and effective_idx < len(sig) and sig[effective_idx] == 1:
                return True
        if "robot_conf" in pre_calc:
            rc = pre_calc["robot_conf"]
            bl = rc.get("buy", [])
            if modes.get("robot_confluence","both") != "bearish" and effective_idx < len(bl) and bl[effective_idx]:
                return True
        if "utbot" in pre_calc:
            ub = pre_calc["utbot"]
            pl = ub.get("pos", [])
            if modes.get("utbot","both") != "bearish" and 0 < effective_idx < len(pl) and pl[effective_idx] == 1 and pl[effective_idx - 1] != 1:
                return True
        return False

    def _get_sell_signal(self, i, pre_calc, historical, exit_conditions):
        """Generate sell signal using closed bar to avoid look-ahead bias.
        When user defines exit_conditions, those take priority (AND logic).
        When no custom conditions, use selected indicators (OR logic).
        """
        close = historical[i]["close_price"]
        prev_close = historical[i - 1]["close_price"] if i > 0 else close
        effective_idx = i

        # PRIORITY 1: Custom exit conditions (AND logic)
        if exit_conditions:
            cond_met = True
            for c in exit_conditions:
                op = c.get("operator", "")
                val = float(c.get("value", 0) or 0)
                ind = c.get("indicator", "close")
                if not op:
                    continue
                cur_v = self._get_indicator_value(ind, effective_idx, close, historical, pre_calc)
                prev_v = self._get_indicator_value(ind, max(0, effective_idx - 1), prev_close, historical, pre_calc)
                if op == "greater_than":
                    cond_met = cond_met and cur_v > val
                elif op == "less_than":
                    cond_met = cond_met and cur_v < val
                elif op == "crosses_above":
                    cond_met = cond_met and cur_v > val and prev_v <= val
                elif op == "crosses_below":
                    cond_met = cond_met and cur_v < val and prev_v >= val
            return cond_met

        # PRIORITY 2: Selected indicator-based signals - composite Score >=80
        modes = getattr(self, "ind_modes", {})
        if "ema_cross" in pre_calc:
            ec = pre_calc["ema_cross"]
            if modes.get("ema_cross","both") != "bullish" and effective_idx < len(ec.get("cross_down", [])) and ec["cross_down"][effective_idx]:
                return True
        if "bb_squeeze" in pre_calc:
            bs = pre_calc["bb_squeeze"]
            if modes.get("bb_squeeze","both") != "bullish" and effective_idx < len(bs.get("signal", [])) and bs["signal"][effective_idx] == -1:
                return True
        if "comboA" in pre_calc:
            ca = pre_calc["comboA"]
            if modes.get("comboA_ema_st_rsi","both") != "bullish" and effective_idx < len(ca.get("sell", [])) and ca["sell"][effective_idx]:
                return True
        if "comboB" in pre_calc:
            cb = pre_calc["comboB"]
            if modes.get("comboB_bb_vol","both") != "bullish" and effective_idx < len(cb.get("sell", [])) and cb["sell"][effective_idx]:
                return True
        # Dashboard Opportunity Combo: standalone gate (its own min_score)
        if "opp_combo" in pre_calc:
            oc = pre_calc["opp_combo"]
            sl2 = oc.get("sell", []) if isinstance(oc, dict) else []
            if modes.get("opp_combo", "both") != "bullish" and effective_idx < len(sl2) and sl2[effective_idx]:
                return True
        # Market Regime FILTER (sell side): standalone trigger nahi (upar run()
        # gate bearish/bullish mismatch par entry block karta hai).
        # RSI+VWAP+ST+PCR combo: standalone gate (mirror).
        if "combo_rvp" in pre_calc:
            rc = pre_calc["combo_rvp"]
            sl2 = rc.get("sell", []) if isinstance(rc, dict) else []
            if modes.get("combo_rsi_vwap_pcr", "both") != "bullish" and effective_idx < len(sl2) and sl2[effective_idx]:
                return True
        # CONFLUENCE mirror (sell): majority of COMPUTABLE votes must agree.
        # Abstain rule (buy side jaisa): missing value = no vote, never veto.
        try:
            _cv = []
            _ch = historical[effective_idx]
            _cc = _ch["close_price"]
            if ("supertrend" in pre_calc and effective_idx < len(pre_calc["supertrend"])
                    and pre_calc["supertrend"][effective_idx] is not None):
                _cv.append(modes.get("supertrend", "both") != "bullish"
                           and _cc < pre_calc["supertrend"][effective_idx])
            if "macd" in pre_calc:
                _mm = pre_calc["macd"]
                _mv = _mm["macd"][effective_idx] if effective_idx < len(_mm["macd"]) else None
                _sv = _mm["signal"][effective_idx] if effective_idx < len(_mm["signal"]) else None
                _pm = _mm["macd"][effective_idx - 1] if effective_idx > 0 and effective_idx - 1 < len(_mm["macd"]) else None
                _ps = _mm["signal"][effective_idx - 1] if effective_idx > 0 and effective_idx - 1 < len(_mm["signal"]) else None
                if _mv is not None and _sv is not None and _pm is not None and _ps is not None:
                    _cv.append(modes.get("macd", "both") != "bullish" and _mv < _sv and _pm >= _ps)
            if ("rsi" in pre_calc and effective_idx < len(pre_calc["rsi"])
                    and pre_calc["rsi"][effective_idx] is not None):
                _cv.append(modes.get("rsi", "both") != "bullish" and pre_calc["rsi"][effective_idx] > 55)
            if "volume" in pre_calc:
                _vs = pre_calc["volume"].get("signal", [])
                if effective_idx < len(_vs) and _vs[effective_idx] is not None:
                    _cv.append(modes.get("volume_indicator", "both") != "bullish"
                               and _vs[effective_idx] == -1)
            if "dynamic_boll" in pre_calc:
                _db = pre_calc["dynamic_boll"]
                _up = _db.get("upper", [])
                if effective_idx < len(_up) and _up[effective_idx] is not None:
                    _cv.append(_cc > _up[effective_idx])
            if "neural" in pre_calc:
                _ns = pre_calc["neural"].get("signal", [])
                if effective_idx < len(_ns) and _ns[effective_idx] is not None:
                    _cv.append(modes.get("neural_network", "both") != "bullish"
                               and _ns[effective_idx] == -1)
            if "adx" in pre_calc and isinstance(pre_calc["adx"], dict):
                _ad = pre_calc["adx"]
                _av = _ad.get("adx", [])
                _pd = _ad.get("plus_di", [])
                _md = _ad.get("minus_di", [])
                _thr = 25
                try:
                    _thr = float(pre_calc.get("adx_thr", 25) or 25)
                except Exception:
                    pass
                _avv = _av[effective_idx] if effective_idx < len(_av) else None
                _pdv = _pd[effective_idx] if effective_idx < len(_pd) else None
                _mdv = _md[effective_idx] if effective_idx < len(_md) else None
                if _avv is not None and _pdv is not None and _mdv is not None:
                    _cv.append(modes.get("adx", "both") != "bullish"
                               and _avv >= _thr and _mdv > _pdv)
            # Market Regime vote NAHI (sell side bhi pure filter - run() gate)
            # Extended votes (buy-side mirror): single-select ye ids bhi fire
            # karein. Pehle sell-side me ye votes missing the (score gate ke
            # neeche standalone tak pahunchte hi nahi the) -> PE legs par ye
            # indicators kabhi signal nahi dete the.
            if "ema" in pre_calc and effective_idx < len(pre_calc["ema"]) \
                    and pre_calc["ema"][effective_idx] is not None:
                _cv.append(modes.get("ema", "both") != "bullish"
                           and _cc < pre_calc["ema"][effective_idx])
            if "vwap" in pre_calc:
                _vw = pre_calc["vwap"]
                _vv = (_vw.get("vwap", []) if isinstance(_vw, dict) else [])
                if effective_idx < len(_vv) and _vv[effective_idx] is not None:
                    _cv.append(modes.get("vwap", "both") != "bullish"
                               and _cc < _vv[effective_idx])
            if "kama" in pre_calc and effective_idx < len(pre_calc["kama"]) \
                    and pre_calc["kama"][effective_idx] is not None:
                _cv.append(modes.get("kama", "both") != "bullish"
                           and _cc < pre_calc["kama"][effective_idx])
            if "hmm_regime" in pre_calc:
                _sq = pre_calc["hmm_regime"].get("state_sequence", [])
                if effective_idx < len(_sq) and _sq[effective_idx] is not None:
                    _cv.append(modes.get("hmm_regime", "both") != "bullish"
                               and _sq[effective_idx] == "Bearish")
            if "ml_rsi" in pre_calc:
                _ms = pre_calc["ml_rsi"].get("signal", [])
                if effective_idx < len(_ms) and _ms[effective_idx] is not None:
                    _cv.append(modes.get("ml_rsi", "both") != "bullish"
                               and _ms[effective_idx] == -1)
            if "ml_signal" in pre_calc:
                _mp = pre_calc["ml_signal"].get("probability", [])
                if effective_idx < len(_mp) and _mp[effective_idx] is not None:
                    _cv.append(modes.get("ml_signal_filter", "both") != "bullish"
                               and _mp[effective_idx] < 0.40)
            if "oi" in pre_calc:
                _os = pre_calc["oi"].get("signal", [])
                if effective_idx < len(_os) and _os[effective_idx] is not None:
                    _cv.append(modes.get("open_interest", "both") != "bullish"
                               and _os[effective_idx] == -1)
            if "heikin" in pre_calc:
                _hs = pre_calc["heikin"].get("signal", [])
                _ha_ids = pre_calc.get("heikin_ids", {"heikin_ashi"})
                _use_bear = ("heikin_bearish" in _ha_ids) or ("heikin_ashi" in _ha_ids)
                _blk = modes.get("heikin_bearish", modes.get("heikin_ashi", "both"))
                if effective_idx < len(_hs) and _hs[effective_idx] is not None:
                    _cv.append(_use_bear and _blk != "bullish"
                               and _hs[effective_idx] == -1)
            if "rangebo" in pre_calc:
                _rs = pre_calc["rangebo"].get("signal", [])
                if effective_idx < len(_rs) and _rs[effective_idx] is not None:
                    _cv.append(modes.get("range_breakout", "both") != "bullish"
                               and _rs[effective_idx] == -1)
            if "robot_conf" in pre_calc:
                _rb = pre_calc["robot_conf"].get("sell", [])
                if effective_idx < len(_rb) and _rb[effective_idx] is not None:
                    _cv.append(modes.get("robot_confluence", "both") != "bullish"
                               and bool(_rb[effective_idx]))
            if "utbot" in pre_calc:
                _up = pre_calc["utbot"].get("pos", [])
                if 0 < effective_idx < len(_up) and _up[effective_idx] is not None:
                    _cv.append(modes.get("utbot", "both") != "bullish"
                               and _up[effective_idx] == -1 and _up[effective_idx - 1] != -1)
            if "pma" in pre_calc:
                _pv = pre_calc["pma"].get("pma", [])
                if effective_idx < len(_pv) and _pv[effective_idx] is not None:
                    _cv.append(modes.get("predicted_moving_average", "both") != "bullish"
                               and _cc < _pv[effective_idx])
            if "pni" in pre_calc:
                _pn = pre_calc["pni"].get("pma", [])
                if effective_idx < len(_pn) and _pn[effective_idx] is not None:
                    _cv.append(modes.get("predicted_neural_index", "both") != "bullish"
                               and _cc < _pn[effective_idx])
            if _cv:
                import math as _math
                _need = 1 if len(_cv) == 1 else max(2, _math.ceil(len(_cv) / 2))
                if sum(1 for _v in _cv if _v) >= _need:
                    return True
                return False
        except Exception:
            pass
        _score = 0
        _max = 0
        if ("supertrend" in pre_calc and effective_idx < len(pre_calc["supertrend"])
                and pre_calc["supertrend"][effective_idx] is not None):
            _max += 30
            if historical[effective_idx]["close_price"] < pre_calc["supertrend"][effective_idx]:
                _score += 30
        if "ema" in pre_calc and effective_idx < len(pre_calc["ema"]) and pre_calc["ema"][effective_idx] is not None:
            _max += 20
            if historical[effective_idx]["close_price"] < pre_calc["ema"][effective_idx]:
                _score += 20
        if ("rsi" in pre_calc and effective_idx < len(pre_calc["rsi"])
                and pre_calc["rsi"][effective_idx] is not None):
            _max += 20
            if pre_calc["rsi"][effective_idx] > 55:
                _score += 20
        if "vwap" in pre_calc:
            vw = pre_calc["vwap"]
            vwap_vals = vw.get("vwap", []) if isinstance(vw, dict) else []
            _max += 10
            if effective_idx < len(vwap_vals) and vwap_vals[effective_idx] is not None and historical[effective_idx]["close_price"] < vwap_vals[effective_idx]:
                _score += 10
        if _max>0 and _score < _max*0.6:
            return False
        if _score < 30:
            return False
        sell = False
        if ("supertrend" in pre_calc and effective_idx < len(pre_calc["supertrend"])
                and pre_calc["supertrend"][effective_idx] is not None):
            if modes.get("supertrend","both") != "bullish":
                sell = sell or (historical[effective_idx]["close_price"] < pre_calc["supertrend"][effective_idx])
        if ("rsi" in pre_calc and effective_idx < len(pre_calc["rsi"])
                and pre_calc["rsi"][effective_idx] is not None):
            if modes.get("rsi","both") != "bullish":
                sell = sell or (pre_calc["rsi"][effective_idx] > 55)
        if "ema" in pre_calc and effective_idx < len(pre_calc["ema"]) and pre_calc["ema"][effective_idx] is not None:
            if modes.get("ema","both") != "bullish":
                sell = sell or (historical[effective_idx]["close_price"] < pre_calc["ema"][effective_idx])
        if "kama" in pre_calc and effective_idx < len(pre_calc["kama"]):
            kama_v = pre_calc["kama"][effective_idx]
            if kama_v is not None and modes.get("kama","both") != "bullish":
                sell = sell or (historical[effective_idx]["close_price"] < kama_v)
        if "hmm_regime" in pre_calc:
            seq = pre_calc["hmm_regime"].get("state_sequence", [])
            if effective_idx < len(seq) and seq[effective_idx] == "Bearish":
                sell = sell or True
        if "dynamic_boll" in pre_calc:
            db = pre_calc["dynamic_boll"]
            upper = db.get("upper", [])
            if effective_idx < len(upper) and upper[effective_idx] is not None:
                sell = sell or (historical[effective_idx]["close_price"] > upper[effective_idx])
        if "ml_rsi" in pre_calc:
            ml = pre_calc["ml_rsi"]
            sig = ml.get("signal", [])
            if effective_idx < len(sig) and sig[effective_idx] == -1:
                sell = sell or True
        if "ml_signal" in pre_calc:
            prob = pre_calc["ml_signal"].get("probability", [])
            if (effective_idx < len(prob) and prob[effective_idx] is not None
                    and prob[effective_idx] < 0.40):
                sell = sell or True
        if "neural" in pre_calc:
            sig = pre_calc["neural"].get("signal", [])
            if modes.get("neural_network","both") != "bullish" and effective_idx < len(sig) and sig[effective_idx] == -1:
                sell = sell or True
        if "volume" in pre_calc:
            sig = pre_calc["volume"].get("signal", [])
            if modes.get("volume_indicator","both") != "bullish" and effective_idx < len(sig) and sig[effective_idx] == -1:
                sell = sell or True
        if "oi" in pre_calc:
            sig = pre_calc["oi"].get("signal", [])
            if modes.get("open_interest","both") != "bullish" and effective_idx < len(sig) and sig[effective_idx] == -1:
                sell = sell or True
        if "heikin" in pre_calc:
            sig = pre_calc["heikin"].get("signal", [])
            ha_ids = pre_calc.get("heikin_ids", {"heikin_ashi"})
            use_bear = ("heikin_bearish" in ha_ids) or ("heikin_ashi" in ha_ids)
            blk = modes.get("heikin_bearish", modes.get("heikin_ashi", "both"))
            if use_bear and blk != "bullish" and effective_idx < len(sig) and sig[effective_idx] == -1:
                sell = sell or True
        if "rangebo" in pre_calc:
            sig = pre_calc["rangebo"].get("signal", [])
            if modes.get("range_breakout","both") != "bullish" and effective_idx < len(sig) and sig[effective_idx] == -1:
                sell = sell or True
        if "robot_conf" in pre_calc:
            rc = pre_calc["robot_conf"]
            sl2 = rc.get("sell", [])
            if modes.get("robot_confluence","both") != "bullish" and effective_idx < len(sl2) and sl2[effective_idx]:
                sell = sell or True
        if "utbot" in pre_calc:
            ub = pre_calc["utbot"]
            pl = ub.get("pos", [])
            if modes.get("utbot","both") != "bullish" and 0 < effective_idx < len(pl) and pl[effective_idx] == -1 and pl[effective_idx - 1] != -1:
                sell = sell or True
        if not pre_calc:
            sell = True
        return sell

    def _select_strike(self, spot, symbol, option_type, strike_sel, delta_target, otm_dist):
        step = get_strike_step(symbol)
        atm = round(spot / step) * step
        if strike_sel == "delta" and delta_target is not None:
            return self._find_delta_strike(spot, symbol, option_type, float(delta_target))
        offset = otm_dist * step
        if strike_sel == "itm":
            offset = -offset if option_type == "CE" else offset
        elif strike_sel == "otm":
            offset = offset if option_type == "CE" else -offset
        else:
            offset = 0
        return align_strike_price(symbol, atm + offset)

    def _enter_single(self, date, spot, symbol, leg, strike_sel, delta_target, otm_dist):
        option_type = leg.get("option_type", "CE")
        txn_type = leg.get("transaction", "buy").lower()
        lots = int(leg.get("lots", 1))
        # EQ (equity spot) legs: price IS the underlying (no strike/premium);
        # qty = share count (lots field), equity cash costs, intraday-aware.
        if str(option_type).upper() == "EQ":
            qty = max(1, lots)
            premium = TransactionCosts.apply_fill_slippage(float(spot or 0), "BUY" if txn_type == "buy" else "SELL", self.is_live,
                                                           slippage_pct=float(getattr(self, "_slippage_pct", 0.5) or 0.5))
            premium = self._apply_latency(premium, "BUY" if txn_type == "buy" else "SELL")
            costs = TransactionCosts.calculate_equity(premium * qty, txn_type == "sell", getattr(self, "_eq_intraday", True))
            entry_time = getattr(self, '_entry_time', '09:35')
            entry_clock = getattr(self, '_cur_bar_time', '') or entry_time
            # Numeric strike (= entry spot): float(entry["strike"]) call sites
            # across exits/MTM stay crash-free; display shows the level.
            return {
                "date": date, "strike": round(float(spot or 0), 2), "price": round(premium, 2),
                "quantity": qty, "costs": costs, "time": entry_clock,
                "total_cost": round(premium * qty + costs["total"], 2),
                "type": txn_type, "legs": [{"option_type": "EQ", "strike": round(float(spot or 0), 2), "type": txn_type, "lots": lots}],
            }
        qty = lots * get_lot_size(symbol)
        strike = self._select_strike(spot, symbol, option_type, strike_sel, delta_target, otm_dist)
        premium = self._entry_premium(date, spot, strike, option_type)
        # NO synthetic floor: if real tick/chain data has no price for this
        # exact contract, skip the trade honestly (never fabricate ₹1.5).
        if premium is None or float(premium) <= 0:
            self._rej = getattr(self, "_rej", None) or {}
            self._rej["no_real_option_premium"] = self._rej.get("no_real_option_premium", 0) + 1
            return None
        # Dynamic Slippage for Option Selling: SELL entry पर प्राइस और खराब (कम) करें
        # ताकि रियल वर्ल्ड बिड-आस्क स्प्रेड का असर पड़े और बैकटेस्ट लाइव से मिले
        # One shared slippage application only (paper entry does the same).
        # Old code applied _slippage_pct here AND again in
        # apply_fill_slippage -> backtest sells filled 2x worse than paper.
        premium = TransactionCosts.apply_fill_slippage(premium, "BUY" if txn_type == "buy" else "SELL", self.is_live,
                                                       slippage_pct=float(getattr(self, "_slippage_pct", 0.5) or 0.5))
        premium = self._apply_latency(premium, "BUY" if txn_type == "buy" else "SELL")
        premium = max(float(premium), 0.05)
        # TICK SIZE 0.05 (rulebook): fill price exchange tick par snap.
        try:
            from core.services.strategy_rulebook import round_tick as _rtick
            premium = _rtick(premium)
        except Exception:
            pass
        costs = TransactionCosts.calculate(premium * qty, txn_type == "sell", self.is_live)
        entry_time = getattr(self, '_entry_time', '09:35')
        # Real signal-bar time when known (intraday); daily bars honestly
        # keep the configured session entry time.
        entry_clock = getattr(self, '_cur_bar_time', '') or entry_time
        try:
            _sig_inds = list(getattr(self, "_ind_ids", []) or [])
        except Exception:
            _sig_inds = []
        return {
            "date": date, "strike": str(strike), "price": round(premium, 2),
            "quantity": qty, "costs": costs, "time": entry_clock,
            "total_cost": round(premium * qty + costs["total"], 2),
            "type": txn_type, "legs": [{"option_type": option_type, "strike": strike, "type": txn_type, "lots": lots}],
            "expiry": str(getattr(self, "bt_expiry", "") or ""),
            "prem_src": self._prem_source(date, strike, option_type),
            "signal": {"side": txn_type, "indicators": _sig_inds},
        }

    def _enter_spread(self, date, spot, symbol, legs, strike_sel, delta_target, otm_dist):
        step = get_strike_step(symbol)
        atm = round(spot / step) * step
        leg_details = []
        total_cost = 0.0
        qty = 0
        for idx, leg in enumerate(legs):
            option_type = leg.get("option_type", leg.get("optType", "CE"))
            # Support both 'transaction' and frontend 'position'
            txn_type = leg.get("transaction", leg.get("position", "buy"))
            txn_type = str(txn_type).lower()
            if txn_type not in ("buy", "sell"):
                txn_type = "buy"
            lots = int(leg.get("lots", 1) or 1)
            lqty = lots * get_lot_size(symbol)
            # Support both 'strike_selection' and frontend 'strike_type'
            leg_sel_raw = leg.get("strike_selection", leg.get("strike_type", strike_sel))
            leg_sel = str(leg_sel_raw).lower() if isinstance(leg_sel_raw, str) else strike_sel
            if leg_sel not in ("atm", "otm", "itm", "delta"):
                leg_sel = strike_sel
            leg_delta = leg.get("delta_target", delta_target)
            # otm_distance handling; for spread ensure legs have distinct strikes
            if leg.get("otm_distance") is not None:
                leg_otm = int(leg.get("otm_distance") or 0)
            elif leg.get("otmDistance") is not None:
                leg_otm = int(leg.get("otmDistance") or 0)
            else:
                leg_otm = int(otm_dist or 2)
                # Auto widen spread: second leg further OTM by 2 steps, 4-leg iron condor by pattern
                if len(legs) == 2 and idx == 1:
                    leg_otm = leg_otm + 2
                elif len(legs) == 4 and idx in (1, 3):
                    leg_otm = leg_otm + 2
            if "offset" in leg and leg.get("offset") not in (None, "") and leg.get("strike_selection") is None and leg.get("strike_type") is None:
                strike = atm + int(leg.get("offset", 0) or 0) * step
            else:
                strike = self._select_strike(spot, symbol, option_type, leg_sel, leg_delta, leg_otm)
            premium = self._entry_premium(date, spot, strike, option_type)
            # NO synthetic floor: single-leg ki tarah spread me bhi kisi leg ka
            # asli premium na mile to poori trade skip karo (never fabricate).
            if premium is None or float(premium) <= 0:
                self._rej["no_real_option_premium"] = self._rej.get("no_real_option_premium", 0) + 1
                return None
            # Dynamic Slippage for Option Selling: SELL leg पर प्राइस और खराब (कम) करें
            # One shared slippage application only (parity with paper entry).
            premium = TransactionCosts.apply_fill_slippage(premium, "BUY" if txn_type == "buy" else "SELL", self.is_live,
                                                           slippage_pct=float(getattr(self, "_slippage_pct", 0.5) or 0.5))
            premium = max(premium, 1.5)
            costs = TransactionCosts.calculate(premium * lqty, txn_type == "sell", self.is_live)
            # Correct sign: sell = +premium (credit received), buy = -premium (debit paid)
            signed = premium * lqty if txn_type == "sell" else -premium * lqty
            total_cost += signed
            qty += lqty
            leg_details.append({
                "option_type": option_type, "strike": strike, "type": txn_type,
                "lots": lots, "quantity": lqty, "premium": round(premium, 2),
                "costs": costs, "signed_value": round(signed, 2),
            })
        net_premium = abs(total_cost)
        entry_clock = getattr(self, '_cur_bar_time', '') or getattr(self, '_entry_time', '09:35')
        return {
            "date": date, "strike": str(leg_details[0]["strike"]), "price": round(net_premium, 2),
            "quantity": qty, "costs": {"total": sum(l["costs"]["total"] for l in leg_details), **leg_details[0]["costs"]},
            "total_cost": round(total_cost + sum(l["costs"]["total"] for l in leg_details), 2),
            "type": "spread", "legs": leg_details,
            "is_spread": True, "time": entry_clock,
        }

    def _find_delta_strike(self, spot, symbol, option_type, target_delta):
        """Resolve a delta-target strike from the REAL option chain's own
        delta column (zero-copy Parquet lakehouse). No BS delta and no model
        IV: if real delta is not recorded, stay at ATM instead of guessing."""
        step = get_strike_step(symbol)
        atm = round(spot / step) * step
        try:
            from core.services.option_lakehouse import query_option_ticks
            from datetime import datetime, timedelta, timezone
            _ist = timezone(timedelta(hours=5, minutes=30))
            _d = str(getattr(self, "_cur_bar_date", "") or datetime.now(_ist).strftime("%Y-%m-%d"))[:10]
            rows = query_option_ticks(
                underlying=symbol,
                data_mode="1m_chain" if self._intraday_bar() else "tick",
                expiry=str(getattr(self, "bt_expiry", "") or ""),
                start_date=_d, end_date=_d, option_type=option_type,
                limit=5000,
            )
            if rows is None or len(rows) == 0:
                return atm
            best, best_diff = atm, 1.0
            for r in rows.to_dicts():
                d = float(r.get("delta") or 0)
                if d == 0:
                    continue
                diff = abs(abs(d) - abs(float(target_delta or 0)))
                if diff < best_diff:
                    best_diff, best = diff, float(r.get("strike") or atm)
            return best
        except Exception:
            return atm

    def _resolve_sltp(self, leg: dict, risk_management: dict):
        """SL/TP rupees YA percent-of-premium ('2%' / *_pct key) - ek jagah
        parse, `_check_sl_tp` ko unit milti hai (paper SL monitor wahi unit
        trade par store karta hai). Returns (sl_amt, tp_amt, sl_unit, tp_unit).

        REALITY FIX: ab SINGLE shared resolver (utils.helpers.resolve_strategy_
        sltp) use hota hai - backtest aur paper worker dono EXACT same rule
        chalate hain (pct -> rs -> leg -> default Rs1500/3000). Pehle backtest
        me '0 = disabled' + default Rs500/1000 tha jabki paper me '0 = unset'
        + Rs1500/3000 - matlab backtest ka target kabhi hit nahi hota tha
        jabki paper me target tha (exit distribution alag = win-rate alag)."""
        try:
            from utils.helpers import resolve_strategy_sltp as _res
            # symbol diya - tabhi per-index tight defaults (rulebook) lagte
            # hain (NIFTY 15/20, FINNIFTY 18/22, BANKNIFTY 25/35 points).
            sl_amt, sl_unit, tp_amt, tp_unit = _res(
                risk_management, leg, symbol=getattr(self, "bt_symbol", "") or None)
            return sl_amt, tp_amt, sl_unit, tp_unit
        except Exception:
            return 0.0, 0.0, "rs", "rs"

    def _apply_latency(self, price, side, pct=None):
        """Signal -> order -> fill latency ka price cost.

        Live me signal ke baad broker tak jaane, order ack aur fill tak
        me jo hota hai uska cost backtest me count nahi hota tha - wahi gap
        backtest ko paper se better dikhata tha.

        pct=None -> entry latency (`_latency_pct`). Exit call sites apna
        `_latency_pct_exit` pass karte hain."""
        try:
            pct = float(getattr(self, "_latency_pct", 0) if pct is None else pct) or 0
            p = float(price or 0)
            if pct <= 0 or p <= 0:
                return p
            amt = p * pct / 100.0
            return (p + amt) if str(side or "BUY").upper() == "BUY" else max(p - amt, 0.05)
        except Exception:
            return price

    def _confirm_tp(self, hit, txn_type, tp_level, opt_close):
        """Strict target confirmation (v7).

        v6 tak target tabhi book hota tha jab candle ka high/low level ko
        CHHOo jaye (wick touch). Real paper monitor aise touch par exit nahi
        karta (bad-tick/spike guard + confirm re-quote), aur wick-based
        booking hi 80% backtest win-rate ka bada source thi: liquid option me
        ek 5-min wick target cross karke wapas aa jata hai.

        Ab default: TP sirf tab jab COMPLETED candle ka CLOSE level ke paar
        ho (jis disha me profit hai). SL waisa hi rehta hai - extreme par
        worst-case fill (koi optimism nahi).

        `advanced.tp_fill_mode="touch"` se purana behavior wapas milta hai."""
        try:
            if not hit or str(hit.get("reason") or "") != "target":
                return hit
            if str(getattr(self, "_tp_fill_mode", "close") or "close") != "close":
                self._n_tp_confirmed += 1
                return hit
            try:
                oc = float(opt_close or 0)
                lvl = float(tp_level or 0)
            except Exception:
                oc, lvl = 0.0, 0.0
            ok = False
            if oc > 0 and lvl > 0:
                ok = (oc >= lvl) if str(txn_type or "buy").lower() == "buy" else (oc <= lvl)
            if ok:
                self._n_tp_confirmed += 1
                return hit
            self._n_tp_unconfirmed += 1
            return None
        except Exception:
            return hit

    def _trail_update(self, entry_idx, bar_high, bar_low, txn_type):
        """Record the bar's extreme for NEXT-bar trailing. Called at the END of
        each bar's check, so a bar never trails its own stop with the extreme it
        also uses to test the stop. BUY trails the highest high; SELL trails the
        lowest low."""
        if entry_idx is None:
            return
        try:
            prev = self._trailing_best.get(entry_idx)
            if (txn_type or "").lower() == "buy":
                cand = float(bar_high or 0)
                if cand > 0:
                    self._trailing_best[entry_idx] = cand if prev is None or cand > prev else prev
            else:
                cand = float(bar_low or 0)
                if cand > 0:
                    self._trailing_best[entry_idx] = cand if prev is None or cand < prev else prev
        except Exception:
            pass

    def _check_sl_tp(self, current, entry, option_type, txn_type, sl_amt, tp_amt, qty=0, entry_idx=None):
        entry_price = float(entry["price"])
        qty = int(qty or entry.get("quantity", 0) or 0)
        if qty <= 0:
            qty = 1
        # Overall SL/TP from UI are RUPEES per trade (e.g. 1500) YA premium ka
        # PERCENT ('2%'). Convert to premium level via shared helper.
        # Old bug: (sl_amt / entry_price)*100 treated Rs as % of premium, giving 3000%
        # levels that never hit, so Sell trades bled -23k instead of -1500 exit.
        # Shared helper (utils.helpers.sltp_premium_level): paper SL monitor
        # bilkul wahi level par kat-ta hai (parity). qty yahan TOTAL units
        # hai (lots x lot), isliye lot_size=1.
        from utils.helpers import sltp_premium_level as _lvl

        def _to_level(amt, is_sl):
            return _lvl(entry_price, amt, qty, 1, txn_type, is_sl,
                        unit=(getattr(self, "_sl_unit", None) if is_sl
                              else getattr(self, "_tp_unit", None)))

        sl_level = _to_level(sl_amt, True)
        tp_level = _to_level(tp_amt, False)
        # Trailing label audit (user 25-11): initial SL level yaad rakho -
        # agar trailing ne SL badala aur yahi SL hit hua to exit reason
        # 'stoploss' ki jagah 'trailing_lock' dikhao (sahi label).
        self._sl_initial_level = sl_level
        self._last_sl_trailed = False
        # TICK SIZE 0.05 (rulebook): SL/TP levels bhi exchange tick par -
        # paper/live monitor ke tick levels se exactly match karte hain.
        try:
            from core.services.strategy_rulebook import round_tick as _rtick_lvl
            sl_level = _rtick_lvl(sl_level) if sl_level > 0 else sl_level
            tp_level = _rtick_lvl(tp_level) if tp_level > 0 else tp_level
        except Exception:
            pass
        # NOTE (25-11): initial SL/TP direction already enforced by the single
        # shared helper sltp_premium_level() (BUY: SL=entry-pts, TP=entry+pts;
        # SELL: SL=entry+pts, TP=entry-pts). Yahan dobara flip mat karo -
        # trailing lock BUY ka SL entry se upar le ja sakta hai (sahi behavior).

        # ---- Trailing Stop: lock in profits as price moves in favor ----
        # Causal: the trailed stop for bar i uses the best price reached on
        # CLOSED bars only. (Before this, the bar's own extreme raised the
        # stop, then the same bar's opposite extreme was tested against the
        # raised stop = within-bar look-ahead; plus trailing="Lock" hit an
        # undefined trail_dist -> NameError -> whole backtest errored.)
        # ---- TRAILING LOCK % (user rule): profit >= lock_profit_at% par SL
        # cost-price (breakeven) par; lock_profit% diya ho to us profit ko
        # LOCK (SL = entry + lock%). Ye pehle UI se bheja jaata tha par
        # engine IGNORE karta tha (trailing kaam hi nahi karta tha).
        _lock_at = 0.0
        _lock_amt = 0.0
        _lock_unit = "pct"
        try:
            _adv_t = getattr(self, "_advanced", {}) or {}
            _lock_at = float(_adv_t.get("lock_profit_at", 0) or 0)
            _lock_amt = float(_adv_t.get("lock_profit", 0) or 0)
            _lock_unit = str(_adv_t.get("lock_profit_unit") or "pct")
        except Exception:
            _lock_at = 0.0
            _lock_amt = 0.0
            _lock_unit = "pct"
        _lock_armed = False
        if _lock_at > 0 and entry_idx is not None and entry_price > 0:
            try:
                from utils.helpers import trailing_lock_level as _tll
                _prevL = self._trailing_best.get(entry_idx)
                _newL = _tll(entry_price, float(_prevL or 0),
                             (txn_type or "buy") == "buy", _lock_at, _lock_amt,
                             unit=_lock_unit)
                if _newL > 0:
                    _lock_armed = True
                    if (txn_type or "buy") == "buy":
                        if _newL > sl_level:
                            sl_level = _newL
                    else:
                        if sl_level == 0 or _newL < sl_level:
                            sl_level = _newL
            except Exception:
                pass
        trailing = str(getattr(self, "_advanced", {}).get("trailing", "") or "").strip()
        if trailing and trailing.lower() not in ("off", "none", "0", "false", "lock") and entry_idx is not None:
            try:
                _trail_dist = float(trailing)
            except Exception:
                _trail_dist = 0.0
            # RULEBOOK: lock_profit_at diya ho to trailing LOCK arm hone ke
            # baad hi start hota hai (10 pts profit -> 5 lock -> 5-pt trail).
            # Pehle step-based trail entry bar ke extreme se hi SL ko entry
            # ke paas kheench leta tha -> winners breakeven-minus-costs par
            # band ho rahe the (0% win rate).
            _trail_ok = (_lock_armed or _lock_at <= 0)
            if _trail_dist > 0 and _trail_ok:
                # prev_best is updated at the END of each bar (see below).
                _prev = self._trailing_best.get(entry_idx)
                if txn_type == "buy":
                    if _prev and _prev > 0:
                        _td = _prev - _trail_dist
                        # Sirf profit side me trail (loss-side breakeven lock nahi)
                        if _td > entry_price and _td > sl_level:
                            sl_level = _td
                else:  # sell
                    if _prev and _prev > 0:
                        _td = _prev + _trail_dist
                        if _td < entry_price and (sl_level == 0 or _td < sl_level):
                            sl_level = _td
        elif (not _lock_at) and trailing and trailing.lower() == "lock" and entry_idx is not None:
            # Break-even lock once any prior bar closed in profit.
            _prev = self._trailing_best.get(entry_idx)
            if _prev and _prev > 0:
                if txn_type == "buy":
                    if (_prev - entry_price) > 0 and sl_level < entry_price:
                        sl_level = entry_price
                else:  # sell
                    if (entry_price - _prev) > 0 and (sl_level == 0 or sl_level > entry_price):
                        sl_level = entry_price
        try:
            self._last_sl_trailed = abs(float(sl_level) - float(self._sl_initial_level or 0)) > 0.0001
        except Exception:
            self._last_sl_trailed = False
        # ... rest of method

        # EQ (equity spot) legs: SL/TP on the BAR's spot high/low directly -
        # no Black-Scholes, no option chain. Same source as EQ entry.
        if str(option_type or "").upper() == "EQ":
            try:
                _hs = float(current.get("high_price", 0) or current.get("close_price", 0) or 0)
                _ls = float(current.get("low_price", 0) or current.get("close_price", 0) or 0)
                if _hs and _ls:
                    _eh, _el = max(_hs, _ls), min(_hs, _ls)
                    self._trail_update(entry_idx, _eh, _el, txn_type)
                    # v7: EQ ka bhi close-confirmed target (wick par nahi).
                    _hit = self._check_sl_tp_levels(
                        txn_type, sl_level, tp_level, _eh, _el,
                        bar_open=float(current.get("open_price", 0) or 0))
                    return self._confirm_tp(
                        _hit, txn_type, tp_level,
                        float(current.get("close_price", 0) or 0))
            except Exception:
                pass
        # SL/TP is on OPTION PREMIUM, not spot. Only the option's OWN real
        # print high/low is used — never a modeled Black-Scholes range.
        strike = float(entry.get("strike", 0) or 0)
        cur_d = str(current.get("trade_date", ""))[:10]
        # Daily high/low cache is a WHOLE-SESSION range. On a 5m/15m bar it
        # contains prices from before/after this bar (look-ahead) - that is
        # how a 12:05 sell hit TP on the 09:15 low. Clocked bars must use
        # their own real option bar (below).
        try:
            if not self._intraday_bar():
                _bd = str(current.get("trade_date", ""))[:10]
                _oh, _ol, _oc = 0, 0, 0
                for _sp in (str(strike), str(int(strike)) if float(strike) == int(float(strike)) else str(strike)):
                    _oh = float(self.premium_cache.get(f"{_bd}_{_sp}_{option_type}_h", 0) or 0)
                    _ol = float(self.premium_cache.get(f"{_bd}_{_sp}_{option_type}_l", 0) or 0)
                    _oc = float(self.premium_cache.get(f"{_bd}_{_sp}_{option_type}_c", 0) or 0)
                    if _oh > 0 and _ol > 0:
                        break
                if _oh > 0 and _ol > 0:
                    _eh, _el = max(_oh, _ol), min(_oh, _ol)
                    self._trail_update(entry_idx, _eh, _el, txn_type)
                    # v7: target = day ke OPTION CLOSE par confirmed (wick
                    # high/low par nahi) - paper monitor bhi spike touch par
                    # exit nahi karta. SL worst-case extreme par hi rehta hai.
                    _hit = self._check_sl_tp_levels(txn_type, sl_level, tp_level, _eh, _el)
                    return self._confirm_tp(_hit, txn_type, tp_level, _oc)
        except Exception:
            pass
        # STRICT real-contract bar: high/low/open/close of THIS option from
        # recorded tick / 1-minute-chain data ONLY. No Black-Scholes and no
        # delta-synthetic band ? an invented range minted fantasy SL/TP hits.
        _obar = self._real_option_bar(cur_d, strike, option_type)
        if not _obar or _obar.get("high") is None:
            # Real premium unavailable for this bar -> honest no-hit (never
            # fabricate a modelled option path).
            return None
        prem_high, prem_low = _obar["high"], _obar["low"]
        prem_open, prem_close = _obar["open"], _obar["close"]
        self._trail_update(entry_idx, prem_high, prem_low, txn_type)
        _hit = self._check_sl_tp_levels(txn_type, sl_level, tp_level, prem_high, prem_low,
                                        bar_open=prem_open)
        return self._confirm_tp(_hit, txn_type, tp_level, prem_close)

    def _real_option_bar(self, date, strike, option_type):
        """OHLC of one concrete option contract from the Parquet lakehouse.

        Tick/1m-chain rows are IST-sorted; only prints up to the current bar
        clock are used (no look-ahead). Returns None honestly when no real
        data exists — never a modeled Black-Scholes/delta substitute.
        """
        try:
            from core.services.option_lakehouse import query_option_ticks
            _d = str(date or "")[:10]
            _clock = str(getattr(self, "_cur_bar_time", "") or "")
            _pref = str(getattr(self, "bt_expiry", "") or "")
            if not _pref:
                try:
                    _pref = str(self._expiry_for_date(_d) or "")
                except Exception:
                    _pref = ""
            rows = None
            _modes = (("1m_chain", "tick") if self._intraday_bar() else ("tick", "1m_chain"))
            _exps = self._lakehouse_expiry_candidates(_d, _pref)
            for _mode in _modes:
                for _expiry in _exps:
                    _r = query_option_ticks(
                        underlying=self.bt_symbol, data_mode=_mode, expiry=_expiry,
                        start_date=_d, end_date=_d, option_type=option_type,
                        strike=float(strike) if strike else None, limit=5000)
                    if _r is not None and len(_r) > 0:
                        rows = _r
                        break
                if rows is not None and len(rows) > 0:
                    break
            if (rows is None or len(rows) == 0) and strike:
                # Nearest real strike that actually traded that day.
                for _mode in _modes:
                    for _expiry in _exps:
                        _r = query_option_ticks(
                            underlying=self.bt_symbol, data_mode=_mode, expiry=_expiry,
                            start_date=_d, end_date=_d, option_type=option_type,
                            strike=None, limit=20000)
                        if _r is None or len(_r) == 0:
                            continue
                        _all = [r for r in _r.to_dicts() if float(r.get("strike") or 0) > 0]
                        if not _all:
                            continue
                        _kbest = min(_all, key=lambda r: abs(float(r.get("strike") or 0) - float(strike)))
                        _ksel = float(_kbest.get("strike") or 0)
                        import polars as _pl
                        rows = _r.filter(_pl.col("strike") == _ksel)
                        break
                    if rows is not None and len(rows) > 0:
                        break
            if rows is None or len(rows) == 0:
                return None
            recs = rows.to_dicts()
            if _clock:
                recs = [r for r in recs if str(r.get("timestamp", ""))[11:16] <= _clock]
                if not recs:
                    # Only later prints exist for this day; never peek ahead.
                    return None
            points = []
            for r in recs:
                px = float(r.get("ltp") or r.get("close_price") or 0)
                hi = float(r.get("high_price") or 0) or px
                lo = float(r.get("low_price") or 0) or px
                if px > 0:
                    points.append((px, hi, lo))
            if not points:
                return None
            return {"open": points[0][0], "close": points[-1][0],
                    "high": max(p[1] for p in points), "low": min(p[2] for p in points)}
        except Exception:
            return None

    def _day_iv(self, date, spot=0.0):
        """Us din ka REAL implied volatility (DB ke actual option prints se).

        Ek query: us din ke saare option rows (strike/type/expiry/close) +
        us din ka underlying close. ATM ke closest strike ka IV nikaalte
        hain. Zero = data nahi mila (model IV use hoga)."""
        try:
            d = str(date or "")[:10]
            if not d:
                return 0.0
            memo = getattr(self, "_day_iv_cache", None)
            if not isinstance(memo, dict):
                memo = {}
            if d in memo:
                return float(memo.get(d) or 0)
            memo[d] = 0.0
            self._day_iv_cache = memo
            if getattr(self, "_skip_db", False):
                return 0.0
            from core.models.database import Database as _DB
            db = _DB.get_instance()
            sym = getattr(self, "bt_symbol", "")
            urow = db.fetch_one(
                "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND trade_date=? "
                "AND option_type IS NULL LIMIT 1", [sym, d])
            uclose = float((urow or {}).get("close_price") or 0) or float(spot or 0)
            if uclose <= 0:
                return 0.0
            rows = db.fetch_all(
                "SELECT strike_price, option_type, close_price, expiry_date FROM bhavcopy_data "
                "WHERE symbol=? AND trade_date=? AND option_type IN ('CE','PE')",
                [sym, d])
            if not rows:
                return 0.0
            from utils.helpers import implied_volatility as _iv_solver
            best_iv, best_dist = 0.0, 1e12
            for r in rows:
                try:
                    k = float(r.get("strike_price") or 0)
                    px = float(r.get("close_price") or 0)
                    if k <= 0 or px <= 0:
                        continue
                    exp = str(r.get("expiry_date") or "")[:10]
                    if not exp:
                        continue
                    import datetime as _dt
                    d1 = _dt.datetime.strptime(d, "%Y-%m-%d")
                    d2 = _dt.datetime.strptime(exp, "%Y-%m-%d")
                    yrs = max((d2 - d1).days, 0) / 365.0
                    if yrs <= 0:
                        continue
                    dist = abs(k - uclose)
                    if dist >= best_dist:
                        continue
                    iv = _iv_solver(px, uclose, k, yrs, str(r.get("option_type") or "CE"))
                    if iv and iv > 0:
                        best_iv, best_dist = float(iv), dist
                except Exception:
                    continue
            memo[d] = best_iv
            return best_iv
        except Exception:
            return 0.0

    def _pricing_iv(self, date, strike, option_type, spot=0.0):
        """Explicit user IV ya us din ka REAL IV — koi model IV nahi."""
        try:
            _exp = getattr(self, "implied_volatility", None)
            if _exp:
                return float(_exp)
            return float(self._day_iv(date, spot) or 0)
        except Exception:
            return 0.0

    def _prem_source(self, date, strike, option_type) -> str:
        """Premium kahan se aaya: db / db_near / live / model / spot (EQ).
        Per (date,strike) memoize - same contract dobara puche to DB nahi.
        Spec §7 labeling."""
        try:
            _ck = (str(date or "")[:10], float(strike or 0), str(option_type or "").upper())
            # Real tick / fake fallback labels memo se PEHLE - warna purana
            # 'model' label cache ho kar chhupa reh jata tha.
            if _ck in getattr(self, "_tick_prem_keys", set()):
                return "real_ticks"
            if _ck in getattr(self, "_db_prem_keys", set()):
                return "db"
            if _ck in getattr(self, "_fake_prem_keys", set()):
                return "fake_fallback"
            _memo = getattr(self, "_prem_src_cache", None)
            if isinstance(_memo, dict) and _ck in _memo:
                return _memo[_ck]
        except Exception:
            _memo = None
        _src = "no_data"
        try:
            if str(option_type or "").upper() == "EQ":
                _src = "spot"
            elif getattr(self, "is_live", False):
                _src = "live"
            elif self._intraday_bar():
                # Clocked bar reads ONLY the real 1m-chain/tick print.
                _src = "real_ticks" if _ck in getattr(self, "_tick_prem_keys", set()) else "no_data"
            elif not getattr(self, "_skip_db", False):
                from core.models.database import Database as _DB
                _db = _DB.get_instance()
                try:
                    row = _db.fetch_one(
                        "SELECT open_price, close_price FROM bhavcopy_data WHERE symbol=? AND trade_date=? AND strike_price=? AND option_type=?",
                        [getattr(self, "bt_symbol", ""), str(date or "")[:10], float(strike or 0), option_type])
                    if row and (float(row.get("open_price") or 0) > 0 or float(row.get("close_price") or 0) > 0):
                        _src = "db"
                except Exception:
                    pass
                if _src == "model":
                    try:
                        from utils.helpers import get_strike_step as _step
                        _st = _step(getattr(self, "bt_symbol", "") or "") or 0
                        row2 = _db.fetch_one(
                            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type=? AND ABS(strike_price-?) <= ?*2 AND trade_date=? ORDER BY ABS(strike_price-?) LIMIT 1",
                            [getattr(self, "bt_symbol", ""), option_type, float(strike or 0), _st, str(date or "")[:10], float(strike or 0)])
                        if row2 and float(row2.get("close_price") or 0) > 0:
                            _src = "db_near"
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            if isinstance(_memo, dict):
                _memo[_ck] = _src
                self._prem_src_cache = _memo
        except Exception:
            pass
        return _src

    def _check_sl_tp_levels(self, txn_type, sl_level, tp_level, opt_high, opt_low, bar_open=None):
        """Shared hit comparison for DB-range and model-range callers.

        `level` is the trigger (same as paper's sltp_premium_level). `fill`
        models a real stop-market order:
          - bar OPEN already beyond SL (gap) -> fill at open (first available
            price; worst realistic outcome),
          - intrabar breach -> fill at the SL LEVEL (the stop triggers there;
            slippage is applied once by the caller),
          - `bar_open=None` (legacy callers / unit tests) -> breached extreme
            (max pessimism, old documented behavior).
        TP is a resting LIMIT at the level (conservative)."""
        try:
            hit_sl, hit_tp = False, False
            if txn_type == "buy":
                hit_sl = sl_level > 0 and opt_low <= sl_level
                hit_tp = tp_level > 0 and opt_high >= tp_level
            else:
                hit_sl = sl_level > 0 and opt_high >= sl_level
                hit_tp = tp_level > 0 and opt_low <= tp_level
            if hit_sl:
                _ob = 0.0
                try:
                    _ob = float(bar_open or 0)
                except Exception:
                    _ob = 0.0
                if _ob > 0:
                    # Gap-through fills at the open; else stop triggers at level.
                    if txn_type == "buy":
                        fill = _ob if _ob <= sl_level else sl_level
                    else:
                        fill = _ob if _ob >= sl_level else sl_level
                else:
                    fill = min(sl_level, opt_low) if txn_type == "buy" else max(sl_level, opt_high)
                return {"reason": "stoploss", "level": sl_level, "fill": fill}
            if hit_tp:
                return {"reason": "target", "level": tp_level, "fill": tp_level}
        except Exception:
            pass
        return None

    def run_vectorbt(self, df, symbol="NIFTY"):
        """Vectorbt lightning-fast in-memory backtest on DataFrame (no CSV)."""
        try:
            import vectorbt as vbt
            # df is Pandas DataFrame with columns: open, high, low, close (lowercase from nsepython/openchart)
            # Map to proper case for vectorbt
            if 'close' in df.columns:
                close = df['close']
            elif 'Close' in df.columns:
                close = df['Close']
            else:
                close = df.iloc[:,3] if len(df.columns)>3 else df.iloc[:,0]
            # Simple SMA crossover via vectorbt
            fast_ma = vbt.MA.run(close, 13)
            slow_ma = vbt.MA.run(close, 34)
            entries = fast_ma.ma_crossed_above(slow_ma)
            exits = fast_ma.ma_crossed_below(slow_ma)
            pf = vbt.Portfolio.from_signals(close, entries, exits, fees=0.001, sl_stop=0.02, tp_stop=0.04)
            return {"success": True, "stats": pf.stats(), "trades": pf.trades.records_readable, "equity": pf.value()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _square_off_expiry(self, historical, entries, exits, symbol, option_type, txn_type, qty, risk_management, cur_date=None):
        """Automatically square off all open option positions on expiry day at market close.
        This prevents physical delivery risk and is critical for F&O options.
        LAST-BAR ONLY (cur_date must equal the window's last date): an earlier
        revision ran this on EVERY bar, closing positions at the FUTURE last-bar
        price days early (look-ahead) and freeing bogus re-entries."""
        import datetime
        # Find the last bar's date
        last_date = historical[-1]["trade_date"] if historical else ""
        if cur_date and cur_date != last_date:
            return None
        # Check if any position is open and we're on or near expiry
        open_positions = len(entries) - len(exits)
        if open_positions > 0:
            # Parse expiry: legs' explicit expiry_date wins; otherwise the
            # exchange rule (NSE Tue / BSE Thu weekly; last one monthly).
            exp_type = getattr(self, '_expiry_hint', 'weekly')
            try:
                bar_dt = datetime.datetime.strptime(last_date, "%Y-%m-%d")
                if getattr(self, '_explicit_expiry', False) and self.bt_expiry:
                    # Fixed contract the user chose: square off on ITS expiry day.
                    is_expiry = (self.bt_expiry[:10] == last_date)
                elif self._use_weekly(exp_type):
                    is_expiry = (bar_dt.weekday() == self._expiry_weekday())
                else:
                    _last = self._last_weekday_of_month(bar_dt.year, bar_dt.month,
                                                        self._expiry_weekday())
                    is_expiry = (_last.strftime("%Y-%m-%d") == last_date)
                
                if is_expiry:
                    # Square off all positions at close price (last bar's close)
                    for i in range(len(exits), len(entries)):
                        entry = entries[i]
                        exit_prem = self._close_premium(last_date, float(historical[-1]["close_price"]), 
                            float(entry["strike"]), option_type)
                        exit_prem = TransactionCosts.apply_fill_slippage(exit_prem, 
                            "SELL" if txn_type == "buy" else "BUY", self.is_live,
                            slippage_pct=float(getattr(self, "_slippage_pct", 0.5) or 0.5))
                        self._close_position(entries, exits, entry, exit_prem, "expiry_squareoff",
                            last_date, qty, txn_type, trigger="expiry")
            except Exception:
                pass
        return None

    def prefetch_option_premiums(self, symbol, start_date, end_date):
        """One batched query for the whole backtest window into premium_cache.
        Without this, every bar x leg costs 1-2 PG roundtrips (200+ queries =
        10-20s on cloud). Same key/value semantics as the per-bar lookups."""
        try:
            if getattr(self, "_skip_db", False):
                return 0
            from core.models.database import Database
            rows = Database.get_instance().fetch_all(
                "SELECT trade_date, strike_price, option_type, open_price, high_price, low_price, close_price "
                "FROM bhavcopy_data WHERE symbol=? AND option_type IN ('CE','PE') "
                "AND trade_date BETWEEN ? AND ?",
                [symbol, str(start_date)[:10], str(end_date)[:10]],
            )
            n = 0
            for r in rows or []:
                try:
                    d = str(r.get("trade_date", ""))[:10]
                    st = float(r.get("strike_price") or 0)
                    op = str(r.get("option_type") or "")
                    o = float(r.get("open_price") or 0)
                    c = float(r.get("close_price") or 0)
                    if not d or st <= 0 or op not in ("CE", "PE"):
                        continue
                    # Store both float + int-like strike spellings: engine
                    # callers pass either (22850 vs 22850.0) and keys must hit.
                    _spell = [str(st)]
                    try:
                        if float(st) == int(float(st)):
                            _spell.append(str(int(float(st))))
                    except Exception:
                        pass
                    for _sp in _spell:
                        if o > 0:
                            self.premium_cache.setdefault(f"{d}_{_sp}_{op}_e", o)
                            n += 1
                        elif c > 0:
                            self.premium_cache.setdefault(f"{d}_{_sp}_{op}_e", c)
                            n += 1
                        if c > 0:
                            self.premium_cache.setdefault(f"{d}_{_sp}_{op}_c", c)
                            n += 1
                        elif o > 0:
                            self.premium_cache.setdefault(f"{d}_{_sp}_{op}_c", o)
                            n += 1
                        # Option bar high/low for SL/TP parity (same source as
                        # entry, never spot-BS vs DB mix on one bar)
                        try:
                            _h = float(r.get("high_price") or 0)
                            _l = float(r.get("low_price") or 0)
                            if _h > 0:
                                self.premium_cache.setdefault(f"{d}_{_sp}_{op}_h", _h)
                            if _l > 0:
                                self.premium_cache.setdefault(f"{d}_{_sp}_{op}_l", _l)
                        except Exception:
                            pass
                except Exception:
                    continue
            return n
        except Exception:
            return 0

    def _intraday_bar(self) -> bool:
        """True when the engine is executing an intraday (clock) bar.

        Bhavcopy option prints are DAILY (one open/high/low/close per date).
        A 5m/15m bar at 12:05 must NOT read the 09:15 daily open or the whole
        day's high/low - that is look-ahead and was the #1 backtest-vs-paper
        gap (daily print -> same entry premium all day + TP hit on the day's
        low every time). Intraday bars are priced from the bar's own spot via
        the shared model/cache instead.
        """
        return bool(getattr(self, "_cur_bar_time", ""))

    # ---- Real-print resolvers (shared by entry / exit / SL-TP bars) -------
    def _lakehouse_expiry_candidates(self, date, preferred=""):
        """Expiry partitions to try: preferred/calendar first, then every real
        lakehouse expiry for this underlying, nearest first. Bhavcopy expiry is
        exchange-truth and can be a day/contract off the calendar guess, which
        used to make a valid backtest silently return 0 trades."""
        import datetime as _dt
        out = []
        pref = str(preferred or "")[:10]
        if pref:
            out.append(pref)
        try:
            from core.services.option_lakehouse import OPTION_TICKS_ROOT
            base = OPTION_TICKS_ROOT / f"underlying={getattr(self, 'bt_symbol', '')}"
            exps = set()
            if base.is_dir():
                for mode_dir in base.iterdir():
                    if not mode_dir.is_dir():
                        continue
                    for ed in mode_dir.iterdir():
                        if ed.is_dir() and ed.name.startswith("expiry="):
                            e = ed.name.split("=", 1)[-1]
                            if e:
                                exps.add(e)
            d0 = str(date or "")[:10]

            def _dist(e):
                try:
                    return abs((_dt.date.fromisoformat(e) - _dt.date.fromisoformat(d0)).days)
                except Exception:
                    return 10 ** 6
            for e in sorted(exps, key=_dist):
                if e not in out:
                    out.append(e)
        except Exception:
            pass
        return out

    def _pick_real_row(self, rows):
        """Last real print at/before the current bar clock (never look ahead).

        Daily rows sorted ASC. If every print is after the clock, the earliest
        print is used (a real price, just stale) instead of a future one."""
        clock = str(getattr(self, "_cur_bar_time", "") or "")
        if clock:
            prior = [r for r in rows if str(r.get("timestamp", ""))[11:16] <= clock]
            if prior:
                return prior[-1]
            return rows[0] if rows else None
        return rows[-1] if rows else None

    def _lakehouse_premium(self, date, strike, option_type, anchor_exp=None):
        """Real option premium from the Parquet lakehouse for this contract.

        Tries intraday mode first on clocked bars then tick, and every real
        expiry (nearest first). Falls back to the nearest real strike for that
        day when the aligned strike has no print. Real prints only - never a
        modeled Black-Scholes premium."""
        try:
            from core.services.option_lakehouse import query_option_ticks
        except Exception:
            return None
        d = str(date or "")[:10]
        if not d:
            return None
        pref = str(anchor_exp or getattr(self, "bt_expiry", "") or "")
        if not pref:
            try:
                pref = str(self._expiry_for_date(d) or "")
            except Exception:
                pref = ""
        modes = ("1m_chain", "tick") if self._intraday_bar() else ("tick", "1m_chain")
        exps = self._lakehouse_expiry_candidates(d, pref)

        def _query(mode, exp, k):
            try:
                return query_option_ticks(
                    underlying=self.bt_symbol, data_mode=mode, expiry=exp,
                    start_date=d, end_date=d, option_type=option_type,
                    strike=k, limit=4000)
            except Exception:
                return None

        # 1) Exact strike
        if strike:
            for mode in modes:
                for exp in exps:
                    df = _query(mode, exp, float(strike))
                    if df is not None and len(df) > 0:
                        row = self._pick_real_row(df.to_dicts())
                        if row:
                            val = float(row.get("ltp") or row.get("close_price") or 0)
                            if val > 0:
                                self._tick_prem_keys.add(
                                    (d, float(strike or 0), str(option_type or "").upper()))
                                return val
        # 2) Nearest real strike on the same day (any strike -> closest)
        for mode in modes:
            for exp in exps:
                df = _query(mode, exp, None)
                if df is None or len(df) == 0:
                    continue
                try:
                    rows = df.to_dicts()
                except Exception:
                    continue
                rows = [r for r in rows if float(r.get("strike") or 0) > 0]
                if not rows:
                    continue
                target = float(strike or 0)
                if target > 0:
                    kbest = min(rows, key=lambda r: abs(float(r.get("strike") or 0) - target))
                    ksel = float(kbest.get("strike") or 0)
                    rows = [r for r in rows if abs(float(r.get("strike") or 0) - ksel) < 0.01]
                row = self._pick_real_row(rows)
                if row:
                    val = float(row.get("ltp") or row.get("close_price") or 0)
                    if val > 0:
                        self._tick_prem_keys.add(
                            (d, float(strike or 0), str(option_type or "").upper()))
                        return val
        return None

    def _db_premium(self, date, strike, option_type, for_exit=False):
        """Real bhavcopy print from the local DB (works offline).

        One daily open/high/low/close per contract exists, so a clocked bar
        uses the real OPEN before 15:15 (no look-ahead) and the real CLOSE at
        the session end. Nearest real strike within two steps is used when the
        aligned strike was not traded that day."""
        if getattr(self, "_skip_db", False):
            return None
        d = str(date or "")[:10]
        if not d or not strike:
            return None
        clock = str(getattr(self, "_cur_bar_time", "") or "")
        want_close = bool(for_exit) and (clock >= "15:15" if clock else True)
        try:
            db = Database.get_instance()
            row = db.fetch_one(
                "SELECT open_price, close_price FROM bhavcopy_data WHERE symbol=? AND trade_date=? "
                "AND ABS(strike_price-?)<0.01 AND option_type=?",
                [self.bt_symbol, d, float(strike or 0), option_type])
            if not row:
                from utils.helpers import get_strike_step
                step = get_strike_step(self.bt_symbol)
                row = db.fetch_one(
                    "SELECT open_price, close_price FROM bhavcopy_data WHERE symbol=? AND option_type=? "
                    "AND trade_date=? AND ABS(strike_price-?) <= ?*2 ORDER BY ABS(strike_price-?) LIMIT 1",
                    [self.bt_symbol, option_type, d, float(strike or 0), step, float(strike or 0)])
            if row:
                op = float(row.get("open_price") or 0)
                cl = float(row.get("close_price") or 0)
                val = cl if (want_close and cl > 0) else (op if op > 0 else cl)
                if val > 0:
                    self._db_prem_keys.add(
                        (d, float(strike or 0), str(option_type or "").upper()))
                    return val
        except Exception:
            return None
        return None

    def _entry_premium(self, date, spot, strike, option_type):
        key = f"{date}_{strike}_{option_type}_e"
        _intra = self._intraday_bar()
        # Intraday bar: the date-only cache holds a DAILY open print (one
        # value for the whole session). Never use it for a clocked bar.
        if not _intra and key in self.premium_cache:
            return self.premium_cache[key]
        # DATA HONESTY: REAL option prints only (lakehouse tick/1m_chain or
        # local bhavcopy). NEVER a Black-Scholes/synthetic premium. If there is
        # genuinely no real print for the day, return None so the caller skips
        # the entry honestly instead of trading on fabricated prices.
        val = self._lakehouse_premium(date, strike, option_type)
        if not val:
            val = self._db_premium(date, strike, option_type, for_exit=False)
        if val and float(val) > 0:
            if not _intra:
                self.premium_cache[key] = float(val)
            return float(val)
        # Real LTP from live market - only in live mode
        if self.is_live:
            try:
                from core.services.live_market_data import LiveMarketData
                live = LiveMarketData().get_option_ltp(self.bt_symbol, strike, option_type, getattr(self, 'bt_expiry', '') or '')
                if live and float(live) > 0:
                    val = float(live)
                    self.premium_cache[key] = val
                    return val
            except Exception:
                pass
        # NO Black-Scholes / synthetic fallback. Return None honestly so the
        # caller can skip entry or mark the trade as unpriceable.
        return None

    def _expiry_weekday(self) -> int:
        """Expiry weekday (NSE 2025+ rule). NSE weeklies = TUESDAY; BSE
        (SENSEX/BANKEX) = THURSDAY. Same for monthly contracts (last one of
        the month). BANKNIFTY/FINNIFTY/stocks are NSE."""
        sym = str(getattr(self, "bt_symbol", "") or "").upper().replace(" ", "")
        return 3 if sym in ("SENSEX", "BANKEX") else 1

    def _use_weekly(self, expiry_type) -> bool:
        """Weekly only when hinted weekly AND the symbol actually has weekly
        contracts (BANKNIFTY/FINNIFTY and stocks have MONTHLY only)."""
        if str(expiry_type or "weekly").lower() == "monthly":
            return False
        sym = str(getattr(self, "bt_symbol", "") or "").upper().replace(" ", "")
        return sym in ("NIFTY", "SENSEX", "BANKEX")

    def _last_weekday_of_month(self, year, month, wd: int):
        import datetime
        import calendar
        d = datetime.date(year, month, calendar.monthrange(year, month)[1])
        while d.weekday() != wd:
            d -= datetime.timedelta(days=1)
        return d

    def _next_month(self, y: int, m: int):
        return (y, m + 1) if m < 12 else (y + 1, 1)

    def _get_expiry_type(self):
        # Check legs for expiry hint
        return getattr(self, '_expiry_hint', 'weekly')

    def _exit_premium(self, date, spot, strike, option_type):
        # EQ legs exit at the execution-bar spot (no option chain involved)
        if str(option_type or "").upper() == "EQ":
            return max(float(spot or 0), 0.01)
        return self._entry_premium(date, spot, strike, option_type)

    def _close_premium(self, date, spot, strike, option_type, anchor_exp=None):
        # EQ legs exit at the execution-bar spot (no option chain involved)
        if str(option_type or "").upper() == "EQ":
            return max(float(spot or 0), 0.01)
        key = f"{date}_{strike}_{option_type}_c"
        _intra = self._intraday_bar()
        if not _intra and key in self.premium_cache:
            return self.premium_cache[key]
        # REAL exit print: lakehouse tick/1m_chain first, then local bhavcopy.
        # Only the last real print at/before this bar's clock is used (no
        # look-ahead); never a modeled Black-Scholes premium.
        val = self._lakehouse_premium(date, strike, option_type, anchor_exp=anchor_exp)
        if not val:
            val = self._db_premium(date, strike, option_type, for_exit=True)
        if val and float(val) > 0:
            if not _intra:
                self.premium_cache[key] = float(val)
            return float(val)
        # Real live LTP fallback - only in live mode
        if self.is_live:
            try:
                from core.services.live_market_data import LiveMarketData
                live = LiveMarketData().get_option_ltp(self.bt_symbol, strike, option_type, getattr(self, 'bt_expiry', '') or '')
                if live and float(live) > 0:
                    val = float(live)
                    self.premium_cache[key] = val
                    return val
            except Exception:
                pass
        # NO Black-Scholes / synthetic fallback. Return None honestly so the
        # caller can skip exit or mark the trade as unpriceable.
        return None

    def _expiry_for_date(self, bar_date):
        """Exchange expiry for PRICING on `bar_date`: NSE weekly = Tuesday,
        BSE = Thursday, monthly = last expiry weekday. NOTE: pure exchange
        calendar (parity-pinned tests) - the 2-day roll is a LIVE
        order-routing rule (order_expiry/roll_two_days), backtest pricing
        us se alag chalne par entry/DTE shape kuchal jata tha."""
        import datetime
        try:
            d = datetime.datetime.strptime(str(bar_date)[:10], "%Y-%m-%d").date() if bar_date else datetime.date.today()
        except Exception:
            d = datetime.date.today()
        wd = self._expiry_weekday()
        if self._use_weekly(self._get_expiry_type()):
            days = (wd - d.weekday()) % 7
            if days == 0:
                days = 7
            return (d + datetime.timedelta(days=days)).strftime("%Y-%m-%d")
        last = self._last_weekday_of_month(d.year, d.month, wd)
        if last <= d:
            y, m = self._next_month(d.year, d.month)
            last = self._last_weekday_of_month(y, m, wd)
        return last.strftime("%Y-%m-%d")

    def _days_to_expiry(self, bar_date, expiry_type="weekly", anchor=None):
        import datetime
        # PRE-SET CONTRACT WINS (base-parity semantics): bt_expiry ko run()
        # (legs.expiry_date), replay engine ya test fixtures pehle se de sakte
        # hain - non-empty hone par sab pricing USI date tak chalti hai,
        # overwrite kabhi nahi. Khali hai to bar date ke exchange calendar se
        # recompute hota hai (aur next call ke liye cache hota hai - base
        # behavior: expire hone par run-loop ise clear kar deta hai).
        try:
            if not anchor:
                anchor = str(getattr(self, "bt_expiry", "") or "")[:10]
        except Exception:
            pass
        if anchor:
            try:
                a = datetime.datetime.strptime(str(anchor)[:10], "%Y-%m-%d").date()
                b = datetime.datetime.strptime(str(bar_date)[:10], "%Y-%m-%d").date() if bar_date else a
                return max(1, (a - b).days)
            except Exception:
                return 7
        exp_date = self._expiry_for_date(bar_date)
        try:
            self.bt_expiry = str(exp_date)[:10]
        except Exception:
            pass
        try:
            bar_dt = datetime.datetime.strptime(bar_date, "%Y-%m-%d").date() if bar_date else datetime.date.today()
            exp_dt = datetime.datetime.strptime(str(exp_date)[:10], "%Y-%m-%d").date()
            return max(1, (exp_dt - bar_dt).days)
        except Exception:
            return 7

    def _open_unrealized(self, entry, cur, option_type, txn_type, qty):
        """Unrealized MTM of one OPEN position at current bar (costs ignored for trigger)."""
        try:
            if entry.get("is_spread"):
                spot = float(cur.get("close_price", 0) or 0)
                cur_val = 0.0
                for leg in entry.get("legs", []):
                    prem = self._close_premium(cur.get("trade_date", ""), spot, float(leg["strike"]), leg["option_type"])
                    if leg.get("type") == "buy":
                        cur_val += prem * float(leg.get("quantity", 0) or 0)
                    else:
                        cur_val -= prem * float(leg.get("quantity", 0) or 0)
                entry_cash = sum(float(l.get("signed_value", 0) or 0) for l in entry.get("legs", []))
                return cur_val + entry_cash
            spot = float(cur.get("close_price", 0) or 0)
            prem = self._close_premium(cur.get("trade_date", ""), spot, float(entry["strike"]), option_type)
            eqty = float(entry.get("quantity", qty) or qty)
            if txn_type == "buy":
                return prem * eqty - float(entry.get("total_cost", 0) or 0)
            # SELL: entry costs bhi ghatao (BUY jaisa) - warna MTM trigger
            # ~entry-costs der se fire hota tha (loser zyada der held).
            _basis = float(entry.get("price", 0) or 0) * eqty
            _costs = float(entry.get("total_cost", 0) or 0) - _basis
            return _basis - prem * eqty - max(0.0, _costs)
        except Exception:
            return 0.0

    def _close_position(self, entries, exits, entry, exit_prem, reason, exit_date, qty, txn_type, trigger=""):
        if entry.get("is_spread"):
            self._close_spread(entries, exits, entry, reason, exit_date, trigger=trigger)
            return
        # DATA HONESTY: if no real premium available, do NOT fabricate a fill.
        # Skip the exit honestly so paper/backtest parity is preserved.
        try:
            _ep = float(exit_prem)
        except Exception:
            _ep = 0.0
        if _ep <= 0:
            # No real price available - skip exit, mark trade as unclosed.
            entries.append({"_skip_exit": True, "reason": reason, "date": exit_date})
            return
        # Floor at option tick size only. Never floor to spot-based levels:
        # SL/TP exits (e.g. 9.30 on a far-OTM option) must execute exactly,
        # else a spot*1.5% floor (~42) silently destroys every stop-loss.
        exit_prem = max(_ep, 0.05)
        # TICK SIZE 0.05 (rulebook): exit fill bhi tick par snap.
        try:
            from core.services.strategy_rulebook import round_tick as _rtick_ex
            exit_prem = _rtick_ex(exit_prem)
        except Exception:
            pass
        # v7: EXIT-side latency. Stop/market/EOD exits trigger hone ke baad
        # order broker tak jaata hai - fill level se thoda worse hota hai.
        # TP ek resting LIMIT hai (level par fill, paper bhi level book karta
        # hai) - uspar latency nahi.
        if str(reason or "").lower() != "target":
            try:
                _lp = float(getattr(self, "_latency_pct_exit", 0) or 0)
            except Exception:
                _lp = 0.0
            if _lp > 0:
                exit_prem = self._apply_latency(
                    exit_prem, "SELL" if txn_type == "buy" else "BUY", pct=_lp)
                self._n_exit_latency += 1
        try:
            _eq_exit = str((entry.get("legs", [{}])[0] or {}).get("option_type", "")) == "EQ"
        except Exception:
            _eq_exit = False
        if _eq_exit:
            # Exit side is opposite of entry (buy->SELL): same convention as above
            exit_costs = TransactionCosts.calculate_equity(
                exit_prem * qty, txn_type == "buy", getattr(self, "_eq_intraday", True))
        else:
            exit_costs = TransactionCosts.calculate(exit_prem * qty, txn_type == "buy", self.is_live)
        if txn_type == "buy":
            pnl = (exit_prem * qty - exit_costs["total"]) - entry["total_cost"]
        else:
            pnl = (entry["price"] * entry["quantity"] - entry["costs"]["total"]) - (exit_prem * qty + exit_costs["total"])
        exit_clock = (
            getattr(self, '_cur_bar_time', '')
            or getattr(self, '_early_exit_time', None)
            or getattr(self, '_exit_time', '15:14')
        )
        try:
            _elegs = entry.get("legs", [{}]) or [{}]
            _eopt = str((_elegs[0] or {}).get("option_type", "") or "")
        except Exception:
            _eopt = ""
        exits.append({
            "date": exit_date, "strike": entry["strike"], "price": round(exit_prem, 2),
            "quantity": qty, "exit_costs": exit_costs, "reason": reason, "pnl": round(pnl, 2),
            "time": exit_clock, "trigger": trigger or reason,
            "prem_src": self._prem_source(exit_date, entry.get("strike", 0), _eopt),
        })

    def _close_spread(self, entries, exits, entry, reason, exit_date, trigger=""):
        exit_costs_total = 0.0
        exit_value = 0.0
        for leg in entry.get("legs", []):
            option_type = leg["option_type"]
            strike = leg["strike"]
            lqty = leg["quantity"]
            spot = self._close_spot_for_date(exit_date)
            prem = self._close_premium(exit_date, spot, strike, option_type)
            prem = TransactionCosts.apply_fill_slippage(prem, "SELL" if leg["type"] == "buy" else "BUY", self.is_live,
                                                        slippage_pct=float(getattr(self, "_slippage_pct", 0.5) or 0.5))
            # v7 exit latency (spread legs bhi market exit hain, TP nahi)
            if str(reason or "").lower() != "target":
                try:
                    _lp = float(getattr(self, "_latency_pct_exit", 0) or 0)
                except Exception:
                    _lp = 0.0
                if _lp > 0:
                    prem = self._apply_latency(
                        prem, "SELL" if leg["type"] == "buy" else "BUY", pct=_lp)
                    self._n_exit_latency += 1
            costs = TransactionCosts.calculate(prem * lqty, leg["type"] == "buy", self.is_live)
            exit_costs_total += costs["total"]
            if leg["type"] == "buy":
                exit_value += prem * lqty
            else:
                exit_value -= prem * lqty
        entry_cash = sum(l["signed_value"] for l in entry["legs"])
        # entry["total_cost"] = entry_cash + entry_brokerage (Ec)
        entry_costs = entry["total_cost"] - entry_cash
        # Net PnL = entry_cash (credit + / debit -) + exit_value (reverse legs) - brokerage both sides
        pnl = entry_cash + exit_value - entry_costs - exit_costs_total
        exit_clock = (
            getattr(self, '_cur_bar_time', '')
            or getattr(self, '_early_exit_time', None)
            or getattr(self, '_exit_time', '15:14')
        )
        exits.append({
            "date": exit_date, "strike": entry["strike"], "price": round(abs(exit_value), 2),
            "quantity": entry["quantity"], "exit_costs": {"total": round(exit_costs_total, 2)},
            "reason": reason, "pnl": round(pnl, 2), "is_spread": True,
            "time": (
                getattr(self, '_cur_bar_time', '')
                or getattr(self, '_early_exit_time', None)
                or getattr(self, '_exit_time', '15:14')
            ),
            "trigger": trigger or reason, "prem_src": "spread",
        })

    def _close_spot_for_date(self, bar_date):
        # Intraday bars: use the bar's own spot (the daily DB close belongs to
        # 15:30, not to this clock time - same look-ahead as the premium fix).
        if self._intraday_bar():
            return float(getattr(self, "_cur_close", 0) or 0)
        if getattr(self, "_skip_db", False):
            return float(getattr(self, "_cur_close", 0) or 0)
        row = Database.get_instance().fetch_one(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND trade_date=? AND option_type IS NULL",
            [self.bt_symbol, bar_date],
        )
        if row and row["close_price"]:
            return float(row["close_price"])
        row2 = Database.get_instance().fetch_one(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND trade_date=? AND option_type='CE' ORDER BY ABS(strike_price - (SELECT close_price FROM bhavcopy_data WHERE symbol=? AND trade_date=? AND option_type IS NULL)) LIMIT 1",
            [self.bt_symbol, bar_date, self.bt_symbol, bar_date],
        )
        if row2 and row2["close_price"]:
            return float(row2["close_price"])
        return 0

    def _build_result(self, symbol, start_date, end_date, entries, exits, historical=None):
        equity = [self.initial_capital]
        capital = self.initial_capital
        total_trades = 0
        wins = 0
        win_amounts = []
        loss_amounts = []
        trade_list = []
        total_brokerage = 0
        for i in range(len(entries)):
            entry = entries[i]
            exit = exits[i] if i < len(exits) else None
            if exit:
                pnl = exit["pnl"]
                capital += pnl
                total_trades += 1
                total_brokerage += entry["costs"]["total"] + exit["exit_costs"]["total"]
                if pnl > 0:
                    wins += 1
                    win_amounts.append(pnl)
                else:
                    loss_amounts.append(abs(pnl))
                entry_time_str = entry.get("time", "09:35")
                exit_time_str = exit.get("time", "15:14")
                if entry.get("is_spread"):
                    legs_str = " + ".join(
                        f"{'Sell' if l['type']=='sell' else 'Buy'} {l['option_type']} {l['strike']}"
                        for l in entry.get("legs", [])
                    )
                    trade_list.append({
                        "index": total_trades,
                        "symbol": symbol,
                        "entry_date": entry.get("date", ""),
                        "entry_time": entry_time_str,
                        "exit_date": exit.get("date", ""),
                        "exit_time": exit_time_str,
                        "option_type": "Spread",
                        "strike": entry.get("strike", ""),
                        "expiry": entry.get("expiry", ""),
                        "position": legs_str,
                        "quantity": entry.get("quantity", 0),
                        "lots": entry.get("legs", [{}])[0].get("lots", 1) if entry.get("legs") else 1,
                        "entry_price": entry.get("price", 0),
                        "exit_price": exit.get("price", 0),
                        "gross_pnl": None,
                        "slippage": None,
                        "pnl": pnl,
                        "pnl_formatted": f"₹{pnl:,.2f}",
                        "exit_reason": exit.get("reason", ""),
                        "reason": exit.get("reason", ""),
                        "trigger": exit.get("trigger", "") or exit.get("reason", ""),
                        "entry_src": "spread",
                        "exit_src": exit.get("prem_src", "spread"),
                        "signal_side": "",
                        "signal_indicators": [],
                        "validation": None,
                        "vix": None,
                        "is_spread": True,
                        "risk_amount_rs": round(float(entry.get("risk_amount_rs", 0) or 0), 2),
                        "r_multiple": round(pnl / max(float(entry.get("risk_amount_rs", 0) or 0), 0.01), 2)
                        if float(entry.get("risk_amount_rs", 0) or 0) > 0 else 0.0,
                        "signal_time": entry.get("signal_time", entry.get("time", "")),
                    })
                else:
                    leg_info = entry.get("legs", [{}])[0] if entry.get("legs") else {}
                    try:
                        _gq = int(entry.get("quantity", 0) or 0)
                        _ge = float(entry.get("price", 0) or 0)
                        _gx = float(exit.get("price", 0) or 0)
                        _gross = ((_gx - _ge) if entry.get("type") == "buy" else (_ge - _gx)) * _gq
                        _slip = _gross - float(pnl or 0)
                        # Structured diagnostics: gross vs all-in charges vs net
                        # (audit trail per trade - parity reviews).
                        try:
                            _chg = float((entry.get("costs") or {}).get("total", 0) or 0) + float((exit.get("exit_costs") or {}).get("total", 0) or 0)
                        except Exception:
                            _chg = 0.0
                    except Exception:
                        _gross, _slip, _chg = 0.0, 0.0, 0.0
                    try:
                        _sig = entry.get("signal", {}) or {}
                    except Exception:
                        _sig = {}
                    _risk_amt = float(entry.get("risk_amount_rs", 0) or 0)
                    trade_list.append({
                        "index": total_trades,
                        "symbol": symbol,
                        "entry_date": entry.get("date", ""),
                        "entry_time": entry_time_str,
                        "exit_date": exit.get("date", ""),
                        "exit_time": exit_time_str,
                        "option_type": leg_info.get("option_type", "CE"),
                        "strike": entry.get("strike", ""),
                        "expiry": entry.get("expiry", ""),
                        "position": "Sell" if entry.get("type") == "sell" else "Buy",
                        "quantity": entry.get("quantity", 0),
                        "lots": leg_info.get("lots", 1),
                        "entry_price": entry.get("price", 0),
                        "exit_price": exit.get("price", 0),
                        "gross_pnl": round(_gross, 2),
                        "slippage": round(_slip, 2),
                        "charges": round(_chg, 2),
                        "pnl": pnl,
                        "pnl_formatted": f"₹{pnl:,.2f}",
                        "exit_reason": exit.get("reason", ""),
                        "reason": exit.get("reason", ""),
                        "trigger": exit.get("trigger", "") or exit.get("reason", ""),
                        "entry_src": entry.get("prem_src", ""),
                        "exit_src": exit.get("prem_src", ""),
                        "signal_side": _sig.get("side", ""),
                        "signal_indicators": _sig.get("indicators", []) or [],
                        "validation": None,
                        "vix": None,
                        "risk_amount_rs": round(_risk_amt, 2),
                        "r_multiple": round(pnl / max(_risk_amt, 0.01), 2) if _risk_amt > 0 else 0.0,
                        "signal_time": entry.get("signal_time", entry.get("time", "")),
                    })
            equity.append(capital)
        total_return = capital - self.initial_capital
        total_return_pct = (total_return / self.initial_capital) * 100
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        loss_rate = 100 - win_rate if total_trades > 0 else 0
        avg_win = sum(win_amounts) / len(win_amounts) if win_amounts else 0
        avg_loss = sum(loss_amounts) / len(loss_amounts) if loss_amounts else 0
        profit_factor = sum(win_amounts) / max(sum(loss_amounts), 0.01) if loss_amounts else (float('inf') if win_amounts else 0.0)
        profit_factor_display = round(profit_factor, 4) if profit_factor != float('inf') else 999999.0
        max_dd = self._max_drawdown(equity)
        sharpe = self._sharpe(self._daily_equity(trade_list, start_date, end_date))
        # Frontend expects many extra fields - provide 0/default when no trades to avoid NaN/undefined
        avg_profit_per_trade = (total_return / total_trades) if total_trades > 0 else 0
        max_win = max(win_amounts) if win_amounts else 0
        max_loss = max(loss_amounts) if loss_amounts else 0
        # Streaks
        max_win_streak = 0
        max_loss_streak = 0
        cur_ws = 0
        cur_ls = 0
        for tl in trade_list:
            if tl["pnl"] > 0:
                cur_ws += 1
                cur_ls = 0
                max_win_streak = max(max_win_streak, cur_ws)
            else:
                cur_ls += 1
                cur_ws = 0
                max_loss_streak = max(max_loss_streak, cur_ls)
        # Quantman Drawdown Days: days strategy stays in drawdown before new peak (blog 2025)
        max_dd_duration = 0
        max_trades_in_dd = 0
        if len(equity) > 1:
            peak = equity[0]
            dd_start = None
            for idx, val in enumerate(equity):
                if val > peak:
                    if dd_start is not None:
                        dur = idx - dd_start
                        max_dd_duration = max(max_dd_duration, dur)
                        max_trades_in_dd = max(max_trades_in_dd, dur)
                        dd_start = None
                    peak = val
                elif val < peak and dd_start is None:
                    dd_start = idx
            if dd_start is not None:
                dur = len(equity) - dd_start
                max_dd_duration = max(max_dd_duration, dur)
                max_trades_in_dd = max(max_trades_in_dd, dur)
            # also show in days (approx 1 trade ~1 day, Quantman counts calendar days)
            # max_dd_duration already in trades; for display same as days
        # Reward/risk, expectancy, return/maxDD
        reward_risk = (avg_win / max(avg_loss, 0.01)) if avg_loss > 0 else 0
        expectancy = (win_rate/100 * avg_win - (1-win_rate/100)*avg_loss) if total_trades>0 else 0
        return_maxdd = (total_return_pct / max(max_dd, 0.01)) if max_dd>0 else 0
        net_pnl = total_return
        monthly_pnl = {}
        for t in trade_list:
            m = t["entry_date"][:7]
            monthly_pnl[m] = monthly_pnl.get(m, 0) + t["pnl"]
        # Execution-realism audit (not a strategy filter): tells the user
        # whether this run could actually be reproduced by paper/live.
        _src_e, _src_x, _reasons = {}, {}, {}
        for t in trade_list:
            _src_e[str(t.get("entry_src") or "?")] = _src_e.get(str(t.get("entry_src") or "?"), 0) + 1
            _src_x[str(t.get("exit_src") or "?")] = _src_x.get(str(t.get("exit_src") or "?"), 0) + 1
            _reasons[str(t.get("exit_reason") or "?")] = _reasons.get(str(t.get("exit_reason") or "?"), 0) + 1
        _warn = []
        if self._used_intraday_bars and _src_e.get("db"):
            _warn.append("intraday bar par daily option print mila - engine build purana hai")
        if (not self._used_intraday_bars) and self._trade_mode == "intraday":
            _warn.append("daily bars + intraday trade_mode: entry 09:15 open par model hui hai, "
                         "10:05 ka exact fill backtest me simulate nahi ho sakta - intraday timeframe (5m/15m) chuno")
        _zero_hold = sum(1 for t in trade_list
                         if str(t.get("entry_date")) == str(t.get("exit_date"))
                         and str(t.get("entry_time")) == str(t.get("exit_time")))

        # ---- Option-Selling Risk Metrics (naked sell = unlimited risk) ----
        _is_naked_sell = False
        _max_profit_per_trade = 0.0   # premium received (capped profit for sell)
        _max_possible_loss_est = 0.0  # theoretical worst-case if spot gaps deep ITM
        _margin_estimate_rs = 0.0     # rough margin blocked (20-25% of notional)
        _gap_risk_warning = False
        _sell_trades = [t for t in trade_list if str(t.get("position", "")).lower() == "sell"]
        if _sell_trades:
            _is_naked_sell = True
            _spot_ref = 0.0
            try:
                # PURANA BUG: yahan `historical` undefined tha (NameError silently
                # catch hota tha) → _spot_ref hamesha 0 → naked-sell risk metrics
                # wrong fallback par bante the. Ab run() se real bars pass hote
                # hain (old callers ke liye None-safe).
                _hb = historical or []
                _spot_ref = float((_hb[-1] or {}).get("close_price", 0) or 0) if _hb else 0.0
            except Exception:
                pass
            for _t in _sell_trades:
                try:
                    _prem = float(_t.get("entry_price", 0) or 0)
                    _qty = int(_t.get("quantity", 0) or 0)
                    _strike = float(_t.get("strike", 0) or 0)
                    _opt = str(_t.get("option_type", "")).upper()
                    # Max profit = premium received (capped for option selling)
                    _max_profit_per_trade = max(_max_profit_per_trade, _prem * _qty)
                    # Max possible loss estimate: if spot goes deep ITM
                    # CE Sell: loss if spot >> strike; PE Sell: loss if spot << strike
                    if _opt == "CE" and _strike > _spot_ref and _spot_ref > 0:
                        _loss = (_strike - _spot_ref) * _qty  # per unit, approx
                    elif _opt == "PE" and _spot_ref > _strike and _spot_ref > 0:
                        _loss = (_spot_ref - _strike) * _qty
                    else:
                        _loss = _strike * 0.5 * _qty  # conservative fallback
                    _max_possible_loss_est = max(_max_possible_loss_est, _loss)
                    # Margin estimate: ~20% of notional for naked short (broker varies)
                    _notional = _strike * _qty if _strike > 0 else _prem * _qty * 10
                    _margin_estimate_rs = max(_margin_estimate_rs, _notional * 0.20)
                except Exception:
                    pass
            # Gap risk: if strategy holds overnight (positional or daily bars)
            if self._trade_mode != "intraday" or not self._used_intraday_bars:
                _gap_risk_warning = True
            # Also warn if any trade stayed overnight
            for _t in trade_list:
                try:
                    if str(_t.get("entry_date", "")) != str(_t.get("exit_date", "")):
                        _gap_risk_warning = True
                        break
                except Exception:
                    pass
        _warn_opt = []
        if _is_naked_sell:
            _warn_opt.append("NAKED SELL: single-leg short — unlimited loss possible. Spread/hedge लगाओ या चोटा साइज़ रखो।")
        if _gap_risk_warning:
            _warn_opt.append("गैप रिस्क: रात भर पोज़िशन carry करने पर गैप-अप/गैप-डाउन से SL breach हो सकता है।")
        if _max_possible_loss_est > 0:
            _warn_opt.append(f"सबसे बुरा अनुमानित नुकसान: ₹{_max_possible_loss_est:,.0f} प्रति ट्रेड (स्टॉक ज़ीरो पर जाने का सैद्धांतिक केस)।")
        _warn.extend(_warn_opt)
        # DATA HONESTY warning: agar koi bhi fill ₹5 fake fallback se aaya to
        # result 'REAL' nahi hai - UI ko batana zaroori (user rule 24-11).
        _fake_n = sum(1 for t in trade_list
                      if str(t.get("entry_src") or "") == "fake_fallback"
                      or str(t.get("exit_src") or "") == "fake_fallback")
        if _fake_n:
            _warn.append(f"⚠️ {_fake_n} trade(s) me premium kisi real/model source se nahi mila — ₹5 fallback use hua. Ye result REAL data ka nahi hai; NSE option sync + tick data check karo.")

        return {
            "success": True,
            "metrics": {
                "initial_capital": self.initial_capital,
                "final_capital": round(capital, 2),
                "total_return": round(total_return, 2),
                "total_return_pct": round(total_return_pct, 4),
                "win_rate": round(win_rate, 2),
                "loss_rate": round(loss_rate, 2),
                "max_drawdown": round(max_dd, 4),
                "profit_factor": profit_factor_display,
                "profit_factor_infinite": profit_factor == float('inf'),
                "sharpe_ratio": round(sharpe, 4),
                "total_trades": total_trades,
                "winning_trades": wins,
                "losing_trades": total_trades - wins,
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "avg_profit_per_trade": round(avg_profit_per_trade, 2),
                "net_pnl": round(net_pnl, 2),
                "max_win": round(max_win, 2),
                "max_loss": round(max_loss, 2),
                "max_profit_per_trade": round(_max_profit_per_trade, 2),
                "max_possible_loss_est": round(_max_possible_loss_est, 2),
                "margin_estimate_rs": round(_margin_estimate_rs, 2),
                "is_naked_sell": _is_naked_sell,
                "gap_risk_warning": _gap_risk_warning,
                "max_dd_duration": max_dd_duration,
                "max_trades_in_dd": max_trades_in_dd,
                "return_maxdd": round(return_maxdd, 2),
                "reward_risk": round(reward_risk, 2),
                "expectancy": round(expectancy, 2),
                "max_win_streak": max_win_streak,
                "max_loss_streak": max_loss_streak,
                "signals_evaluated": int(getattr(self, "_n_eval", 0) or 0),
                "signals_generated": int(getattr(self, "_n_sig", 0) or 0),
                "signals_buy": int(getattr(self, "_n_sig_buy", 0) or 0),
                "signals_sell": int(getattr(self, "_n_sig_sell", 0) or 0),
                "rejected": dict(getattr(self, "_rej", {}) or {}),
                "coverage": dict(getattr(self, "_cov", {}) or {}),
                "total_brokerage": round(total_brokerage, 2),
                "realism": {
                    "engine_version": ENGINE_VERSION,
                    "intraday_bars": bool(self._used_intraday_bars),
                    "trade_mode": self._trade_mode,
                    "entry_src_counts": _src_e,
                    "exit_src_counts": _src_x,
                    "exit_reason_counts": _reasons,
                    "zero_hold_trades": _zero_hold,
                    # ---- v6/v7 realism audit (backtest == paper ka proof) ----
                    "closed_candles_only": True,
                    "forming_bars_dropped": int(getattr(self, "_dropped_forming", 0) or 0),
                    "option_band_model": "delta-vega-free" if self._used_intraday_bars else "db-range",
                    "option_band_scale": float((getattr(self, "_advanced", {}) or {}).get("option_band_scale", 1.0) or 1.0),
                    "latency_slippage_pct": float(getattr(self, "_latency_pct", 0) or 0),
                    "latency_slippage_exit_pct": float(getattr(self, "_latency_pct_exit", 0) or 0),
                    "latency_s": float(getattr(self, "_latency_s", 0) or 0),
                    "fill_slippage_pct": float(getattr(self, "_slippage_pct", 0.5) or 0.5),
                    "sltp_source": f"sl={getattr(self, '_sl_unit', 'rs')}/{round(float(getattr(self, '_sl_amt', 0) or 0), 2)}"
                                   f" tp={getattr(self, '_tp_unit', 'rs')}/{round(float(getattr(self, '_tp_amt', 0) or 0), 2)}",
                    "real_iv_days": len(getattr(self, "_day_iv_cache", {}) or {}),
                    "premium_source_parity": ("broker-quote-first (is_live)" if bool(getattr(self, "is_live", False))
                                              else "model real-IV anchored (historical broker quotes DB me nahi hain)"),
                    "recommended_timeframe": "5m" if self._trade_mode == "intraday" else ("1d" if self._trade_mode == "positional" else "1d"),
                    # ---- v7 ENTRY-LATENCY audit (paper fill delay in bars) ----
                    "entry_latency_bars": int(getattr(self, "_entry_latency_bars", 1) or 1),
                    "entry_latency_note": "signal bar se N bars baad entry (paper broker fill delay; 5m TF me 1 bar = ~5 min)",
                    # ---- v7 strict-fill audit ----
                    # TP sirf candle CLOSE confirmation par (wick par nahi).
                    "tp_fill_mode": str(getattr(self, "_tp_fill_mode", "close")),
                    "tp_confirmed_by_close": int(getattr(self, "_n_tp_confirmed", 0) or 0),
                    "tp_wick_touches_ignored": int(getattr(self, "_n_tp_unconfirmed", 0) or 0),
                    "exit_latency_fills": int(getattr(self, "_n_exit_latency", 0) or 0),
                    "fake_premium_fills": _fake_n,
                    "tick_priced_fills": len(getattr(self, "_tick_prem_keys", set()) or ()),
                    "db_priced_fills": len(getattr(self, "_db_prem_keys", set()) or ()),
                    "warnings": _warn,
                },
                "equity_curve": equity,
                "trade_list": trade_list,
                "monthly_pnl": monthly_pnl,
                "source": "ratrade.quantitative.engine",
            },
        }

    def _max_drawdown(self, equity):
        if not equity:
            return 0
        peak = equity[0]
        max_dd = 0
        for v in equity:
            if v > peak:
                peak = v
            dd = (peak - v) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)
        return max_dd * 100

    def _daily_equity(self, trade_list, start_date="", end_date=""):
        """Equity marked on every WEEKDAY of the requested window.

        Flat days carry the previous capital, so return variance is measured
        on calendar trading days rather than on one point per trade (the old
        per-trade curve, multiplied by sqrt(252), annualised variance that
        happened to have n_trades samples with no sense of time decay)."""
        try:
            y1, m1, d1 = [int(x) for x in str(start_date)[:10].split("-")]
            y2, m2, d2 = [int(x) for x in str(end_date)[:10].split("-")]
            cur, end = datetime.date(y1, m1, d1), datetime.date(y2, m2, d2)
        except Exception:
            return [self.initial_capital] + [self.initial_capital + sum(
                float(t.get("pnl", 0) or 0) for t in (trade_list or []))]
        by_day = {}
        cap = self.initial_capital
        for t in trade_list or []:
            cap += float(t.get("pnl", 0) or 0)
            d = str(t.get("exit_date") or t.get("entry_date") or "")[:10]
            if d:
                by_day[d] = cap
        eq = [self.initial_capital]
        cur_cap = self.initial_capital
        while cur <= end:
            if cur.weekday() < 5:
                _d = cur.strftime("%Y-%m-%d")
                if _d in by_day:
                    cur_cap = by_day[_d]
                eq.append(cur_cap)
            cur += datetime.timedelta(days=1)
        return eq

    def _sharpe(self, equity):
        if len(equity) < 2:
            return 0
        returns = [(equity[i] - equity[i - 1]) / max(equity[i - 1], 1) for i in range(1, len(equity))]
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / max(len(returns) - 1, 1)
        std = math.sqrt(var)
        return (mean / std) * math.sqrt(252) if std > 0 else 0

    def _reset(self):
        self.ohlc_cache = {}
        self.bt_symbol = ""
        self.bt_expiry = ""
        self._explicit_expiry = False  # legs expiry_date = fixed contract (respect it)
        self._cur_bar_time = ""
        self._used_intraday_bars = False
        self._trailing_best = {}  # trade index -> best price seen (for trailing SL)
        self._prem_src_cache = {}
        # Data-honesty audit (24-11): kis premium ne real tick / DB / fake
        # fallback se aayi. Fake ₹5 fallback ko 'model' bolna galat tha.
        self._tick_prem_keys = set()
        self._db_prem_keys = set()
        self._fake_prem_keys = set()
