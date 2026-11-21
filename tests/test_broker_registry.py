"""Broker registry + disconnect + Fyers-first priority.

Covers:
- Dhan is GONE from the broker registry (UI + backend).
- Fyers is broker #1 (priority order + auto-select).
- Disconnect clears the session and survives env-seeded credentials
  (the 'disconnected' flag blocks the env seed from reviving it).
- Re-connect (save-config) clears the flag again.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import routes.broker as B


def test_dhan_removed_from_registry():
    assert "dhan" not in B.BROKER_DEFAULTS
    assert "dhan" not in B.BROKER_INTERNAL
    assert "dhan" not in B.BROKER_ORDER
    # no Dhan endpoint left in the router
    paths = {getattr(r, "path", "") for r in B.router.routes}
    for p in paths:
        assert "dhan" not in p.lower(), p


def test_fyers_is_broker_number_one():
    keys = list(B.BROKER_DEFAULTS.keys())
    assert keys[0] == "fyers", keys
    assert B.BROKER_ORDER[0] == "fyers", B.BROKER_ORDER
    # Bigul is the second broker
    assert keys[1] == "bigul", keys
    assert B.BROKER_ORDER[1] == "bigul", B.BROKER_ORDER


def test_auto_broker_prefers_fyers(monkeypatch):
    monkeypatch.setattr(B, "_real_token", lambda b: b in ("fyers", "bigul"))
    assert B._auto_live_broker() == "fyers"


def test_disconnect_clears_session_and_blocks_env_seed(monkeypatch):
    """Disconnect: stored creds gone + env seed must NOT revive the session."""
    db_rows = {}

    class _Row(dict):
        pass

    def _fetch_one(sql, params=None):
        key = (params or [""])[0]
        return db_rows.get(key)

    def _execute(sql, params=None):
        # UPDATE settings ... WHERE setting_key=?   /  INSERT ... VALUES (?, ?)
        sql_l = " ".join(sql.split())
        if sql_l.startswith("UPDATE settings"):
            key = params[-1]
            if key in db_rows:
                db_rows[key] = {"setting_value": params[0]}
                return 1
            return 0
        if sql_l.startswith("INSERT INTO settings"):
            db_rows[params[0]] = {"setting_value": params[1]}
            return 1
        if sql_l.startswith("DELETE FROM settings"):
            db_rows.pop(params[0], None)
            return 1
        return 0

    monkeypatch.setattr(B.Database, "get_instance", staticmethod(lambda: type(
        "DB", (), {"fetch_one": staticmethod(_fetch_one),
                   "execute": staticmethod(_execute)})()))
    monkeypatch.setenv("RATRADE_FYERS_APP_ID", "ENVAPP-100")
    monkeypatch.setenv("RATRADE_FYERS_SECRET", "envsecret")

    # connected first
    B._save_config("fyers", {"app_id": "DBAPP-100", "secret": "dbsecret",
                              "access_token": "abc1234567890"})
    cfg = B._get_config("fyers")
    assert cfg.get("app_id") == "ENVAPP-100"      # env priority for credentials
    assert cfg.get("access_token") == "abc1234567890"
    assert B._real_token("fyers") is True

    # disconnect
    res = B.disconnect_broker(B.ConnectRequest(broker="fyers"))
    assert res.get("success") is True
    assert res.get("disconnected") is True
    # session gone even though env still holds the credentials
    assert B._get_config("fyers") == {}
    assert B._real_token("fyers") is False
    assert B._auto_live_broker() == ""


def test_reconnect_clears_disconnect_flag(monkeypatch):
    db_rows = {}

    def _fetch_one(sql, params=None):
        return db_rows.get((params or [""])[0])

    def _execute(sql, params=None):
        sql_l = " ".join(sql.split())
        if sql_l.startswith("UPDATE settings"):
            key = params[-1]
            if key in db_rows:
                db_rows[key] = {"setting_value": params[0]}
                return 1
            return 0
        if sql_l.startswith("INSERT INTO settings"):
            db_rows[params[0]] = {"setting_value": params[1]}
            return 1
        if sql_l.startswith("DELETE FROM settings"):
            db_rows.pop(params[0], None)
            return 1
        return 0

    monkeypatch.setattr(B.Database, "get_instance", staticmethod(lambda: type(
        "DB", (), {"fetch_one": staticmethod(_fetch_one),
                   "execute": staticmethod(_execute)})()))

    B._save_config("fyers", {"app_id": "A-100", "secret": "s",
                              "access_token": "tok1234567890"})
    B._set_disconnected("fyers", True)
    assert B._is_disconnected("fyers") is True
    assert B._get_config("fyers") == {}

    # Setup/Connect writes creds again -> back online
    B._save_config("fyers", {"app_id": "A-100", "secret": "s",
                              "access_token": "tok1234567890"})
    assert B._is_disconnected("fyers") is False
    assert B._get_config("fyers").get("access_token") == "tok1234567890"


def test_disconnect_unknown_broker():
    res = B.disconnect_broker(B.ConnectRequest(broker="dhan"))
    assert res.get("success") is False
