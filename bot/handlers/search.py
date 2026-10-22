import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

from database.crud import get_search_filters
from bot.states.search import SearchStates
from bot.services.aviasales_api import aviasales_api
from bot.services.city_resolver import city_resolver
from bot.utils.formatters import (
    format_ticket_inline_title,
    format_ticket_message,
    format_price,
    format_transfers,
    format_date_short,
    format_date_full,
)
from bot.keyboards.common_kb import ticket_keyboard
from bot.keyboards.calendar_kb import calendar_keyboard

logger = logging.getLogger(__name__)

router = Router()


@router.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(SearchStates.choosing_origin)
    await message.answer(
        "<b>Поиск авиабилетов</b>\n\n"
        "Введите город вылета (название или IATA код):\n"
        "Например: <code>Москва</code> или <code>MOW</code>"
    )


@router.message(SearchStates.choosing_origin)
async def process_origin(message: Message, state: FSMContext):
    origin = city_resolver.resolve(message.text)
    if not origin:
        await message.answer(
            f"Город «{message.text}» не найден.\n"
            "Попробуйте ещё раз или используйте IATA код (MOW, LED, IST...):"
        )
        return

    origin_name = city_resolver.get_city_name(origin)
    await state.update_data(origin=origin, origin_name=origin_name)
    await state.set_state(SearchStates.choosing_destination)

    await message.answer(
        f"Вылет из: <b>{origin_name} ({origin})</b>\n\n"
        "Введите город прибытия:"
    )


@router.message(SearchStates.choosing_destination)
async def process_destination(message: Message, state: FSMContext):
    destination = city_resolver.resolve(message.text)
    if not destination:
        await message.answer(
            f"Город «{message.text}» не найден.\nПопробуйте ещё раз:"
        )
        return

    dest_name = city_resolver.get_city_name(destination)
    await state.update_data(destination=destination, dest_name=dest_name)
    await state.set_state(SearchStates.choosing_date)

    data = await state.get_data()
    await message.answer(
        f"Маршрут: <b>{data['origin_name']} -> {dest_name}</b>\n\n"
        "Выберите дату вылета в календаре. При желании дату можно ввести вручную:",
        reply_markup=calendar_keyboard("search", include_no_date=True),
    )


