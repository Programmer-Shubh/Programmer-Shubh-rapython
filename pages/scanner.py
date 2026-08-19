import streamlit as st
import plotly.graph_objects as go
from core.models.bhavcopy_model import BhavcopyModel
from core.services.live_market_data import LiveMarketData
from core.services.indicator_engine import IndicatorEngine
from utils.helpers import get_strike_step


def render_scanner(db):
    st.title("🔍 Scanner")
    bhav = BhavcopyModel()
    live = LiveMarketData()
    ind_engine = IndicatorEngine()

    tab1, tab2, tab3 = st.tabs(["VWAP Scanner", "Breakout Scanner", "OI Analysis"])

    with tab1:
        st.subheader("VWAP Scanner")
        symbol = st.selectbox("Symbol", ["NIFTY", "BANKNIFTY", "FINNIFTY"], key="scan_sym")
        dates = bhav.get_dates(symbol)
        if not dates:
            st.warning("Import data first.")
            return
        date = st.selectbox("Date", dates[:20], key="scan_date")
        expiries = bhav.get_expiries(symbol, date)
        if not expiries:
            st.info("No option data for this date.")
            return
        expiry = st.selectbox("Expiry", expiries, key="scan_exp")

        chain = bhav.get_option_chain(symbol, date, expiry)
        if not chain:
            st.info("No chain data.")
            return

        spot = live.get_spot_price(symbol)
        step = get_strike_step(symbol)
        atm = round(spot / step) * step if spot > 0 else 0

        results = []
        ce_data = [r for r in chain if r["option_type"] == "CE"]
        for r in sorted(ce_data, key=lambda x: x["strike_price"]):
            strike = r["strike_price"]
            if strike < atm:
                continue
            oi = r.get("oi", 0)
            vol = r.get("volume", 0)
            ltp = r.get("close_price", 0)
            oi_change = oi
            signal = "NEUTRAL"
            if vol > 1000 and oi > 5000:
                signal = "BULLISH" if strike > atm else "BEARISH"
            elif vol > 500:
                signal = "WATCH"
            results.append({
                "Strike": strike, "LTP": ltp, "OI": oi, "Volume": vol,
                "Signal": signal, "Distance": f"+{int(strike - atm)}",
            })

        if results:
            st.dataframe(results, use_container_width=True)
        else:
            st.info("No signals found.")

    with tab2:
        st.subheader("Breakout Scanner")
        sym2 = st.selectbox("Symbol", ["NIFTY", "BANKNIFTY"], key="brk_sym")
        dates2 = bhav.get_dates(sym2)
        if dates2:
            lookback = st.slider("Lookback Days", 5, 30, 14)
            recent_dates = dates2[:lookback]
            closes = []
            for d in reversed(recent_dates):
                exps = bhav.get_expiries(sym2, d)
                if exps:
                    chain = bhav.get_option_chain(sym2, d, exps[0])
                    ce_atm = [r for r in chain if r["option_type"] == "CE"]
                    if ce_atm:
                        closes.append({"date": d, "close": ce_atm[len(ce_atm)//2].get("close_price", 0)})

            if closes:
                prices = [c["close"] for c in closes]
                rsi = ind_engine.calculate_rsi(prices)
                if rsi[-1] > 70:
                    st.warning(f"RSI Overbought: {rsi[-1]:.1f}")
                elif rsi[-1] < 30:
                    st.success(f"RSI Oversold: {rsi[-1]:.1f}")
                else:
                    st.info(f"RSI: {rsi[-1]:.1f}")

                fig = go.Figure()
                fig.add_trace(go.Scatter(x=[c["date"] for c in closes], y=prices, mode="lines+markers"))
                fig.update_layout(title=f"{sym2} Price Trend", template="plotly_dark", height=300)
                st.plotly_chart(fig, use_container_width=True)

    with tab3:
        st.subheader("OI Analysis")
        sym3 = st.selectbox("Symbol", ["NIFTY", "BANKNIFTY"], key="oi_sym")
        dates3 = bhav.get_dates(sym3)
        if dates3:
            date3 = st.selectbox("Date", dates3[:10], key="oi_date")
            exps = bhav.get_expiries(sym3, date3)
            if exps:
                exp3 = st.selectbox("Expiry", exps, key="oi_exp")
                chain = bhav.get_option_chain(sym3, date3, exp3)
                ce = sorted([r for r in chain if r["option_type"] == "CE"], key=lambda x: x.get("oi", 0), reverse=True)[:10]
                pe = sorted([r for r in chain if r["option_type"] == "PE"], key=lambda x: x.get("oi", 0), reverse=True)[:10]

                c1, c2 = st.columns(2)
                with c1:
                    st.caption("Top CE OI")
                    st.dataframe([{"Strike": r["strike_price"], "OI": r.get("oi", 0), "LTP": r["close_price"]} for r in ce])
                with c2:
                    st.caption("Top PE OI")
                    st.dataframe([{"Strike": r["strike_price"], "OI": r.get("oi", 0), "LTP": r["close_price"]} for r in pe])

                total_ce_oi = sum(r.get("oi", 0) for r in chain if r["option_type"] == "CE")
                total_pe_oi = sum(r.get("oi", 0) for r in chain if r["option_type"] == "PE")
                ratio = total_pe_oi / max(total_ce_oi, 1)
                st.metric("PCR (Put/Call Ratio)", f"{ratio:.2f}")
