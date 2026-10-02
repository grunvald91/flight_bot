import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest
from cryptography.exceptions import InvalidTag

os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/test")
os.environ.setdefault("PROFILE_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"t" * 32).decode("ascii"))

from bot.handlers.tutu import _booking_summary, _carousel_keyboard
from bot.passenger_web import ProfileInput, _booking_snapshot, _safe_avia_offer, _validated_profile
from bot.services.passenger_crypto import ProfileCrypto
from bot.services.passenger_profile_store import _checkout_hash
from bot.services.telegram_web_auth import validate_telegram_init_data


def _valid_input(**overrides):
    data = {
        "first_name": "ARTUR", "last_name": "IVANOV", "middle_name": None,
        "birthday": "1991-03-14", "sex": "MALE", "document_type": "FOREIGN_PASSPORT",
        "document_number": "721234567", "expires_at": "2031-03-14",
        "email": "synthetic@example.invalid", "phone": "+79990001122", "storage_consent": True,
    }
    data.update(overrides)
    return ProfileInput(**data)


def _init_data(token: str, user_id: int, auth_date: int | None = None) -> str:
    values = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAE-test",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_profile_crypto_is_owner_bound_and_tamper_evident():
    crypto = ProfileCrypto(secrets.token_bytes(32))
    profile = {"passenger": {"document": {"number": "721234567"}}}
    encrypted = crypto.encrypt(profile, owner_id=10)
    assert encrypted.startswith(b"PP3") and b"721234567" not in encrypted
    assert crypto.decrypt(encrypted, owner_id=10) == profile
    with pytest.raises(InvalidTag):
        crypto.decrypt(encrypted, owner_id=11)
    tampered = bytearray(encrypted); tampered[-1] ^= 1
    with pytest.raises((InvalidTag, ValueError)):
        crypto.decrypt(bytes(tampered), owner_id=10)


def test_telegram_init_data_signature_age_and_identity():
    token = "123456:test-token"
    raw = _init_data(token, 987654321)
    identity = validate_telegram_init_data(raw, token)
    assert identity.user_id == 987654321
    with pytest.raises(ValueError):
        validate_telegram_init_data(raw, token + "bad")
    with pytest.raises(ValueError):
        validate_telegram_init_data(_init_data(token, 1, int(time.time()) - 600), token)
    with pytest.raises(ValueError):
        validate_telegram_init_data(raw + "&user=%7B%22id%22%3A2%7D", token)


def test_validated_profile_preserves_spelling_and_consent():
    profile = _validated_profile(_valid_input(first_name="Artur", last_name="Ivanov-Smirnov"))
    assert profile["passenger"]["first_name"] == "ARTUR"
    assert profile["passenger"]["last_name"] == "IVANOV-SMIRNOV"
    assert profile["passenger"]["document"]["number"] == "721234567"
    assert profile["consent"]["storage"] is True


def test_safe_avia_offer_drops_unneeded_urls_and_requires_exact_party():
    ref = {
        "product_type": "avia", "search_results_url": "https://example.test/should-not-be-sent",
        "offer_hash": "offer", "departure_geo_city_id": 1, "arrival_geo_city_id": 2,
        "departure_at": "2026-10-01T10:00:00+03:00", "passengers_full": 1,
        "passengers_child": 0, "passengers_infant": 0,
    }
    clean = _safe_avia_offer(ref)
    assert "search_results_url" not in clean and "product_type" not in clean
    assert clean["offer_hash"] == "offer"
    with pytest.raises(Exception):
        _safe_avia_offer({**ref, "is_multi_pnr": True})
    assert _checkout_hash({"checkout_ref": {**ref, "result_id": "first"}}) == _checkout_hash(
        {"checkout_ref": {**ref, "result_id": "second", "card_id": "changed"}})


def test_booking_snapshot_requires_exact_flights_and_matching_departure():
    ref = {"offer_hash": "offer", "departure_geo_city_id": 1,
           "arrival_geo_city_id": 2, "departure_at": "2026-10-01T10:00:00+03:00"}
    summary = {"from": "Москва", "to": "Стамбул", "price_amount": 12345,
               "departure_at": ref["departure_at"], "segments": [
                   {"from": "Москва", "to": "Стамбул", "flight": "SU100", "departure_at": ref["departure_at"]}]}
    assert _booking_snapshot({"passenger": {}, "customer": {}},
                             {"checkout_ref": ref, "offer_summary": summary})["offer"] == ref
    with pytest.raises(Exception):
        _booking_snapshot({"passenger": {}, "customer": {}},
                          {"checkout_ref": ref, "offer_summary": {**summary, "segments": []}})
    with pytest.raises(Exception):
        _booking_snapshot({"passenger": {}, "customer": {}},
                          {"checkout_ref": ref, "offer_summary": {**summary, "departure_at": "different"}})


def test_booking_summary_and_callback_are_safe_and_bounded():
    offer = {
        "offer_id": "offer-1", "price": {"amount": 12345, "currency": "RUB"},
        "departure_at": "2026-10-01T10:00:00+03:00", "arrival_at": "2026-10-01T12:00:00+03:00",
        "legs": [{"from": "Москва", "to": "Стамбул", "segments": []}],
        "checkout_ref": {
            "product_type": "avia", "offer_hash": "hash", "departure_geo_city_id": 1,
            "arrival_geo_city_id": 2, "departure_at": "2026-10-01T10:00:00+03:00", "passengers_full": 1,
        },
    }
    summary = _booking_summary(offer)
    assert summary["from"] == "Москва" and summary["to"] == "Стамбул"
    markup = _carousel_keyboard("searchkey", [offer], 0, "price")
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
    assert any(value.startswith("tutubook:") for value in callbacks)
    assert all(len(value.encode()) <= 64 for value in callbacks)


def test_profile_page_uses_dedicated_assets_and_telegram_auth():
    root = Path(__file__).parents[1] / "bot"
    html = (root / "templates" / "passenger_profile.html").read_text(encoding="utf-8")
    js = (root / "static" / "passenger_profile.js").read_text(encoding="utf-8")
    assert "<style>" not in html and "<script>" not in html
    assert 'src="/assets/telegram-web-app.js"' in html
    assert (root / "static" / "telegram-web-app.js").stat().st_size > 1000
    assert "tg.initData" in js and "api/session/exchange" in js
    assert "innerHTML" not in js
    assert 'id="bookingBtn" class="primary" type="button" disabled' in html
    assert "profile_full" not in js
    assert "context.profile_masked" in js
