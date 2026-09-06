"""Secure sandboxed runner for user-supplied custom Python trading strategies.

Security layers (defense in depth):
1. Static AST scan - blocks dangerous imports/names/attributes before anything runs.
2. Subprocess isolation - user code runs in a separate short-lived process with a
   hard timeout, so infinite loops / hangs can never block the web server.
3. Guarded ``__import__`` inside the child - only allow-listed modules importable
   at runtime even if the static scan is bypassed.
4. Restricted builtins - file/network/system builtins (open, eval, exec, compile,
   input, ...) are removed from the user namespace.
5. Output contract - only a JSON-serializable dict of trades/equity comes back.

Allowed libraries: pandas, numpy, backtrader (+ stdlib math helpers).
Historical data is passed in as a standard pandas DataFrame with columns:
date, open, high, low, close, volume.
"""
import ast
import json
import math
import os
import subprocess
import sys
import tempfile

MAX_CODE_CHARS = 30000
EXEC_TIMEOUT_SEC = 30
MAX_TRADES = 5000

ALLOWED_MODULES = {
    "pandas", "numpy", "backtrader",
    "math", "statistics", "datetime", "collections",
    "itertools", "functools", "operator", "re", "json",
}

BLOCKED_NAMES = {
    "__import__", "eval", "exec", "compile", "open", "input",
    "globals", "locals", "vars", "dir", "memoryview",
    "setattr", "delattr", "breakpoint", "exit", "quit",
    "os", "sys", "subprocess", "socket", "pathlib", "shutil",
    "requests", "urllib", "http", "inspect", "ctypes", "importlib",
    "threading", "multiprocessing", "concurrent", "signal",
    "pty", "tty", "glob", "pickle", "marshal", "shelve",
    "sqlite3", "builtins",
}

TEMPLATE_CODE = '''"""Custom strategy example (pandas mode).

`data` is a pandas DataFrame with columns:
date, open, high, low, close, volume  (oldest -> newest)

Define run(data) and return a dict with a "trades" list.
Each trade: entry_date, exit_date, entry_price, exit_price, qty (default 1).
"""
import pandas as pd


def run(data: pd.DataFrame):
    fast = data["close"].rolling(10).mean()
    slow = data["close"].rolling(30).mean()
    trades = []
    open_trade = None
    for i in range(30, len(data)):
        row = data.iloc[i]
        prev_fast, prev_slow = fast.iloc[i - 1], slow.iloc[i - 1]
        if open_trade is None:
            if fast.iloc[i] > slow.iloc[i] and prev_fast <= prev_slow:
                open_trade = {"entry_date": str(row["date"]), "entry_price": float(row["close"])}
        else:
            if fast.iloc[i] < slow.iloc[i] and prev_fast >= prev_slow:
                open_trade.update({"exit_date": str(row["date"]),
                                   "exit_price": float(row["close"]), "qty": 1})
                trades.append(open_trade)
                open_trade = None
    # Square off any open position at the last close
    if open_trade is not None:
        last = data.iloc[-1]
        open_trade.update({"exit_date": str(last["date"]),
                           "exit_price": float(last["close"]), "qty": 1})
        trades.append(open_trade)
    return {"trades": trades}
'''


def validate_code(code: str) -> str | None:
    """Return an error message if code is unsafe, else None."""
    if not code or not code.strip():
        return "Code is empty."
    if len(code) > MAX_CODE_CHARS:
        return f"Code too large ({len(code)} chars, max {MAX_CODE_CHARS})."
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"Syntax error on line {e.lineno}: {e.msg}"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import,)):
            for a in node.names:
                root = (a.name or "").split(".")[0]
                if root not in ALLOWED_MODULES:
                    return f"Import blocked: '{a.name}'. Allowed: pandas, numpy, backtrader, math, statistics, datetime, collections, itertools, functools, operator, re, json."
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root not in ALLOWED_MODULES:
                return f"Import blocked: 'from {node.module} ...'. Allowed: pandas, numpy, backtrader, math, statistics, datetime, collections, itertools, functools, operator, re, json."
        elif isinstance(node, ast.Name):
            if node.id in BLOCKED_NAMES:
                return f"Blocked name used: '{node.id}'."
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                return f"Private attribute access blocked: '.{node.attr}'."
    low = code.lower()
    for pat in ("__class__", "__dict__", "__mro__", "__subclasses__", "__bases__",
                "getattr(", "os.", "sys.", "subprocess", "socket", "open(",
                "urllib", "requests.", "importlib"):
        if pat in low:
            return f"Blocked pattern detected: '{pat}'."
    return None


