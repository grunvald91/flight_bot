import html
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot

from bot.config import MOSCOW_CHANNEL_ID, SOCHI_CHANNEL_ID, TIMEZONE
from bot.services.international_discovery import discover_international_offers

logger = logging.getLogger(__name__)

CHANNELS = (
    ("moscow", "MOW", "Москвы", MOSCOW_CHANNEL_ID),
    ("sochi", "AER", "Сочи", SOCHI_CHANNEL_ID),
)


def _flag(country_code: str | None) -> str:
    code = (country_code or "").upper()
    if len(code) != 2 or not code.isalpha():
        return "🌍"
    return "".join(chr(127397 + ord(ch)) for ch in code)


def _price(offer: dict) -> int:
    try:
        return int(float((offer.get("price") or {}).get("amount") or 0))
    except (TypeError, ValueError):
        return 0


def _transfers(offer: dict) -> str:
    legs = offer.get("legs") or []
    segments = (legs[0] or {}).get("segments") or [] if legs else []
    count = max(0, len(segments) - 1)
    if count == 0:
        return "прямой"
    if count == 1:
        return "1 пересадка"
    if count < 5:
        return f"{count} пересадки"
    return f"{count} пересадок"


def _time(offer: dict) -> str:
    raw = str(offer.get("departure_at") or "")
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%H:%M")
    except ValueError:
        return ""


def _format_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return value


def format_channel_post(origin_label: str, departure_date: str, offers: list[dict]) -> str:
    lines = [
        f"✈️ <b>Самые дешёвые билеты за границу из {html.escape(origin_label)}</b>",
        f"📅 Вылет завтра: <b>{_format_date(departure_date)}</b>",
        "",
    ]

    for index, offer in enumerate(offers[:10], 1):
        discovery = offer.get("_discovery") or {}
        country = str(discovery.get("country") or "Страна не определена")
        city = str(discovery.get("city") or "Город не определён")
        country_code = str(discovery.get("country_code") or "")
        amount = _price(offer)
        price_text = f"{amount:,}".replace(",", " ") + " ₽" if amount else "цена не указана"
        details = [_transfers(offer)]
        depart_time = _time(offer)
        if depart_time:
            details.insert(0, depart_time)

        search_url = offer.get("search_results_url") or (offer.get("checkout_ref") or {}).get("search_results_url")
        destination = f"{_flag(country_code)} <b>{html.escape(country)} — {html.escape(city)}</b>"
        if search_url:
            destination = f'<a href="{html.escape(str(search_url), quote=True)}">{destination}</a>'

        lines.append(f"{index}. {destination} — <b>{price_text}</b>")
        lines.append(f"   {' · '.join(details)}")

    lines.extend([
        "",
        "🎟 Только туда · 1 взрослый · эконом.",
        "Цены подтверждены через Туту на момент поиска и могут измениться до оформления.",
        "🔎 Больше вариантов: @a_ticketbot",
    ])
    return "\n".join(lines)


async def publish_channel(
    bot: Bot,
    *,
    origin: str,
    origin_label: str,
    chat_id: int | str,
    departure_date: str,
) -> int:
    offers = await discover_international_offers(
        origin,
        departure_date,
        result_limit=10,
    )
    if not offers:
        logger.warning("No verified international offers for %s on %s", origin, departure_date)
        return 0

    text = format_channel_post(origin_label, departure_date, offers)
    if getattr(offers, "unverified", 0):
        text += (
            f"\n\n⚠️ Не удалось проверить направлений: {offers.unverified}. "
            "Список отсортирован по цене среди подтверждённых вариантов."
        )

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    logger.info(
        "Published %d cheapest international destinations from %s for %s to %s",
        min(10, len(offers)),
        origin,
        departure_date,
        chat_id,
    )
    return min(10, len(offers))


async def publish_tomorrow_channels(bot: Bot) -> dict[str, int]:
    now = datetime.now(ZoneInfo(TIMEZONE))
    departure_date = (now.date() + timedelta(days=1)).isoformat()
    result: dict[str, int] = {}

    for key, origin, origin_label, chat_id in CHANNELS:
        if not chat_id:
            logger.warning("%s channel is not configured; skipping", key)
            result[key] = 0
            continue
        try:
            result[key] = await publish_channel(
                bot,
                origin=origin,
                origin_label=origin_label,
                chat_id=chat_id,
                departure_date=departure_date,
            )
        except Exception:
            logger.exception("Daily channel publication failed for %s", key)
            result[key] = 0

    return result
