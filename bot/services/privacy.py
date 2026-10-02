"""Stable, non-reversible request-log pseudonyms; no message payloads."""
import hashlib
import hmac

from bot.config import BOT_TOKEN


def request_log_identity(telegram_id: int) -> int:
    if not BOT_TOKEN:
        raise RuntimeError("Bot token unavailable for request-log pseudonyms")
    digest = hmac.new(BOT_TOKEN.encode(), str(telegram_id).encode(), hashlib.sha256).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)