_HARNESS = r'''
import io, json, sys, traceback
import pandas as pd

CODE_PATH = sys.argv[1]
DATA_PATH = sys.argv[2]
CAPITAL = float(sys.argv[3]) if len(sys.argv) > 3 else 100000.0

ALLOWED = {"pandas", "numpy", "backtrader", "math", "statistics", "datetime",
           "collections", "itertools", "functools", "operator", "re", "json"}
_real_import = __import__
def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = (name or "").split(".")[0]
    if root not in ALLOWED:
        raise ImportError(f"Import blocked in sandbox: '{name}'")
    return _real_import(name, globals, locals, fromlist, level)

SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "filter": filter, "float": float, "int": int,
    "isinstance": isinstance, "issubclass": issubclass, "len": len,
    "list": list, "map": map, "max": max, "min": min, "print": print,
    "range": range, "round": round, "set": set, "sorted": sorted,
    "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    "super": super, "object": object, "type": type, "property": property,
    "staticmethod": staticmethod, "classmethod": classmethod,
    "True": True, "False": False, "None": None,
    "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
    "KeyError": KeyError, "IndexError": IndexError, "ZeroDivisionError": ZeroDivisionError,
    "AttributeError": AttributeError, "NameError": NameError,
    "ImportError": ImportError, "RuntimeError": RuntimeError,
    "StopIteration": StopIteration,
    "__build_class__": __build_class__, "__name__": "__strategy__",
    "__import__": _guarded_import,
}

def _emit(obj):
    sys.stdout.write("RATRADE_RESULT:" + json.dumps(obj, default=str) + "\n")

try:
    df = pd.read_csv(DATA_PATH)
    with open(CODE_PATH, "r", encoding="utf-8") as f:
        code = f.read()
    import types as _types
    _mod = _types.ModuleType("user_strategy")
    _mod.__dict__["__builtins__"] = dict(SAFE_BUILTINS)
    sys.modules["user_strategy"] = _mod  # lets backtrader metaclass resolve cls.__module__
    ns = _mod.__dict__
    buf = io.StringIO()
    _old = sys.stdout
    sys.stdout = buf
    try:
        exec(compile(code, "<strategy>", "exec"), ns)
    finally:
        sys.stdout = _old
    logs = buf.getvalue()[-4000:]
    out = None
    if callable(ns.get("run")):
        out = ns["run"](df)
    elif "CustomStrategy" in ns:
        import backtrader as bt
        data = df.copy()
        data["datetime"] = pd.to_datetime(data["date"])
        feed = bt.feeds.PandasData(dataname=data.set_index("datetime"))
        cerebro = bt.Cerebro()
        cerebro.addstrategy(ns["CustomStrategy"])
        cerebro.adddata(feed)
        cerebro.broker.setcash(CAPITAL)
        cerebro.broker.setcommission(commission=0.0005)
        cerebro.run()
        val = cerebro.broker.getvalue()
        out = {"trades": [], "equity_curve": [CAPITAL, val],
               "logs": f"backtrader final portfolio value: {val:.2f}. Define run(data) for per-trade detail."}
        logs = (logs + "\n" + out["logs"])[-4000:]
    else:
        raise RuntimeError("Define run(data) or a backtrader Strategy named CustomStrategy.")
    if not isinstance(out, dict) or "trades" not in out:
        raise RuntimeError("run(data) must return a dict like {'trades': [...]}.")
    _emit({"ok": True, "trades": out.get("trades", [])[:5000],
           "equity_curve": out.get("equity_curve", [])[:5000], "logs": logs})
except Exception:
    _emit({"ok": False, "error": traceback.format_exc()[-4000:]})
'''


def _compute_metrics(trades: list, initial_capital: float) -> dict:
    pnls = []
    for t in trades:
        try:
            pnl = float(t.get("pnl", 0))
        except Exception:
            pnl = 0.0
        pnls.append(pnl)
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    net = round(sum(pnls), 2)
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p <= 0))
    avg_win = round(gross_win / wins, 2) if wins else 0.0
    avg_loss = round(gross_loss / losses, 2) if losses else 0.0
    cap = initial_capital
    peak = cap
    max_dd = 0.0
    for p in pnls:
        cap += p
        peak = max(peak, cap)
        if peak > 0:
            max_dd = max(max_dd, (peak - cap) / peak * 100)
    sharpe = 0.0
    if n > 1:
        mean = sum(pnls) / n
        var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
        if var > 0:
            sharpe = round(mean / math.sqrt(var) * math.sqrt(252), 4)
    return {
        "initial_capital": initial_capital,
        "final_capital": round(cap, 2),
        "net_pnl": net,
        "total_trades": n,
        "winning_trades": wins,
        "losing_trades": losses,
        "win_rate": round(wins / n * 100, 2) if n else 0.0,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (round(gross_win, 2) if gross_win > 0 else 0.0),
        "max_drawdown": round(max_dd, 2),
        "sharpe_ratio": sharpe,
    }


