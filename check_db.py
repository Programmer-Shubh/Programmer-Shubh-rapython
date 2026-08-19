import sqlite3
conn = sqlite3.connect("D:/python web/data/ratrade.db")
conn.row_factory = sqlite3.Row
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
for t in tables:
    cnt = conn.execute(f"SELECT COUNT(*) FROM {t['name']}").fetchone()[0]
    print(f"{t['name']}: {cnt} rows")
symbols = conn.execute("SELECT DISTINCT symbol FROM bhavcopy_data LIMIT 10").fetchall()
print("Symbols:", [s[0] for s in symbols])
dates = conn.execute("SELECT MIN(trade_date), MAX(trade_date) FROM bhavcopy_data").fetchone()
print(f"Date range: {dates[0]} to {dates[1]}")
ce = conn.execute("SELECT COUNT(*) FROM bhavcopy_data WHERE option_type='CE'").fetchone()[0]
pe = conn.execute("SELECT COUNT(*) FROM bhavcopy_data WHERE option_type='PE'").fetchone()[0]
spot = conn.execute("SELECT COUNT(*) FROM bhavcopy_data WHERE option_type IS NULL").fetchone()[0]
print(f"CE: {ce}, PE: {pe}, Spot: {spot}")
conn.close()
