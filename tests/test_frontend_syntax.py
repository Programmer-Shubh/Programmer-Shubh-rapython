"""Frontend regression gate: index.html ka inline JS parse hona chahiye.
Ek bracket tootne par POORA script fail = koi button kaam nahi karta
(wahi 'koi button click nahi ho raha' bug tha)."""
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _inline_blocks():
    p = os.path.join(os.path.dirname(__file__), "..", "static", "index.html")
    html = open(os.path.abspath(p), encoding="utf-8").read()
    return re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", html, re.S)


def test_inline_js_parses():
    blocks = _inline_blocks()
    assert blocks, "koi inline script block nahi mila"
    node = shutil.which("node")
    if not node:
        # Node nahi to kam-se-kam bracket balance check
        for b in blocks:
            assert b.count("(") == b.count(")"), "paren mismatch"
            assert b.count("{") == b.count("}"), "brace mismatch"
            assert b.count("[") == b.count("]"), "bracket mismatch"
        return
    import tempfile
    for i, b in enumerate(blocks):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(b)
            tmp = f.name
        try:
            r = subprocess.run([node, "--check", tmp], capture_output=True,
                               text=True, timeout=60)
            assert r.returncode == 0, f"block {i} JS syntax error:\n{r.stderr[:800]}"
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass
