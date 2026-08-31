"""TradingView Free Webhook bridge (soranoo pattern).
Polls IMAP inbox for TradingView alert emails and forwards to RaTrade webhook.

Ref: https://github.com/soranoo/TradingView-Free-Webhook-Alerts
- IMAP email_listener -> _parse_tv_message -> POST /api/webhook/soranoo
- Supports Gmail/Outlook via env TV_IMAP_HOST/USER/PASS, broadcast to webhook + Telegram.

Enable via env: TV_FREE_ENABLED=1. Disabled by default (no IMAP creds -> no-op).
"""
import os, time, logging
logger = logging.getLogger(__name__)

def poll_once_and_forward():
    host = os.environ.get("TV_IMAP_HOST","")
    user = os.environ.get("TV_IMAP_USER","")
    pwd = os.environ.get("TV_IMAP_PASS","")
    if not host or not user or not pwd:
        return 0
    try:
        import imaplib, email
        from email.header import decode_header
        import requests, json, re
        m = imaplib.IMAP4_SSL(host)
        m.login(user, pwd)
        m.select("INBOX")
        # Unseen from noreply@tradingview.com
        status, msgs = m.search(None, '(UNSEEN FROM "noreply@tradingview.com")')
        if status != "OK" or not msgs[0]:
            m.logout()
            return 0
        count = 0
        webhook_url = os.environ.get("TV_WEBHOOK_URL", "http://localhost:8000/api/webhook/soranoo")
        for num in msgs[0].split():
            _, data = m.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(data[0][1])
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                        body = part.get_payload(decode=True).decode(errors="ignore")
                        break
            else:
                body = msg.get_payload(decode=True).decode(errors="ignore")
            # Forward raw body
            try:
                requests.post(webhook_url, data=body.encode(), headers={"Content-Type":"text/plain"}, timeout=5)
                count += 1
                m.store(num, '+FLAGS', '\\Seen')
            except Exception as e:
                logger.warning(f"tv-free forward fail: {e}")
        m.logout()
        return count
    except Exception as e:
        logger.warning(f"tv-free poll fail: {e}")
        return 0
