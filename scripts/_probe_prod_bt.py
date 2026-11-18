"""Powershell-side probe: call prod /api/backtest/run-async with a config.
Reads body from scripts/_probe_prod_body.json."""
import sys, json, time, urllib.request

HOST = "https://ratrade-iwi2.onrender.com"

def main():
    cfg = json.load(open(sys.argv[1]))
    body = json.dumps(cfg).encode()
    req = urllib.request.Request(HOST + "/api/backtest/run-async", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    r = json.load(urllib.request.urlopen(req, timeout=60))
    print("start:", r)
    jid = r.get("job_id")
    t0 = time.time()
    while time.time() - t0 < 280:
        time.sleep(3)
        rr = json.load(urllib.request.urlopen(
            urllib.request.Request(f"{HOST}/api/backtest/result/{jid}"), timeout=30))
        st = rr.get("status")
        if st == "done":
            res = rr.get("result") or {}
            m = res.get("metrics") or {}
            per = res.get("per_symbol") or {}
            cov = (per.get(list(per.keys())[0], {}) if per else {}).get("data") or {}
            print("DONE took_ms=", res.get("took_ms"))
            print("trades=", m.get("total_trades"), "wr=", m.get("win_rate"),
                  "net=", m.get("net_pnl"), "pf=", m.get("profit_factor"),
                  "dd=", m.get("max_drawdown"))
            print("coverage:", cov)
            print("why_zero:", (per.get(list(per.keys())[0], {}) if per else {}).get("why_zero"))
            print("realism:", json.dumps((per.get("NIFTY", {}) or {}).get("realism", {}), indent=1)[:600])
            return
        if st == "error":
            print("ERROR job:", rr)
            return
    print("TIMEOUT waiting for job")

if __name__ == "__main__":
    main()
