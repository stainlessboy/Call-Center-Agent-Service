from __future__ import annotations

import json
import os
import ssl
import urllib.request
from typing import Optional, Tuple


def _build_ssl_context() -> ssl.SSLContext:
    """
    Default SSL verification is disabled (to tolerate self-signed proxies).
    Set TELEGRAM_SSL_VERIFY=true to enable verification.
    """
    verify_raw = (os.getenv("TELEGRAM_SSL_VERIFY") or "").strip().lower()
    if verify_raw in {"1", "true", "yes", "on"}:
        return ssl.create_default_context()
    return ssl._create_unverified_context()  # pragma: no cover - opt-in only


def send_telegram_message(token: str, chat_id: int, text: str) -> Tuple[bool, Optional[str]]:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10, context=_build_ssl_context()) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not data.get("ok"):
            return False, data.get("description") or "telegram api error"
        return True, None
    except Exception as exc:  # pragma: no cover - network issues
        return False, str(exc)
