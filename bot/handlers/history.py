import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

from bot.services.city_resolver import city_resolver
from bot.utils.formatters import format_date_short
from bot.keyboards.common_kb import history_entry_keyboard
from database.crud import get_search_history

logger = logging.getLogger(__name__)

router = Router()


@router.message(Command("history"))
async def cmd_history(message: Message):
    await _show_history(message)


@router.callback_query(F.data == "menu:history")
async def cb_history(callback: CallbackQuery):
    await _show_history(callback.message, edit=True)
    await callback.answer()


async def _show_history(message: Message, edit: bool = False):
    history = await get_search_history(message.chat.id)

    if not history:
        text = "История поиска пуста.\n\nИспользуйте inline-поиск, чтобы найти билеты."
        if edit:
            await message.edit_text(text)
        else:
            await message.answer(text)
        return

    header = f"<b>Последние поиски ({len(history)}):</b>"
    if edit:
        await message.edit_text(header)
    else:
        await message.answer(header)

    for entry in history:
        origin_name = city_resolver.get_city_name(entry.origin)

        if entry.search_type == "route" and entry.destination:
            dest_name = city_resolver.get_city_name(entry.destination)
            route = f"{origin_name} -> {dest_name}"
        else:
            dest_name = None
            route = f"Дешёвые из {origin_name}"

        date_str = ""
        date_short = None
        if entry.depart_date:
            date_short = entry.depart_date.strftime("%d.%m")
            date_str = f" | {format_date_short(str(entry.depart_date))}"

        created = entry.created_at.strftime("%d.%m %H:%M") if entry.created_at else ""

        text = f"<code>{route}{date_str}</code>\n{entry.results_count} рез. — {created}"

        await message.answer(
            text,
            reply_markup=history_entry_keyboard(
                entry.origin,
                entry.destination,
                date_short,
            ),
        )
