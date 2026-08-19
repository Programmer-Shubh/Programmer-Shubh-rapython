import streamlit as st
import plotly.graph_objects as go
from core.models.bhavcopy_model import BhavcopyModel
from core.services.live_market_data import LiveMarketData
from utils.helpers import get_lot_size, format_currency


def render_dashboard(db):
    st.title("📊 Dashboard")
    bhav = BhavcopyModel()
    live = LiveMarketData()

    symbols = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]
    cols = st.columns(len(symbols))
    for idx, sym in enumerate(symbols):
        with cols[idx]:
            spot = live.get_spot_price(sym)
            st.metric(label=sym, value=f"₹{spot:,.2f}" if spot > 0 else "No Data")

    st.markdown("---")

    tab1, tab2, tab3 = st.tabs(["Market Overview", "Portfolio", "Trade History"])

    with tab1:
        symbol = st.selectbox("Select Symbol", symbols, key="dash_sym")
        dates = bhav.get_dates(symbol)
        if dates:
            latest = dates[0]
            chain = bhav.get_option_chain(symbol, latest, bhav.get_expiries(symbol, latest)[0] if bhav.get_expiries(symbol, latest) else "")
            if chain:
                ce_data = [r for r in chain if r.get("option_type") == "CE"]
                pe_data = [r for r in chain if r.get("option_type") == "PE"]
                st.subheader(f"Option Chain - {symbol} ({latest})")
                col1, col2 = st.columns(2)
                with col1:
                    st.caption("CE (Call)")
                    st.dataframe(
                        [{"Strike": r["strike_price"], "LTP": r["close_price"], "OI": r.get("oi", 0), "Vol": r.get("volume", 0)} for r in sorted(ce_data, key=lambda x: x["strike_price"], reverse=True)[:15]],
                        use_container_width=True,
                    )
                with col2:
                    st.caption("PE (Put)")
                    st.dataframe(
                        [{"Strike": r["strike_price"], "LTP": r["close_price"], "OI": r.get("oi", 0), "Vol": r.get("volume", 0)} for r in sorted(pe_data, key=lambda x: x["strike_price"])[:15]],
                        use_container_width=True,
                    )
            else:
                st.info("No option chain data for this date.")
        else:
            st.warning("No data imported yet. Go to Bhavcopy Import.")

    with tab2:
        trade_model = __import__("core.models.trade_model", fromlist=["TradeModel"]).TradeModel()
        positions = trade_model.get_open_positions_with_pnl()
        if positions:
            total_pnl = sum(p["unrealized_pnl"] for p in positions)
            st.metric("Total Unrealized P&L", format_currency(total_pnl))
            for p in positions:
                t = p["trade"]
                with st.expander(f"{t['symbol']} {t['option_type']} {t['strike_price']} - {t['transaction_type']}"):
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Entry", f"₹{t['entry_price']:,.2f}")
                    c2.metric("Current", f"₹{p['current_price']:,.2f}")
                    c3.metric("P&L", format_currency(p["unrealized_pnl"]))
                    c4.metric("Status", t["status"])
        else:
            st.info("No open positions.")

    with tab3:
        closed = trade_model.get_closed_trades()
        if closed:
            st.dataframe(
                [{"Date": t["entry_date"], "Symbol": t["symbol"], "Type": t["option_type"],
                  "Strike": t["strike_price"], "Entry": t["entry_price"], "Exit": t.get("exit_price", "-"),
                  "P&L": format_currency(t["pnl"])} for t in closed[:20]],
                use_container_width=True,
            )
        else:
            st.info("No closed trades yet.")
