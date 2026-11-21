"""Self-ping URL resolution: SELF_URL > RENDER_EXTERNAL_URL > none."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _resolve():
    return ((os.environ.get("SELF_URL") or "").strip()
            or (os.environ.get("RENDER_EXTERNAL_URL") or "").strip()).rstrip("/")


def test_self_url_wins(monkeypatch):
    monkeypatch.setenv("SELF_URL", "https://a.example/")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://b.example/")
    assert _resolve() == "https://a.example"


def test_render_external_fallback(monkeypatch):
    monkeypatch.delenv("SELF_URL", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://ratrade-iwi2.onrender.com/")
    assert _resolve() == "https://ratrade-iwi2.onrender.com"


def test_none_when_unset(monkeypatch):
    monkeypatch.delenv("SELF_URL", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    assert _resolve() == ""
