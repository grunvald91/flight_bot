import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


@dataclass(frozen=True)
class TelegramIdentity:
    user_id: int
    auth_date: int
    init_hash: str


def validate_telegram_init_data(raw: str, bot_token: str, *, max_age_seconds: int = 300) -> TelegramIdentity:
    if not raw or len(raw.encode("utf-8")) > 8192:
        raise ValueError("Invalid Telegram initData size")
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("Duplicate Telegram initData field")
        values[key] = value
    supplied_hash = values.pop("hash", "")
    if len(supplied_hash) != 64:
        raise ValueError("Telegram initData hash is missing")
    check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash.lower()):
        raise ValueError("Telegram initData signature mismatch")
    try:
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Telegram initData identity is invalid") from exc
    now = int(time.time())
    if auth_date > now + 60 or now - auth_date > max_age_seconds:
        raise ValueError("Telegram initData is expired")
    if user_id <= 0:
        raise ValueError("Telegram user id is invalid")
    return TelegramIdentity(
        user_id=user_id,
        auth_date=auth_date,
        init_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )
