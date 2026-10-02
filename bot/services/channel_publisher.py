import html
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot

from bot.config import MOSCOW_CHANNEL_ID, SOCHI_CHANNEL_ID, TIMEZONE
from bot.services.fast_browse import load_indicative_destinations

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[2]
STATE_PATH = ROOT_DIR / "private_cache" / "channel_publication.json"

CHANNELS = (
    ("moscow", "MOW", "Москвы", MOSCOW_CHANNEL_ID),
    ("sochi", "AER", "Сочи", SOCHI_CHANNEL_ID),
)


def _load_state() -> dict:
    try:
        if not STATE_PATH.exists():
            return {"channels": {}}
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {"channels": {}}
        payload.setdefault("channels", {})
        return payload
    except Exception:
        logger.exception("Could not read channel publication state")
        return {"channels": {}}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(STATE_PATH)


def bind_channel(
    key: str,
    *,
    chat_id: int,
    title: str | None = None,
    username: str | None = None,
) -> dict:
    if key not in {"moscow", "sochi"}:
        raise ValueError(f"Unknown channel key: {key}")

    state = _load_state()
    channels = state.setdefault("channels", {})
    previous = channels.get(key) if isinstance(channels.get(key), dict) else {}
    channels[key] = {
        "chat_id": int(chat_id),
        "title": title or "",
        "username": username or "",
        "last_published_date": previous.get("last_published_date"),
    }
    _save_state(state)
    return dict(channels[key])


def get_channel_bindings() -> dict[str, dict | None]:
    channels = _load_state().get("channels") or {}
    return {
        key: dict(channels[key]) if isinstance(channels.get(key), dict) else None
        for key in ("moscow", "sochi")
    }


def _resolve_chat_id(key: str, configured: str) -> int | str | None:
    if configured:
        return configured
    binding = get_channel_bindings().get(key)
    return binding.get("chat_id") if binding else None


def _already_published(key: str, departure_date: str) -> bool:
    binding = get_channel_bindings().get(key)
    return bool(binding and binding.get("last_published_date") == departure_date)


def _mark_published(key: str, departure_date: str) -> None:
    state = _load_state()
    channels = state.setdefault("channels", {})
    binding = channels.get(key)
    if not isinstance(binding, dict):
        binding = {}
        channels[key] = binding
    binding["last_published_date"] = departure_date
    _save_state(state)


def _format_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return value


def _money(value: int | float) -> str:
    amount = float(value)
    if amount.is_integer():
        return f"{amount:,.0f}".replace(",", " ")
    return f"{amount:,.2f}".replace(",", " ")


def _stops_label(count: int | None) -> str:
    if count is None:
        return "пересадки не указаны"
    if count == 0:
        return "прямой"
    if count == 1:
        return "1 пересадка"
    if 2 <= count <= 4:
        return f"{count} пересадки"
    return f"{count} пересадок"


def _time(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%H:%M")
    except ValueError:
        return ""


def format_channel_post(origin_label: str, departure_date: str, options: list[dict]) -> str:
    lines = [
        f"✈️ <b>Самые дешёвые билеты за границу из {html.escape(origin_label)}</b>",
        f"📅 Вылет завтра: <b>{_format_date(departure_date)}</b>",
        "",
    ]

    for index, option in enumerate(options[:10], 1):
        country = html.escape(str(option.get("country") or "Страна не определена"))
        city = html.escape(str(option.get("city") or option.get("destination") or "—"))
        price = _money(option.get("price") or 0)
        details = []
        depart_time = _time(option.get("departure_at"))
        if depart_time:
            details.append(depart_time)
        details.append(_stops_label(option.get("transfers")))

        airline = str(option.get("airline") or "").strip()
        flight_number = str(option.get("flight_number") or "").strip()
        if airline:
            details.append(html.escape(airline))
        if flight_number:
            details.append(html.escape(flight_number))

        lines.append(f"{index}. 🌍 <b>{country} — {city}</b> — <b>≈{price} ₽</b>")
        lines.append(f"   {' · '.join(details)}")

    lines.extend([
        "",
        "🎟 Только туда · 1 взрослый · эконом.",
        "⚠️ Цены Aviasales ориентировочные и могут измениться при переходе к покупке.",
        "🔎 Проверить направление и купить: @a_ticketbot",
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
    result = await load_indicative_destinations(origin, departure_date)
    options = result.options[:10]
    if not options:
        logger.warning("No fast international options for %s on %s", origin, departure_date)
        return 0

    text = format_channel_post(origin_label, departure_date, options)
    if result.unresolved:
        text += f"\n\nℹ️ Не удалось определить страну у {result.unresolved} строк источника; они исключены."

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    logger.info(
        "Published %d fast international destinations from %s for %s",
        len(options),
        origin,
        departure_date,
    )
    return len(options)


async def publish_tomorrow_channels(bot: Bot, *, force: bool = False) -> dict[str, int]:
    now = datetime.now(ZoneInfo(TIMEZONE))
    departure_date = (now.date() + timedelta(days=1)).isoformat()
    result: dict[str, int] = {}

    for key, origin, origin_label, configured_chat_id in CHANNELS:
        chat_id = _resolve_chat_id(key, configured_chat_id)
        if not chat_id:
            logger.warning("%s channel is not configured; skipping", key)
            result[key] = 0
            continue
        if not force and _already_published(key, departure_date):
            logger.info("%s channel already published for %s", key, departure_date)
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
            if result[key] > 0:
                _mark_published(key, departure_date)
        except Exception:
            logger.exception("Fast daily channel publication failed for %s", key)
            result[key] = 0

    return result
