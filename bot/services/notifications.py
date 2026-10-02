import logging

from aiogram import Bot

from bot.services.city_resolver import city_resolver
from bot.services.aviasales_api import aviasales_api
from bot.utils.formatters import format_price, format_date_full, format_transfers
from bot.keyboards.common_kb import ticket_keyboard
from database.models import PriceSubscription

logger = logging.getLogger(__name__)


async def notify_price_drop(bot: Bot, telegram_id: int, sub: PriceSubscription,
                            current_price: int, ticket: dict):
    """Отправляет уведомление о снижении цены."""
    origin_name = city_resolver.get_city_name(sub.origin)
    dest_name = city_resolver.get_city_name(sub.destination)

    old_price_str = format_price(sub.last_price) if sub.last_price else "неизвестно"
    new_price_str = format_price(current_price)

    diff = ""
    if sub.last_price and sub.last_price > current_price:
        saved = sub.last_price - current_price
        diff = f"\n   Экономия: {format_price(saved)}"

    depart = format_date_full(ticket.get("departure_at", ""))
    transfers = format_transfers(ticket.get("transfers", 0))
    airline_code = ticket.get("airline", "")
    airline = city_resolver.get_airline_name(airline_code) if airline_code else ""

    search_link = aviasales_api.build_search_link(sub.origin, sub.destination)

    # Информация о дате подписки
    sub_date_info = ""
    if sub.depart_date:
        sub_date_info = f"\n   Дата подписки: {sub.depart_date.strftime('%d.%m.%Y')}"
    else:
        sub_date_info = "\n   Мониторинг: ближайшие 30 дней"

    text = (
        f"   <b>Цена снизилась!</b>\n\n"
        f"   {origin_name} ({sub.origin}) -> {dest_name} ({sub.destination}){sub_date_info}\n"
        f"   Было: {old_price_str}\n"
        f"   Стало: <b>{new_price_str}</b>{diff}\n"
    )
    if airline:
        text += f"   {airline}\n"
    if depart:
        text += f"   {depart}\n"
    text += f"   {transfers}\n"
    text += f"\n   <a href=\"{search_link}\">Купить на Aviasales</a>"

    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=ticket_keyboard(
                search_link, sub.origin, sub.destination,
                f"{sub.origin}-{sub.destination}",
                depart_date=sub.depart_date.strftime("%Y-%m-%d") if sub.depart_date else None,
                return_date=sub.return_date.strftime("%Y-%m-%d") if sub.return_date else None,
            ),
        )
        logger.info("Price drop notification sent to %d: %s-%s", telegram_id, sub.origin, sub.destination)
    except Exception as e:
        logger.error("Failed to send notification to %d: %s", telegram_id, e)
