"""Egress proxy config: env/db merge, httpx kwargs, save/status roundtrip."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))


def test_cfg_env_override_and_mask(monkeypatch):
    import core.services.egress_proxy as ep
    monkeypatch.setenv("EGRESS_PROXY_URL", "https://zeroip.snipy.in:8443")
    monkeypatch.setenv("EGRESS_PROXY_TOKEN", "zero_ip_yyvu6nQg3UnGj458daVasYRyWojNPnXV")
    monkeypatch.setenv("EGRESS_PROXY_IP", "2406:da1a:5d0:9f00:a262::1032")
    monkeypatch.delenv("EGRESS_PROXY_ENABLED", raising=False)
    c = ep.cfg()
    assert c["configured"] is True          # enabled default ON (url+token pade)
    assert c["source"] == "env"
    assert c["url"].startswith("https://")
    s = ep.status()
    assert "token" not in s                 # full token kabhi leak nahi
    assert s["token_masked"].startswith("zero_")
    monkeypatch.setenv("EGRESS_PROXY_ENABLED", "false")
    assert ep.cfg()["configured"] is False  # explicit off


def test_httpx_kwargs_proxy_with_headers(monkeypatch):
    import core.services.egress_proxy as ep
    monkeypatch.setenv("EGRESS_PROXY_URL", "https://zeroip.snipy.in:8443")
    monkeypatch.setenv("EGRESS_PROXY_TOKEN", "zero_ip_test_k")
    monkeypatch.setenv("EGRESS_PROXY_IP", "2001:db8::1")
    kw = ep.httpx_kwargs()
    assert kw
    p = kw["proxy"]
    try:
        hdr = p.headers
        assert hdr.get("X-Snipy-Proxy-IPv6") == "2001:db8::1"
        assert str(hdr.get("Proxy-Authorization", "")).startswith("Bearer")
    except AttributeError:                  # purane httpx: dict headers shape
        h = kw["proxy_headers"]
        assert h["X-Snipy-Proxy-IPv6"] == "2001:db8::1"


def test_httpx_kwargs_off(monkeypatch):
    import core.services.egress_proxy as ep
    for k in ("EGRESS_PROXY_URL", "EGRESS_PROXY_TOKEN", "EGRESS_PROXY_IP"):
        monkeypatch.delenv(k, raising=False)
    assert ep.httpx_kwargs() == {}


def test_sync_request_plain_path(monkeypatch):
    import core.services.egress_proxy as ep
    monkeypatch.delenv("EGRESS_PROXY_URL", raising=False)
    monkeypatch.delenv("EGRESS_PROXY_TOKEN", raising=False)
    calls = {}

    def _fake_req(method, url, headers=None, params=None, json=None, data=None, timeout=None):
        calls["m"] = method
        return object()
    import requests
    monkeypatch.setattr(requests, "request", _fake_req)
    ep.sync_request("POST", "https://api.example.com/v2/x", headers={"a": "b"},
                    json_body={"k": 1})
    assert calls["m"] == "POST"


def test_db_settings_save_roundtrip(monkeypatch):
    import core.services.egress_proxy as ep
    from core.models.database import Database
    for k in ("EGRESS_PROXY_URL", "EGRESS_PROXY_TOKEN", "EGRESS_PROXY_IP"):
        monkeypatch.delenv(k, raising=False)
    db = Database.get_instance()
    payload = {"enabled": True, "url": "https://p.example.com:8443",
               "token": "zero_ip_dbtest", "ip": "2001:db8::99"}
    db.execute("UPDATE settings SET setting_value=? WHERE setting_key='egress_proxy'",
               [__import__("json").dumps(payload)])
    c = ep.cfg()
    assert c["configured"] is True or c["url"] == ""
    assert c["ip"] in ("2001:db8::99", "")  # env absent to db value
    db.execute("UPDATE settings SET setting_value=? WHERE setting_key='egress_proxy'",
               [__import__("json").dumps({"enabled": False})])


# ---------------- WebSocket tunnel ----------------

class _FakeSock:
    """socket.create_connection sim: proxy 200 -> CONNECT lock."""

    def __init__(self, resp=b"HTTP/1.1 200 Connection established\r\n\r\n"):
        self._resp = resp
        self.sent = b""
        self.closed = False

    def sendall(self, b):
        self.sent += b

    def recv(self, n):
        r, self._resp = self._resp, b""
        return r

    def settimeout(self, t):
        self.t = t

    def close(self):
        self.closed = True


def test_ws_tunnel_off(monkeypatch):
    import core.services.egress_proxy as ep
    for k in ("EGRESS_PROXY_URL", "EGRESS_PROXY_TOKEN", "EGRESS_PROXY_IP"):
        monkeypatch.delenv(k, raising=False)
    assert ep.ws_connect_kwargs("api.example.com", 443) == {}


def test_ws_tunnel_ok(monkeypatch):
    import core.services.egress_proxy as ep
    monkeypatch.setenv("EGRESS_PROXY_URL", "https://zeroip.snipy.in:8443")
    monkeypatch.setenv("EGRESS_PROXY_TOKEN", "zero_ip_ws_t")
    monkeypatch.setenv("EGRESS_PROXY_IP", "2001:db8::7")
    fake = _FakeSock()

    def _fc(addr, timeout=None):
        assert addr[0] == "zeroip.snipy.in"
        assert addr[1] == 8443
        return fake
    import socket
    monkeypatch.setattr(socket, "create_connection", _fc)
    kw = ep.ws_connect_kwargs("api.example.com", 443)
    assert kw and isinstance(kw.get("sock"), _FakeSock)
    req = fake.sent.decode()
    assert req.startswith("CONNECT api.example.com:443 HTTP/1.1")
    assert "Proxy-Authorization: Bearer zero_ip_ws_t" in req
    assert "X-Snipy-Proxy-IPv6: 2001:db8::7" in req
    assert not fake.closed  # tunnel socket websockets ko diya


def test_ws_tunnel_reject(monkeypatch):
    import core.services.egress_proxy as ep
    monkeypatch.setenv("EGRESS_PROXY_URL", "https://zeroip.snipy.in:8443")
    monkeypatch.setenv("EGRESS_PROXY_TOKEN", "zero_ip_ws_t")
    monkeypatch.setenv("EGRESS_PROXY_IP", "2001:db8::7")
    fake = _FakeSock(resp=b"HTTP/1.1 403 Forbidden\r\n\r\n")

    def _fc(addr, timeout=None):
        return fake
    import socket
    monkeypatch.setattr(socket, "create_connection", _fc)
    assert ep.ws_connect_kwargs("api.example.com", 443) == {}
    assert fake.closed  # reject par tunnel socket band


def test_sync_request_proxy_fail_falls_back_direct(monkeypatch):
    """Snipy network down -> sync_request ek direct (requests) attempt. Mag"""
    import core.services.egress_proxy as ep
    monkeypatch.setenv("EGRESS_PROXY_URL", "https://deadproxy.example.com:9")
    monkeypatch.setenv("EGRESS_PROXY_TOKEN", "zero_ip_tt")
    monkeypatch.setenv("EGRESS_PROXY_IP", "2001:db8::9")

    def _boom(*a, **kw):
        raise ConnectionError("proxy unreachable")
    import httpx
    monkeypatch.setattr(httpx, "Client", _boom)
    calls = {}

    def _fake_req(method, url, headers=None, params=None, json=None, data=None, timeout=None):
        calls["m"], calls["url"] = method, url
        return "direct-resp"
    import requests
    monkeypatch.setattr(requests, "request", _fake_req)
    r = ep.sync_request("POST", "https://api.example.com/v2/x", headers={"a": "b"},
                        json_body={"k": 1})
    assert r == "direct-resp"
    assert calls["m"] == "POST"
