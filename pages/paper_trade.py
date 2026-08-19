import streamlit as st
from core.models.trade_model import TradeModel
from core.services.transaction_costs import TransactionCosts
from utils.helpers import get_lot_size, format_currency


def render_paper_trade(db):
    st.title("💹 Paper Trade")
    trade_model = TradeModel()

    tab1, tab2, tab3 = st.tabs(["Open Positions", "Place Trade", "History"])

    with tab1:
        positions = trade_model.get_open_positions_with_pnl()
        if positions:
            total_pnl = sum(p["unrealized_pnl"] for p in positions)
            st.metric("Unrealized P&L", format_currency(total_pnl))

            for p in positions:
                t = p["trade"]
                with st.expander(f"#{t['id']} | {t['symbol']} {t['option_type']} {t['strike_price']} | {t['transaction_type']}"):
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Entry", f"₹{t['entry_price']:,.2f}")
                    c2.metric("Current", f"₹{p['current_price']:,.2f}")
                    c3.metric("P&L", format_currency(p["unrealized_pnl"]))
                    c4.metric("SL/TP", f"₹{t['stop_loss']:.0f} / ₹{t['target']:.0f}")

                    with st.form(f"manage_{t['id']}"):
                        mc1, mc2, mc3 = st.columns(3)
                        new_sl = mc1.number_input("SL", value=float(t["stop_loss"]), key=f"sl_{t['id']}")
                        new_tp = mc2.number_input("TP", value=float(t["target"]), key=f"tp_{t['id']}")
                        if mc3.form_submit_button("Update"):
                            trade_model.update_management(t["id"], new_sl, new_tp, t["auto_action"])
                            st.success("Updated!")
                        if st.form_submit_button("Close Trade"):
                            exit_p = st.number_input("Exit Price", value=p["current_price"], key=f"ex_{t['id']}")
                            trade_model.close_trade(t["id"], exit_p, st.date_input("Date", key=f"dt_{t['id']}").strftime("%Y-%m-%d"))
                            st.success("Trade closed!")
                            st.rerun()
        else:
            st.info("No open positions.")

    with tab2:
        with st.form("new_trade"):
            c1, c2 = st.columns(2)
            symbol = c1.selectbox("Symbol", ["NIFTY", "BANKNIFTY", "FINNIFTY"])
            option_type = c2.selectbox("Option", ["CE", "PE"])
            c3, c4, c5 = st.columns(3)
            strike = c3.number_input("Strike", value=0, step=50)
            premium = c4.number_input("Entry Price", value=0.0, step=0.5)
            lots = c5.number_input("Lots", value=1, min_value=1)
            c6, c7 = st.columns(2)
            sl = c6.number_input("Stop Loss (₹)", value=500.0)
            tp = c7.number_input("Take Profit (₹)", value=1000.0)
            txn = st.selectbox("Transaction", ["BUY", "SELL"])

            if st.form_submit_button("🚀 Place Trade"):
                if premium <= 0 or strike <= 0:
                    st.error("Enter valid strike and premium.")
                else:
                    adj_premium = TransactionCosts.apply_fill_slippage(premium, txn)
                    lot_size = get_lot_size(symbol)
                    costs = TransactionCosts.calculate(adj_premium * lots * lot_size, txn == "SELL")
                    tid = trade_model.insert_trade({
                        "symbol": symbol, "option_type": option_type, "strike_price": strike,
                        "transaction_type": txn, "quantity": lots, "lot_size": lot_size,
                        "entry_price": adj_premium, "stop_loss": sl, "target": tp,
                        "total_cost": costs["total"], "entry_date": st.date_input("Date").strftime("%Y-%m-%d"),
                    })
                    st.success(f"Trade #{tid} placed at ₹{adj_premium:.2f}")

    with tab3:
        closed = trade_model.get_closed_trades()
        if closed:
            st.metric("Total P&L", format_currency(sum(t["pnl"] for t in closed)))
            st.dataframe(
                [{"#": t["id"], "Date": t["entry_date"], "Sym": t["symbol"],
                  "Type": t["option_type"], "Strike": t["strike_price"],
                  "Entry": f"₹{t['entry_price']:.2f}", "Exit": f"₹{t.get('exit_price', 0):.2f}",
                  "P&L": format_currency(t["pnl"])} for t in closed[:30]],
                use_container_width=True,
            )
        else:
            st.info("No closed trades.")
