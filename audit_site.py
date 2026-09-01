import re
import sys
import ssl
import json
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "https://subh.infinityfreeapp.com"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def fetch(url, cookie=None, timeout=60):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
            body = r.read()
            return r.status, body, r.headers.get("Content-Type", ""), None
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, body, e.headers.get("Content-Type", "") if e.headers else "", None
    except Exception as e:
        return 0, b"", "", f"{type(e).__name__}: {e}"


def solve_cookie():
    status, body, ctype, err = fetch(BASE + "/")
    if err:
        raise RuntimeError("challenge fetch failed: " + err)
    text = body.decode("utf-8", "replace")
    hexes = re.findall(r'toNumbers\("([0-9a-fA-F]+)"\)', text)
    if len(hexes) != 3:
        raise RuntimeError(f"expected 3 toNumbers hex strings, got {len(hexes)}")
    key, iv, cipher = bytes.fromhex(hexes[0]), bytes.fromhex(hexes[1]), bytes.fromhex(hexes[2])

    from Crypto.Cipher import AES

    dec = AES.new(key, AES.MODE_CBC, iv).decrypt(cipher)
    cookie_hex = dec.hex()
    return cookie_hex, status


# Regexes that indicate a REAL PHP error/warning output (not CSS class names like text-warning)
ERROR_PATTERNS = [
    (r"Fatal error", "Fatal error"),
    (r"Parse error", "Parse error"),
    (r"<b>Warning</b>", "HTML Warning"),
    (r"<b>Deprecated</b>", "HTML Deprecated"),
    (r"<b>Notice</b>", "HTML Notice"),
    (r"<b>Parse error</b>", "HTML Parse error"),
    (r"PHP (Fatal|Parse|Warning|Deprecated|Notice)", "PHP prefixed error"),
    (r"Uncaught (TypeError|Error|Exception|RuntimeException|ValueError)", "Uncaught exception"),
    (r"Stack trace:", "Stack trace"),
    (r"Undefined (variable|array key|function|property|method|offset)", "Undefined ..."),
    (r"Call to undefined (function|method|property)", "Call to undefined"),
    (r"Creation of dynamic property", "Dynamic property (PHP 8.2+ deprecated)"),
    (r"Trying to access array offset on value of type null", "Array offset on null"),
    (r"Cannot access property", "Cannot access property"),
    (r"Attempt to (read|assign) property", "Attempt to property"),
]


def find_errors(text):
    out = []
    for pat, label in ERROR_PATTERNS:
        for m in re.finditer(pat, text):
            i = m.start()
            snippet = text[max(0, i - 50):i + 220].replace("\n", " ")
            out.append((label, i, snippet))
    return out


def check_page(url, cookie):
    status, body, ctype, err = fetch(url, cookie)
    if err:
        return f"ERROR (transport): {err}"
    path = re.sub(r"^https?://[^/]+", "", url)
    print(f"\n{'=' * 70}")
    print(f"URL: {url}  ->  status={status}, content-type={ctype}, length={len(body)}")
    if status == 0:
        print("ERROR: transport failure")
        return
    text = body.decode("utf-8", "replace")
    is_html = "html" in ctype.lower() or url.endswith("/") or url.endswith(".php") or not ctype
    found = find_errors(text)
    verdict = "OK" if not found else "PHP ERROR DETECTED"
    if 400 <= status < 600:
        verdict = "HTTP ERROR"
    print(f"status: {status}  verdict: {verdict}")
    for label, i, snippet in found:
        print(f"  [{label}] @ {i}: ...{snippet[:260]}...")
    if "html" in ctype.lower() or (not ctype and status < 400):
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        print(f"  title: {m.group(1).strip() if m else '(none)'}")
    else:
        print(f"  body[0:300]: {text[:300]!r}")
    return verdict


def main():
    try:
        cookie_hex, chal_status = solve_cookie()
    except Exception as e:
        print(f"FAILED to solve challenge: {e}")
        return
    print(f"Solved challenge cookie: __test={cookie_hex}  (challenge page served status {chal_status})")
    cookie = f"__test={cookie_hex}"

    urls = [
        "/",
        "/optionchain",
        "/papertrade",
        "/scanner",
        "/strategies",
        "/bhavcopy",
        "/brokers",
        "/api/indices",
        "/api/scan",
        "/api/scan-vwap",
        "/api/papertrade/portfolio",
        "/api/retention-status",
        "/api/optionchain/live?symbol=NIFTY",
    ]
    for u in urls:
        check_page(BASE + u, cookie)


if __name__ == "__main__":
    main()