@router.callback_query(F.data.startswith("caldate:search:"))
async def cb_search_calendar_date(callback: CallbackQuery, state: FSMContext):
    date_str = (callback.data or "").split(":", 2)[2]
    data = await state.get_data()
    origin = data.get("origin")
    destination = data.get("destination")
    if not origin or not destination:
        await callback.answer("Поиск устарел. Начните заново.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(f"Выбрана дата: <b>{format_date_full(date_str)}</b>")
    await callback.answer()
    await _do_search(callback.message, origin, destination, date_str, callback.from_user.id)


@router.callback_query(F.data == "search:nodate")
async def cb_search_nodate(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    await state.clear()
    await _do_search(callback.message, data["origin"], data["destination"], None, callback.from_user.id)


@router.callback_query(F.data == "search:cancel")
async def cb_search_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Поиск отменён")
    await callback.message.edit_text("Поиск отменён.")


@router.message(SearchStates.choosing_date)
async def process_date(message: Message, state: FSMContext):
    from bot.utils.parsers import _parse_date
    date_str = _parse_date(message.text.strip())

    if not date_str:
        await message.answer("Не удалось распознать дату. Формат: ДД.ММ (например, 15.03)")
        return

    data = await state.get_data()
    await state.clear()
    await _do_search(message, data["origin"], data["destination"], date_str, message.from_user.id)


async def _do_search(message: Message, origin: str, destination: str, depart_date: str | None, user_id: int):
    """Выполняет поиск и отправляет результаты с учётом фильтров пользователя."""
    filters = await get_search_filters(user_id)
    direct_only = filters.direct_only
    direct_hint = "\nВключён фильтр «Без пересадок», выключить его можно в /settings."

    origin_name = city_resolver.get_city_name(origin)
    dest_name = city_resolver.get_city_name(destination)

    if depart_date:
        # --- Поиск с датой: прямые → с пересадками → ±1 день ---
        date_display = format_date_full(depart_date)
        from datetime import datetime as _dt
        _d = _dt.strptime(depart_date, "%Y-%m-%d")
        search_query_base = f"{origin}-{destination} {_d.day:02d}.{_d.month:02d}"

        await message.answer(f"Ищу рейсы {origin_name} → {dest_name} на {date_display}...")

        # 1. Прямые рейсы на точную дату
        direct_tickets = await aviasales_api.search_direct_on_date(origin, destination, depart_date)

        # 2. Все рейсы на точную дату (для секции с пересадками)
        all_on_date = await aviasales_api.search_prices_for_dates(
            origin=origin, destination=destination, depart_date=depart_date, direct=False, limit=50
        )
        for t in all_on_date:
            t.setdefault("origin", origin)
            t.setdefault("destination", destination)
            t.setdefault("price", t.get("value", 0))

        # Исключаем из "с пересадками" те, что уже есть в прямых
        direct_keys = {
            (t.get("price", 0), t.get("departure_at", "")[:16], t.get("airline", ""))
            for t in direct_tickets
        }
        connecting_tickets = [
            t for t in all_on_date
            if t.get("transfers", 1) != 0
            and (t.get("price", 0), t.get("departure_at", "")[:16], t.get("airline", "")) not in direct_keys
        ]
        # Дедупликация с пересадок
        seen_conn = set()
        unique_connecting = []
        for t in connecting_tickets:
            key = (t.get("price", 0), t.get("departure_at", "")[:16], t.get("airline", ""))
            if key not in seen_conn:
                seen_conn.add(key)
                unique_connecting.append(t)
        unique_connecting.sort(key=lambda t: (t.get("transfers", 99), t.get("departure_at", "")))
        if direct_only:
            unique_connecting = []

        if not direct_tickets and not unique_connecting:
            await message.answer(
                f"Рейсов {origin_name} → {dest_name} на {date_display} не найдено.\n"
                "Попробуйте другую дату или проверьте маршрут."
                + (direct_hint if direct_only else "")
            )
        else:
            # Прямые рейсы
            if direct_tickets:
                await message.answer(
                    f"<b>✈️ Прямые рейсы — {date_display}</b> ({len(direct_tickets)} вар.)"
                )
                for ticket in direct_tickets:
                    ticket_link = aviasales_api.build_ticket_link(ticket, origin, destination, depart_date)
                    text = format_ticket_message(ticket, ticket_link)
                    await message.answer(
                        text,
                        disable_web_page_preview=True,
                        reply_markup=ticket_keyboard(
                            ticket_link, origin, destination, search_query_base,
                            depart_date=depart_date,
                        ),
                    )
            else:
                await message.answer(f"Прямых рейсов на {date_display} не найдено.")

            # Рейсы с пересадками
            if unique_connecting:
                await message.answer(
                    f"<b>↗️ С пересадками — {date_display}</b> ({len(unique_connecting)} вар.)"
                )
                for ticket in unique_connecting[:5]:
                    ticket_link = aviasales_api.build_ticket_link(ticket, origin, destination, depart_date)
                    text = format_ticket_message(ticket, ticket_link)
                    await message.answer(
                        text,
                        disable_web_page_preview=True,
                        reply_markup=ticket_keyboard(
                            ticket_link, origin, destination, search_query_base,
                            depart_date=depart_date,
                        ),
                    )

        # ±1 день — самые дешёвые для сравнения
        nearby = await aviasales_api.get_nearby_cheapest(origin, destination, depart_date, days=1)
        if direct_only:
            nearby = [(d, t) for d, t in nearby if t.get("transfers", 99) == 0]
        if nearby:
            lines = ["<b>Самые дешёвые в соседние дни:</b>"]
            for date_str, t in nearby:
                price = format_price(t.get("price", 0))
                tr = t.get("transfers", 99)
                tr_text = "прямой" if tr == 0 else format_transfers(tr)
                search_link = aviasales_api.build_search_link(origin, destination, date_str)
                lines.append(
                    f"📅 {format_date_short(date_str)}: {price} ({tr_text}) "
                    f"— <a href=\"{search_link}\">смотреть</a>"
                )
            await message.answer("\n".join(lines), disable_web_page_preview=True)

    else:
        # --- Без даты ---
        if filters.cheapest_month:
            # Самые дешёвые на ближайший месяц, от дешёвых к дорогим
            await message.answer(f"Ищу самые дешёвые билеты {origin_name} → {dest_name} на ближайший месяц...")
            tickets = await aviasales_api.search_cheapest_in_window(origin, destination)
            period_text = " на ближайший месяц"
            headline = "Самые дешёвые на ближайший месяц — найдено"
        else:
            # Прежнее поведение: сначала прямые, потом по времени вылета
            await message.answer(f"Ищу билеты {origin_name} → {dest_name}...")
            tickets = await aviasales_api.search_all_options(
                origin=origin,
                destination=destination,
                depart_date=None,
            )
            period_text = ""
            headline = "Найдено"

        if direct_only:
            tickets = [t for t in tickets if t.get("transfers", 0) == 0]

        if not tickets:
            await message.answer(
                f"Билеты {origin_name} → {dest_name}{period_text} не найдены.\n"
                "Попробуйте выбрать конкретную дату или другой маршрут."
                + (direct_hint if direct_only else "")
            )
            return

        await message.answer(f"<b>{headline} {len(tickets)} вариантов:</b>\n")

        for ticket in tickets[:5]:
            ticket_link = aviasales_api.build_ticket_link(ticket, origin, destination, None)
            text = format_ticket_message(ticket, ticket_link)
            await message.answer(
                text,
                disable_web_page_preview=True,
                reply_markup=ticket_keyboard(ticket_link, origin, destination, f"{origin}-{destination}"),
            )

        if len(tickets) > 5:
            await message.answer(
                f"Показаны 5 из {len(tickets)}.\n"
                "Для полного списка используйте inline-поиск:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="Показать все в inline",
                        switch_inline_query_current_chat=f"{origin}-{destination}",
                    )],
                ]),
            )