def run_custom_backtest(code: str, historical: list, symbol: str,
                        initial_capital: float = 100000.0) -> dict:
    err = validate_code(code)
    if err:
        return {"success": False, "error": err}
    if not historical or len(historical) < 5:
        return {"success": False, "error": f"Not enough historical data for {symbol}."}
    rows = [{
        "date": h.get("trade_date", ""),
        "open": float(h.get("open_price", 0) or 0),
        "high": float(h.get("high_price", 0) or 0),
        "low": float(h.get("low_price", 0) or 0),
        "close": float(h.get("close_price", 0) or 0),
        "volume": int(h.get("volume", 0) or 0),
    } for h in historical]
    try:
        import pandas as pd
        df = pd.DataFrame(rows)
    except Exception as e:
        return {"success": False, "error": f"DataFrame build failed: {e}"}
    tmpdir = tempfile.mkdtemp(prefix="ratrade_py_")
    code_path = os.path.join(tmpdir, "strategy.py")
    data_path = os.path.join(tmpdir, "data.csv")
    harness_path = os.path.join(tmpdir, "harness.py")
    try:
        with open(code_path, "w", encoding="utf-8") as f:
            f.write(code)
        df.to_csv(data_path, index=False)
        with open(harness_path, "w", encoding="utf-8") as f:
            f.write(_HARNESS)
        proc = subprocess.run(
            [sys.executable, harness_path, code_path, data_path, str(initial_capital)],
            capture_output=True, text=True, timeout=EXEC_TIMEOUT_SEC, cwd=tmpdir,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Time limit exceeded ({EXEC_TIMEOUT_SEC}s) - check for infinite loops."}
    except Exception as e:
        return {"success": False, "error": f"Sandbox launch failed: {e}"}
    finally:
        for p in (code_path, data_path, harness_path):
            try:
                os.remove(p)
            except Exception:
                pass
        try:
            os.rmdir(tmpdir)
        except Exception:
            pass
    payload = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("RATRADE_RESULT:"):
            try:
                payload = json.loads(line[len("RATRADE_RESULT:"):])
            except Exception:
                pass
    if payload is None:
        detail = (proc.stderr or "")[-2000:]
        return {"success": False, "error": f"Script crashed with no result. {detail}"}
    if not payload.get("ok"):
        return {"success": False, "error": payload.get("error", "Unknown script error.")}
    raw_trades = payload.get("trades", []) or []
    trades = []
    for i, t in enumerate(raw_trades[:MAX_TRADES]):
        if not isinstance(t, dict):
            continue
        try:
            ep = float(t.get("entry_price", 0))
            xp = float(t.get("exit_price", 0))
            qty = float(t.get("qty", t.get("quantity", 1)) or 1)
        except Exception:
            continue
        side = str(t.get("side", "long")).lower()
        pnl = (xp - ep) * qty if side != "short" else (ep - xp) * qty
        trades.append({
            "id": i + 1,
            "entry_date": str(t.get("entry_date", "")),
            "exit_date": str(t.get("exit_date", "")),
            "entry_price": round(ep, 2),
            "exit_price": round(xp, 2),
            "qty": qty,
            "side": side,
            "pnl": round(pnl, 2),
        })
    equity = []
    cap = initial_capital
    equity.append(round(cap, 2))
    for t in trades:
        cap += t["pnl"]
        equity.append(round(cap, 2))
    if payload.get("equity_curve"):
        try:
            equity = [round(float(x), 2) for x in payload["equity_curve"]]
        except Exception:
            pass
    metrics = _compute_metrics(trades, initial_capital)
    return {
        "success": True,
        "symbol": symbol,
        "metrics": metrics,
        "trades": trades,
        "equity_curve": equity,
        "logs": str(payload.get("logs", ""))[-4000:],
        "bars": len(rows),
    }
