import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

from bot.services.city_resolver import city_resolver
from bot.services.aviasales_api import aviasales_api
from bot.keyboards.common_kb import favorites_keyboard
from bot.utils.formatters import format_price, format_date_short, format_time
from database.crud import get_favorites, add_favorite, remove_favorite

logger = logging.getLogger(__name__)

router = Router()


@router.message(Command("favorites"))
async def cmd_favorites(message: Message):
    await _send_favorites(message.bot, message.from_user.id)


@router.callback_query(F.data == "menu:favorites")
async def cb_favorites(callback: CallbackQuery):
    await callback.answer()
    await _send_favorites(callback.bot, callback.from_user.id)


@router.callback_query(F.data.startswith("fav:"))
async def cb_add_favorite(callback: CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Ошибка")
        return

    origin, destination = parts[1], parts[2]
    added = await add_favorite(callback.from_user.id, origin, destination)

    if added:
        origin_name = city_resolver.get_city_name(origin)
        dest_name = city_resolver.get_city_name(destination)
        await callback.answer(f"Добавлено в избранное: {origin_name} -> {dest_name}")
    else:
        await callback.answer("Уже в избранном")


@router.callback_query(F.data.startswith("delfav:"))
async def cb_remove_favorite(callback: CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Ошибка")
        return

    origin, destination = parts[1], parts[2]
    removed = await remove_favorite(callback.from_user.id, origin, destination)

    if removed:
        await callback.answer("Удалено из избранного")
        try:
            await callback.message.edit_text("Удалено из избранного ✓")
        except Exception:
            pass
    else:
        await callback.answer("Не найдено")


async def _send_favorites(bot, telegram_id: int):
    """Отправляет список избранных маршрутов напрямую пользователю с актуальными ценами."""
    favorites = await get_favorites(telegram_id)

    if not favorites:
        await bot.send_message(
            telegram_id,
            "У вас пока нет избранных маршрутов.\n\nИспользуйте inline-поиск и нажмите «В избранное».",
            parse_mode="HTML",
        )
        return

    await bot.send_message(
        telegram_id,
        f"<b>Избранные маршруты ({len(favorites)}):</b>",
        parse_mode="HTML",
    )

    for fav in favorites:
        origin_name = city_resolver.get_city_name(fav.origin)
        dest_name = city_resolver.get_city_name(fav.destination)

        # Получаем актуальную цену из API с датой и временем
        price_info = "цена неизвестна"
        try:
            tickets = await aviasales_api.search_prices_for_dates(
                origin=fav.origin,
                destination=fav.destination,
                direct=True,
                limit=10,
            )
            if not tickets:
                tickets = await aviasales_api.search_prices_for_dates(
                    origin=fav.origin,
                    destination=fav.destination,
                    limit=10,
                )

            if tickets:
                cheapest = min(tickets, key=lambda t: t.get("price", float("inf")))
                price = cheapest.get("price", 0)
                if price > 0:
                    dep_at = cheapest.get("departure_at", "")
                    date_str = ""
                    time_str = ""
                    if dep_at:
                        date_str = format_date_short(dep_at[:10]) if len(dep_at) >= 10 else ""
                        time_str = format_time(dep_at) if len(dep_at) > 10 else ""

                    transfers = cheapest.get("transfers", 0)
                    tr_text = "прямой" if transfers == 0 else f"{transfers} пер."

                    parts = [format_price(price)]
                    if date_str:
                        parts.append(date_str)
                    if time_str:
                        parts.append(time_str)
                    parts.append(tr_text)
                    price_info = " | ".join(parts)
        except Exception as e:
            logger.error("Error fetching price for favorite %s-%s: %s", fav.origin, fav.destination, e)

        await bot.send_message(
            telegram_id,
            f"   {origin_name} ({fav.origin}) -> {dest_name} ({fav.destination})\n   {price_info}",
            parse_mode="HTML",
            reply_markup=favorites_keyboard(fav.origin, fav.destination),
        )
