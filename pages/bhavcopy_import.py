import streamlit as st
import pandas as pd
from core.models.bhavcopy_model import BhavcopyModel


def render_bhavcopy(db):
    st.title("📥 Bhavcopy Import")
    bhav = BhavcopyModel()

    tab1, tab2 = st.tabs(["Import CSV", "View Data"])

    with tab1:
        st.markdown("""
        ### Import Format
        CSV columns: `symbol, trade_date, expiry_date, strike_price, option_type, open_price, high_price, low_price, close_price, volume, oi`
        """)
        uploaded = st.file_uploader("Upload Bhavcopy CSV", type=["csv"])
        if uploaded:
            df = pd.read_csv(uploaded)
            st.dataframe(df.head(10))
            if st.button("Import Data"):
                records = df.to_dict("records")
                count = bhav.import_data(records)
                st.success(f"Imported {count} records!")

        st.markdown("---")
        st.subheader("Quick Import from NSE")
        symbol = st.selectbox("Symbol", ["NIFTY", "BANKNIFTY", "FINNIFTY"], key="imp_sym")
        imp_date = st.date_input("Date", key="imp_date")
        if st.button("Fetch & Import"):
            with st.spinner("Fetching from NSE..."):
                try:
                    import requests
                    url = f"https://archives.nseindia.com/content/historical/DERIVATIVES/{imp_date.year}/{imp_date.strftime('%b')}/fo{imp_date.strftime('%d%b%Y').upper()}data{symbol}CMS.csv"
                    resp = requests.get(url, timeout=30)
                    if resp.status_code == 200:
                        df = pd.read_csv(pd.io.common.StringIO(resp.text))
                        records = []
                        for _, row in df.iterrows():
                            records.append({
                                "symbol": symbol,
                                "trade_date": imp_date.strftime("%Y-%m-%d"),
                                "expiry_date": str(row.get("Expiry", "")),
                                "strike_price": float(row.get("Strike Price", 0)),
                                "option_type": "CE" if "CE" in str(row.get("Option Type", "")) else "PE",
                                "open_price": float(row.get("Open", 0)),
                                "high_price": float(row.get("High", 0)),
                                "low_price": float(row.get("Low", 0)),
                                "close_price": float(row.get("Close", 0)),
                                "volume": int(row.get("Contracts", 0)),
                                "oi": int(row.get("Open Int", 0)),
                            })
                        count = bhav.import_data(records)
                        st.success(f"Imported {count} records!")
                    else:
                        st.error(f"Failed to fetch: {resp.status_code}")
                except Exception as e:
                    st.error(f"Error: {e}")

    with tab2:
        symbols = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]
        sym = st.selectbox("Symbol", symbols, key="view_sym")
        dates = bhav.get_dates(sym)
        if dates:
            st.info(f"Available dates: {len(dates)} | Range: {dates[-1]} to {dates[0]}")
            sel_date = st.selectbox("Select Date", dates[:30])
            expiries = bhav.get_expiries(sym, sel_date)
            if expiries:
                sel_exp = st.selectbox("Expiry", expiries)
                chain = bhav.get_option_chain(sym, sel_date, sel_exp)
                if chain:
                    st.dataframe(
                        [{"Strike": r["strike_price"], "Type": r["option_type"],
                          "Open": r["open_price"], "High": r["high_price"],
                          "Low": r["low_price"], "Close": r["close_price"],
                          "Vol": r.get("volume", 0)} for r in chain[:50]],
                        use_container_width=True,
                    )
        else:
            st.warning("No data for this symbol.")
