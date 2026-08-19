import streamlit as st
import plotly.graph_objects as go
import json
from datetime import date, timedelta
from core.models.database import Database
from core.models.bhavcopy_model import BhavcopyModel
from core.services.backtest_engine import BacktestEngine
from utils.helpers import format_currency


def render_strategy_builder(db):
    st.title("🧠 Strategy Builder")
    bhav = BhavcopyModel()

    with st.sidebar:
        st.header("Strategy Setup")
        strat_name = st.text_input("Strategy Name", "My Strategy")
        symbol = st.selectbox("Symbol", ["NIFTY", "BANKNIFTY", "FINNIFTY"])
        c1, c2 = st.columns(2)
        start = c1.date_input("Start Date", date.today() - timedelta(days=180))
        end = c2.date_input("End Date", date.today())
        timeframe = st.selectbox("Timeframe", ["daily", "15m", "5m"])

        st.header("Indicators")
        indicators = []
        if st.checkbox("Predicted MA", value=True):
            pma_lb = st.slider("PMA Lookback", 10, 50, 20, key="pma_lb")
            indicators.append({"id": "predicted_moving_average", "params": {"lookback": pma_lb, "forward_bars": 5}})
        if st.checkbox("AI Sentiment"):
            asi_lb = st.slider("Sentiment Lookback", 10, 50, 20, key="asi_lb")
            indicators.append({"id": "ai_sentiment", "params": {"lookback": asi_lb}})
        if st.checkbox("AI Volatility"):
            indicators.append({"id": "ai_volatility_range", "params": {"lookback": 20}})
        if st.checkbox("AI Trend Score"):
            indicators.append({"id": "ai_trend_score", "params": {}})
        if st.checkbox("RSI"):
            rsi_p = st.slider("RSI Period", 5, 30, 14, key="rsi_p")
            indicators.append({"id": "rsi", "params": {"period": rsi_p}})
        if st.checkbox("MACD"):
            indicators.append({"id": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}})

        st.header("Entry Conditions")
        entry_indicator = st.selectbox("Indicator", ["close", "ai_sentiment", "predicted_moving_average", "ai_trend_score"], key="entry_ind")
        entry_op = st.selectbox("Operator", ["greater_than", "less_than", "crosses_above"], key="entry_op")
        entry_val = st.number_input("Value", value=0.0, step=1.0, key="entry_val")

        st.header("Risk Management")
        option_type = st.selectbox("Option Type", ["CE", "PE"])
        transaction = st.selectbox("Transaction", ["buy", "sell"])
        strike_sel = st.selectbox("Strike", ["otm", "atm", "itm", "delta"])
        sl = st.number_input("Stop Loss (₹)", value=500.0, step=50.0)
        tp = st.number_input("Take Profit (₹)", value=1000.0, step=50.0)
        lots = st.number_input("Lots", value=1, min_value=1)
        max_hold = st.slider("Max Holding (bars)", 5, 50, 20)
        trade_mode = st.selectbox("Trade Mode", ["positional", "intraday"])
        max_trades = st.slider("Max Trades/Day", 1, 10, 5)

    run = st.button("🚀 Run Backtest", type="primary", use_container_width=True)

    if run:
        entry_conditions = [{"indicator": entry_indicator, "operator": entry_op, "value": str(entry_val)}]
        legs = [{"option_type": option_type, "lots": lots, "transaction": transaction, "stop_loss": sl, "take_profit": tp}]
        advanced = {
            "trailing_sl": False, "max_holding_bars": max_hold,
            "strike_selection": strike_sel, "otm_distance": 2,
            "delta_target": None, "trade_mode": trade_mode,
            "implied_volatility": 0.14, "days_to_expiry": 15,
        }
        risk = {"max_trades_per_day": max_trades, "daily_stop_loss": sl, "daily_take_profit": tp}

        with st.spinner("Running backtest..."):
            historical = bhav.get_by_symbol(symbol, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), True)
            if not historical:
                st.error(f"No data for {symbol} between {start} and {end}")
                return
            if len(historical) > 120:
                historical = historical[-120:]
                st.warning("Limited to last 120 bars for performance.")

            engine = BacktestEngine()
            result = engine.run(
                historical, symbol, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
                indicators, entry_conditions, [], legs, advanced, risk,
            )

        if not result.get("success"):
            st.error(result.get("error", "Backtest failed"))
            return

        m = result["metrics"]
        st.success("Backtest Complete!")

        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Trades", m["total_trades"])
        c2.metric("Win Rate", f"{m['win_rate']}%")
        c3.metric("Return", f"{m['total_return_pct']}%")
        c4.metric("Max DD", f"{m['max_drawdown']}%")
        c5.metric("Sharpe", f"{m['sharpe_ratio']}")
        c6.metric("Profit Factor", f"{m['profit_factor']}")

        col_a, col_b = st.columns(2)
        with col_a:
            if m["equity_curve"]:
                fig = go.Figure()
                fig.add_trace(go.Scatter(y=m["equity_curve"], mode="lines", name="Equity", line=dict(color="#00FF7F", width=2)))
                fig.update_layout(title="Equity Curve", template="plotly_dark", height=300)
                st.plotly_chart(fig, use_container_width=True)
        with col_b:
            if m.get("monthly_pnl"):
                months = list(m["monthly_pnl"].keys())
                values = list(m["monthly_pnl"].values())
                fig = go.Figure()
                fig.add_trace(go.Bar(x=months, y=values, marker_color=["#00FF7F" if v >= 0 else "#FF4444" for v in values]))
                fig.update_layout(title="Monthly P&L", template="plotly_dark", height=300)
                st.plotly_chart(fig, use_container_width=True)

        if m.get("trade_list"):
            st.subheader("Trade List")
            st.dataframe(
                [{"Entry": t["entry"]["date"], "Exit": t["exit"]["date"] if t.get("exit") else "-",
                  "Strike": t["entry"]["strike"], "Entry ₹": t["entry"]["price"],
                  "Exit ₹": t["exit"]["price"] if t.get("exit") else "-",
                  "P&L": format_currency(t["pnl"])} for t in m["trade_list"]],
                use_container_width=True,
            )
