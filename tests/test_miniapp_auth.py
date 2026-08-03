"""Telegram Mini App initData verification."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from app.miniapp.auth import InitDataError, _user_from_fields, verify_init_data

BOT_TOKEN = "123456:TEST-TOKEN"


def _sign(fields: dict[str, str], token: str = BOT_TOKEN) -> str:
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def _fields(**overrides) -> dict[str, str]:
    base = {
        "auth_date": str(int(time.time())),
        "query_id": "AAF",
        "user": json.dumps({"id": 42, "first_name": "Ali", "language_code": "uz"}),
    }
    base.update(overrides)
    return base


def test_valid_init_data_passes():
    parsed = verify_init_data(_sign(_fields()), bot_token=BOT_TOKEN)
    assert json.loads(parsed["user"])["id"] == 42


def test_tampered_payload_is_rejected():
    raw = _sign(_fields())
    tampered = raw.replace("Ali", "Bob")
    with pytest.raises(InitDataError):
        verify_init_data(tampered, bot_token=BOT_TOKEN)


def test_wrong_bot_token_is_rejected():
    with pytest.raises(InitDataError):
        verify_init_data(_sign(_fields()), bot_token="999:OTHER")


def test_missing_hash_is_rejected():
    with pytest.raises(InitDataError):
        verify_init_data(urlencode(_fields()), bot_token=BOT_TOKEN)


def test_expired_auth_date_is_rejected():
    old = _fields(auth_date=str(int(time.time()) - 7200))
    with pytest.raises(InitDataError):
        verify_init_data(_sign(old), bot_token=BOT_TOKEN, ttl_seconds=3600)


def test_expiry_check_can_be_disabled():
    old = _fields(auth_date=str(int(time.time()) - 7200))
    assert verify_init_data(_sign(old), bot_token=BOT_TOKEN, ttl_seconds=0)


def test_signature_field_participates_in_the_hash():
    """Bot API 8.0+ signs `signature` too — only `hash` is left out."""
    fields = _fields(signature="ed25519-value")
    parsed = verify_init_data(_sign(fields), bot_token=BOT_TOKEN)
    assert parsed["signature"] == "ed25519-value"


def test_signature_less_variant_is_still_accepted():
    """Fallback for clients that keep `signature` out of the data-check-string."""
    fields = _fields()
    raw = _sign(fields) + "&signature=ed25519-value"
    assert verify_init_data(raw, bot_token=BOT_TOKEN)


def test_tampering_with_signature_alone_is_rejected():
    fields = _fields(signature="ed25519-value")
    raw = _sign(fields).replace("ed25519-value", "forged-value")
    with pytest.raises(InitDataError):
        verify_init_data(raw, bot_token=BOT_TOKEN)


def test_empty_init_data_is_rejected():
    with pytest.raises(InitDataError):
        verify_init_data("", bot_token=BOT_TOKEN)


def test_user_is_parsed_from_fields():
    parsed = verify_init_data(_sign(_fields()), bot_token=BOT_TOKEN)
    user = _user_from_fields(parsed)
    assert user.telegram_user_id == 42
    assert user.first_name == "Ali"
    assert user.language_code == "uz"
    assert user.is_dev is False


def test_user_payload_must_be_json():
    parsed = verify_init_data(_sign(_fields(user="not-json")), bot_token=BOT_TOKEN)
    with pytest.raises(InitDataError):
        _user_from_fields(parsed)
