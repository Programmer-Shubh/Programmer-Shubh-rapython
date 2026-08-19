import streamlit as st
from core.models.bhavcopy_model import BhavcopyModel
from core.models.trade_model import TradeModel
from core.services.live_market_data import LiveMarketData
from core.services.transaction_costs import TransactionCosts
from utils.helpers import get_lot_size, get_strike_step


def render_option_chain(db):
    st.title("🔗 Option Chain")
    bhav = BhavcopyModel()
    live = LiveMarketData()

    symbols = ["NIFTY", "BANKNIFTY", "FINNIFTY"]
    col1, col2, col3 = st.columns(3)
    with col1:
        symbol = st.selectbox("Symbol", symbols)
    dates = bhav.get_dates(symbol)
    with col2:
        date = st.selectbox("Date", dates[:30] if dates else ["No Data"])
    expiries = bhav.get_expiries(symbol, date) if dates else []
    with col3:
        expiry = st.selectbox("Expiry", expiries if expiries else ["No Expiry"])

    if not dates:
        st.warning("Import Bhavcopy data first.")
        return

    chain = bhav.get_option_chain(symbol, date, expiry)
    spot = live.get_spot_price(symbol)

    if not chain:
        st.info("No option data for this date/expiry.")
        return

    ce_data = {r["strike_price"]: r for r in chain if r.get("option_type") == "CE"}
    pe_data = {r["strike_price"]: r for r in chain if r.get("option_type") == "PE"}
    all_strikes = sorted(set(list(ce_data.keys()) + list(pe_data.keys())))

    step = get_strike_step(symbol)
    atm = round(spot / step) * step if spot > 0 else all_strikes[len(all_strikes) // 2]

    st.subheader(f"Spot: ₹{spot:,.2f} | ATM: {atm} | {symbol} Option Chain")

    rows = []
    for strike in all_strikes:
        ce = ce_data.get(strike, {})
        pe = pe_data.get(strike, {})
        distance = strike - atm
        rows.append({
            "CE_LTP": ce.get("close_price", 0),
            "CE_OI": ce.get("oi", 0),
            "CE_Vol": ce.get("volume", 0),
            "Strike": strike,
            "Distance": f"{'+'if distance>0 else ''}{int(distance)}",
            "PE_LTP": pe.get("close_price", 0),
            "PE_OI": pe.get("oi", 0),
            "PE_Vol": pe.get("volume", 0),
        })

    df = st.dataframe(
        rows,
        column_config={
            "CE_LTP": st.column_config.NumberColumn("CE LTP", format="₹%.2f"),
            "CE_OI": st.column_config.NumberColumn("CE OI"),
            "CE_Vol": st.column_config.NumberColumn("CE Volume"),
            "Strike": st.column_config.NumberColumn("Strike", format="%d"),
            "Distance": "Dist",
            "PE_LTP": st.column_config.NumberColumn("PE LTP", format="₹%.2f"),
            "PE_OI": st.column_config.NumberColumn("PE OI"),
            "PE_Vol": st.column_config.NumberColumn("PE Volume"),
        },
        use_container_width=True,
        hide_index=True,
    )

    st.markdown("---")
    st.subheader("Place Paper Trade")
    trade_model = TradeModel()

    with st.form("trade_form"):
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            trade_strike = st.selectbox("Strike", all_strikes)
        with c2:
            trade_type = st.selectbox("Type", ["CE", "PE"])
        with c3:
            txn = st.selectbox("Action", ["BUY", "SELL"])
        with c4:
            qty = st.number_input("Lots", min_value=1, value=1)

        sl = st.number_input("Stop Loss (₹)", value=500.0, step=50.0)
        tp = st.number_input("Take Profit (₹)", value=1000.0, step=50.0)
        submitted = st.form_submit_button("🚀 Place Trade")

        if submitted:
            chain_row = ce_data.get(trade_strike) if trade_type == "CE" else pe_data.get(trade_strike)
            premium = chain_row.get("close_price", 0) if chain_row else 0
            if premium <= 0:
                premium = live.get_option_ltp(symbol, trade_strike, trade_type) or 0
            if premium <= 0:
                st.error("No premium data for this strike.")
                return
            premium = TransactionCosts.apply_fill_slippage(premium, txn)
            lot_size = get_lot_size(symbol)
            costs = TransactionCosts.calculate(premium * qty * lot_size, txn == "SELL")
            trade_id = trade_model.insert_trade({
                "symbol": symbol, "option_type": trade_type, "strike_price": trade_strike,
                "expiry_date": expiry, "transaction_type": txn, "quantity": qty,
                "lot_size": lot_size, "entry_price": premium, "stop_loss": sl,
                "target": tp, "total_cost": costs["total"], "entry_date": date,
            })
            st.success(f"Trade placed! ID: {trade_id} | Entry: ₹{premium:.2f}")
