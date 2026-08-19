import streamlit as st
import pandas as pd
import sqlite3
import json
from datetime import datetime, timedelta
import os

from core.services.backtest_engine import BacktestEngine
from core.services.indicator_engine import IndicatorEngine
from core.services.transaction_costs import TransactionCosts
from core.services.live_market_data import LiveMarketData
from core.models.database import Database
from core.models.trade_model import TradeModel
from core.models.bhavcopy_model import BhavcopyModel
from utils.helpers import get_lot_size, get_strike_step

st.set_page_config(
    page_title="RaTrade - Professional Trading Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .stMetric { background: #1a1a2e; padding: 10px; border-radius: 8px; border: 1px solid #333; }
    .profit { color: #00FF7F !important; }
    .loss { color: #FF4444 !important; }
    div[data-testid="stSidebar"] { background: #0a0a1a; }
    .stButton>button { background: #00FF7F; color: #000; font-weight: bold; border: none; }
    .stButton>button:hover { background: #00CC66; }
</style>
""", unsafe_allow_html=True)

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "ratrade.db")
Database.set_path(DB_PATH)
db = Database.get_instance()
db.init_schema()

st.sidebar.title("📊 RaTrade")
st.sidebar.markdown("---")

page = st.sidebar.radio(
    "Navigate",
    ["Dashboard", "Option Chain", "Paper Trade", "Strategy Builder", "Bhavcopy Import", "Scanner"]
)

st.sidebar.markdown("---")
st.sidebar.caption(f"🕐 {datetime.now().strftime('%d %b %Y %H:%M:%S')}")

if page == "Dashboard":
    from pages.dashboard import render_dashboard
    render_dashboard(db)
elif page == "Option Chain":
    from pages.option_chain import render_option_chain
    render_option_chain(db)
elif page == "Paper Trade":
    from pages.paper_trade import render_paper_trade
    render_paper_trade(db)
elif page == "Strategy Builder":
    from pages.strategy_builder import render_strategy_builder
    render_strategy_builder(db)
elif page == "Bhavcopy Import":
    from pages.bhavcopy_import import render_bhavcopy
    render_bhavcopy(db)
elif page == "Scanner":
    from pages.scanner import render_scanner
    render_scanner(db)
