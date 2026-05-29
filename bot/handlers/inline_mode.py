import hashlib
import logging
import asyncio
from datetime import datetime, timedelta

from aiogram import Router
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
)

from bot.services.aviasales_api import aviasales_api
from bot.services.city_resolver import city_resolver
from bot.utils.parsers import parse_inline_query
from bot.utils.formatters import (
    format_ticket_inline_title,
    format_ticket_inline_description,
    format_ticket_message,
    format_direction_inline_title,
    format_direction_inline_description,
    format_ticket_message,
    format_price,
    format_transfers,
    format_time,
)
from bot.keyboards.common_kb import ticket_keyboard, connections_shortcut_keyboard
from database.database import async_session_maker
from database.models import User, SearchHistory, Favorite
from database.crud import get_favorites
from sqlalchemy import select

logger = logging.getLogger(__name__)

router = Router()


def _build_search_query(origin: str, destination: str, depart_date: str | None = None, return_date: str | None = None) -> str:
    """Builds inline search query string like 'MOW-IST 15.03' or 'MOW-IST 15.03-22.03'."""
    query = f"{origin}-{destination}"
    if depart_date and len(depart_date) >= 10:
        day = depart_date[8:10]
        month = depart_date[5:7]
        query += f" {day}.{month}"
        if return_date and len(return_date) >= 10:
            rday = return_date[8:10]
            rmonth = return_date[5:7]
            query += f"-{rday}.{rmonth}"
    return query


def _build_connections_query(origin: str, destination: str, depart_date: str | None = None) -> str:
    query = f"пересадки {origin}-{destination}"
    if depart_date and len(depart_date) >= 10:
        day = depart_date[8:10]
        month = depart_date[5:7]
        query += f" {day}.{month}"
    return query


def _build_inline_day_label(date_str: str, start_date) -> str:
    months = {
        1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
        7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек",
    }
    weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    try:
        dep_date = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
    except ValueError:
        return date_str[:10]

    delta = (dep_date - start_date).days
    if delta == 0:
        return "Сегодня"
    if delta == 1:
        return "Завтра"
    if delta == 2:
        return "Послезавтра"
    return f"{weekdays[dep_date.weekday()]}, {dep_date.day} {months[dep_date.month]}"


async def _search_route_without_date(
    origin: str,
    destination: str,
    start_date=None,
    horizon_days: int = 7,
    connections_only: bool = False,
) -> list[dict]:
    """
    Быстрый inline-поиск без даты.
    Использует только лёгкий endpoint по каждому дню окна поиска,
    чтобы успевать ответить в лимит Telegram.
    """
    if start_date is None:
        start_date = datetime.now().date()

    search_dates = []
    tasks = []
    for day_offset in range(max(1, horizon_days)):
        search_date = start_date + timedelta(days=day_offset)
        date_str = search_date.strftime("%Y-%m-%d")
        search_dates.append(date_str)
        if connections_only:
            tasks.append(
                aviasales_api.search_prices_for_dates(
                    origin=origin,
                    destination=destination,
                    depart_date=date_str,
                    return_date=None,
                    one_way=True,
                    direct=False,
                    limit=30,
                    unique=False,
                    sorting="price",
                )
            )
        else:
            tasks.append(
                aviasales_api.search_direct_on_date(
                    origin=origin,
                    destination=destination,
                    depart_date=date_str,
                )
            )

    results = await asyncio.gather(*tasks, return_exceptions=True)

    tickets = []
    for date_str, day_result in zip(search_dates, results):
        if isinstance(day_result, Exception):
            logger.error("No-date search failed for %s-%s on %s: %s", origin, destination, date_str, day_result)
            continue
        if not isinstance(day_result, list):
            continue

        for ticket in day_result:
            if "value" in ticket and "price" not in ticket:
                ticket["price"] = ticket["value"]
            if "depart_date" in ticket and "departure_at" not in ticket:
                ticket["departure_at"] = ticket["depart_date"]
            if not ticket.get("departure_at"):
                ticket["departure_at"] = date_str
            if "return_date" in ticket and "return_at" not in ticket:
                ticket["return_at"] = ticket["return_date"]
            if "number_of_changes" in ticket and "transfers" not in ticket:
                ticket["transfers"] = ticket["number_of_changes"]
            if connections_only and ticket.get("transfers", 0) <= 0:
                continue

            ticket.setdefault("origin", origin)
            ticket.setdefault("destination", destination)
            ticket.setdefault("price", ticket.get("value", 0))
            ticket.setdefault("transfers", 0)
            ticket["inline_day_label"] = _build_inline_day_label(ticket["departure_at"], start_date)
            tickets.append(ticket)

    return tickets


def _build_connections_shortcut_result(
    origin: str,
    destination: str,
    origin_name: str,
    destination_name: str,
    depart_date: str | None = None,
) -> InlineQueryResultArticle:
    query = _build_connections_query(origin, destination, depart_date)
    return InlineQueryResultArticle(
        id=f"connections-{origin}-{destination}-{depart_date or 'nodate'}",
        title=f"↗️ Рейсы с пересадками: {origin_name} → {destination_name}",
        description="Открыть отдельный поиск вариантов с пересадками",
        input_message_content=InputTextMessageContent(
            message_text=(
                f"Рейсы с пересадками по маршруту {origin_name} → {destination_name}.\n"
                "Нажмите кнопку ниже, чтобы открыть отдельный поиск."
            ),
        ),
        reply_markup=connections_shortcut_keyboard(query),
    )

RESULTS_PER_PAGE = 10

HELP_TEXT = (
    "<b>Поиск авиабилетов</b>\n\n"
    "Введите запрос в формате:\n\n"
    "<code>Москва-Стамбул</code> — билеты на русском ✨\n"
    "<code>Москва-Стамбул 15.03</code> — на конкретную дату\n"
    "<code>Москва-Стамбул 15.03-22.03</code> — туда-обратно\n"
    "<code>Москва</code> — дешёвые из города\n"
    "<code>избранные</code> — ваши сохранённые маршруты\n\n"
    "Можно использовать названия городов на русском или IATA коды (MOW, IST, LED...)."
)


@router.inline_query()
async def handle_inline_query(inline_query: InlineQuery):
    query_text = inline_query.query.strip()
    offset = int(inline_query.offset) if inline_query.offset else 0

    parsed = parse_inline_query(query_text)

    if parsed.query_type == "help":
        await _send_help(inline_query)
        return

    if parsed.query_type == "route":
        await _handle_route_search(inline_query, parsed, offset)
    elif parsed.query_type == "cheapest":
        await _handle_cheapest_search(inline_query, parsed, offset)
    elif parsed.query_type == "favorites":
        await _handle_favorites_search(inline_query, offset)


async def _send_help(inline_query: InlineQuery):
    results = [
        InlineQueryResultArticle(
            id="help",
            title="Как искать авиабилеты",
            description="MOW-IST, MOW-IST 15.03, Москва...",
            input_message_content=InputTextMessageContent(
                message_text=HELP_TEXT,
                parse_mode="HTML",
            ),
        )
    ]
    await inline_query.answer(results, cache_time=300, is_personal=False)


async def _handle_route_search(inline_query: InlineQuery, parsed, offset: int):
    origin = city_resolver.resolve(parsed.raw_origin)
    destination = city_resolver.resolve(parsed.raw_destination)
    origin_name = city_resolver.get_city_name(origin) if origin else parsed.raw_origin
    destination_name = city_resolver.get_city_name(destination) if destination else parsed.raw_destination

    if not origin or not destination:
        not_found = parsed.raw_origin if not origin else parsed.raw_destination
        results = [
            InlineQueryResultArticle(
                id="not_found",
                title=f"Город не найден: {not_found}",
                description="Попробуйте IATA код (MOW, IST, LED...)",
                input_message_content=InputTextMessageContent(
                    message_text=f"Город не найден: {not_found}. Используйте IATA коды.",
                ),
            )
        ]
        await inline_query.answer(results, cache_time=10, is_personal=True)
        return

    parsed.origin = origin
    parsed.destination = destination

    # Если дата не указана, используем быстрый параллельный поиск по окну дат.
    tickets = []
    if not parsed.depart_date:
        tickets = await _search_route_without_date(
            origin,
            destination,
            connections_only=parsed.connections_only,
        )
    else:
        # Если дата указана — ищем для этой даты, при нехватке подгружаем соседние дни
        tickets = await aviasales_api.search_all_options(
            origin=origin,
            destination=destination,
            depart_date=parsed.depart_date,
            return_date=parsed.return_date,
        )
        if parsed.connections_only:
            tickets = [t for t in tickets if t.get("transfers", 0) > 0]

        # Подгружаем ±1-2 дня параллельно для показа прямых рейсов на соседние даты
        try:
            base_date = datetime.strptime(parsed.depart_date, "%Y-%m-%d").date()
            import asyncio as _aio
            nearby_tasks = []
            for delta in [1, -1, 2, -2]:
                nearby_date = base_date + timedelta(days=delta)
                nearby_str = nearby_date.strftime("%Y-%m-%d")
                nearby_tasks.append(
                    aviasales_api.search_all_options(
                        origin=origin,
                        destination=destination,
                        depart_date=nearby_str,
                        return_date=None,
                    )
                )
            nearby_results = await _aio.gather(*nearby_tasks, return_exceptions=True)
            for res in nearby_results:
                if isinstance(res, list):
                    if parsed.connections_only:
                        tickets.extend(t for t in res if t.get("transfers", 0) > 0)
                    else:
                        tickets.extend(res)
        except ValueError:
            pass

    # Дедупликация по (цена + время вылета + авиакомпания + пересадки)
    seen = set()
    unique_tickets = []
    for t in tickets:
        price = t.get("price") or t.get("value", 0)
        dep = (t.get("departure_at") or t.get("depart_date", ""))[:16]
        airline = t.get("airline", "")
        transfers = t.get("transfers", 0)
        key = (price, dep, airline, transfers)
        if key not in seen:
            seen.add(key)
            unique_tickets.append(t)
    tickets = unique_tickets

    # Сортировка
    if not parsed.depart_date:
        tickets.sort(
            key=lambda t: (
                t.get("departure_at") or t.get("depart_date", ""),
                t.get("price", float("inf")),
            )
        )
    else:
        # С датой: приоритет — прямые на точную дату (по времени), затем прямые на соседние (по цене), затем с пересадками (по цене)
        def _sort_key(t):
            transfers = t.get("transfers", 99)
            is_direct = 0 if transfers == 0 else 1
            dep_str = (t.get("departure_at") or t.get("depart_date", ""))[:10]
            is_exact_date = 0 if dep_str == parsed.depart_date else 1
            dep_time = (t.get("departure_at") or t.get("depart_date", ""))[:16]
            price = t.get("price", float("inf"))
            # Группа 0: прямые на точную дату — сортируем по времени вылета
            # Группа 1: прямые на другие дни — сортируем по цене
            # Группа 2: с пересадками на точную дату — по цене
            # Группа 3: с пересадками на другие дни — по цене
            group = is_direct * 2 + is_exact_date
            if group == 0:  # прямые на точную дату
                return (group, dep_time, price)
            else:
                return (group, price, dep_time)
        tickets.sort(key=_sort_key)

    await _save_search(inline_query.from_user.id, origin, destination,
                       parsed.depart_date, parsed.return_date, "route", len(tickets))

    has_connections_shortcut = not parsed.connections_only
    page_size = RESULTS_PER_PAGE - 1 if has_connections_shortcut and offset == 0 else RESULTS_PER_PAGE
    page = tickets[offset:offset + page_size]

    results = []
    if not page:
        results.append(
            InlineQueryResultArticle(
                id="no_results",
                title=f"Билеты не найдены: {origin_name} → {destination_name}",
                description="Попробуйте указать дату или повторить поиск позже",
                input_message_content=InputTextMessageContent(
                    message_text=(
                        f"По маршруту {origin_name} → {destination_name} пока не нашлось билетов.\n"
                        "Попробуйте указать дату или повторить поиск чуть позже."
                    ),
                ),
            )
        )

    for i, ticket in enumerate(page):
        ticket_id = hashlib.md5(
            f"{ticket.get('origin','')}-{ticket.get('destination','')}-"
            f"{ticket.get('departure_at','')}-{ticket.get('price',0)}-{i}".encode()
        ).hexdigest()[:16]

        ticket_link = aviasales_api.build_ticket_link(
            ticket, origin, destination, parsed.depart_date, parsed.return_date,
        )

        results.append(
            InlineQueryResultArticle(
                id=ticket_id,
                title=format_ticket_inline_title(ticket),
                description=format_ticket_inline_description(ticket),
                input_message_content=InputTextMessageContent(
                    message_text=format_ticket_message(ticket, ticket_link),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                ),
                reply_markup=ticket_keyboard(
                    ticket_link, origin, destination,
                    _build_search_query(origin, destination, parsed.depart_date, parsed.return_date),
                    depart_date=parsed.depart_date,
                    return_date=parsed.return_date,
                ),
                thumb_url="https://twemoji.maxcdn.com/v/latest/72x72/2708.png",  # ✈️
            )
        )

    if page and has_connections_shortcut and offset == 0:
        results.append(
            _build_connections_shortcut_result(
                origin=origin,
                destination=destination,
                origin_name=origin_name,
                destination_name=destination_name,
                depart_date=parsed.depart_date,
            )
        )

    next_offset = str(offset + page_size) if len(page) == page_size and len(tickets) > offset + page_size else ""

    await inline_query.answer(
        results,
        cache_time=60,
        is_personal=True,
        next_offset=next_offset,
        switch_pm_text="🤖 Открыть бот",
        switch_pm_parameter="start",
    )


async def _handle_cheapest_search(inline_query: InlineQuery, parsed, offset: int):
    origin = city_resolver.resolve(parsed.raw_origin)

    if not origin:
        results = [
            InlineQueryResultArticle(
                id="not_found",
                title=f"Город не найден: {parsed.raw_origin}",
                description="Попробуйте IATA код (MOW, IST, LED...)",
                input_message_content=InputTextMessageContent(
                    message_text=f"Город не найден: {parsed.raw_origin}. Используйте IATA коды.",
                ),
            )
        ]
        await inline_query.answer(results, cache_time=10, is_personal=True)
        return

    parsed.origin = origin
    origin_name = city_resolver.get_city_name(origin)

    # Получаем избранные маршруты из этого города
    all_favorites = await get_favorites(inline_query.from_user.id)
    favorites_from_origin = [f for f in all_favorites if f.origin == origin]

    results = []
    current_index = 0

    # 1️⃣ Показываем избранные маршруты на ближайшие 3 дня
    if favorites_from_origin and offset == 0:
        results.append(
            InlineQueryResultArticle(
                id="fav_section",
                title="⭐ Избранные маршруты из " + origin_name,
                description=f"Найдено {len(favorites_from_origin)} маршрутов",
                input_message_content=InputTextMessageContent(
                    message_text=(
                        f"<b>⭐ Избранные маршруты из {origin_name}</b>\n\n"
                        f"Ищу цены на ближайшие 3 дня..."
                    ),
                    parse_mode="HTML",
                ),
            )
        )

        # Ищем цены на ближайшие 3 дня для избранных маршрутов
        today = datetime.now().date()

        for i, fav in enumerate(favorites_from_origin[:5]):  # Показываем максимум 5 избранных
            for day_offset in range(3):  # 3 дня
                search_date = today + timedelta(days=day_offset)
                date_str = search_date.strftime("%Y-%m-%d")
                date_display = search_date.strftime("%d.%m")

                try:
                    tickets = await aviasales_api.search_all_options(
                        origin=fav.origin,
                        destination=fav.destination,
                        depart_date=date_str,
                    )

                    if tickets:
                        cheapest = min(tickets, key=lambda t: t.get("price", float("inf")))
                        price = cheapest.get("price", 0)
                        price_str = format_price(price)

                        dest_name = city_resolver.get_city_name(fav.destination)
                        ticket_link = aviasales_api.build_ticket_link(
                            cheapest, fav.origin, fav.destination, date_str
                        )

                        msg_text = (
                            f"   🔍 Поиск: <code>{origin_name}-{dest_name} {date_display}</code>\n"
                            f"   💰 Цена: <b>{price_str}</b>\n"
                            f"\n   🔗 <a href=\"{ticket_link}\">Купить на Aviasales</a>"
                        )

                        result_id = hashlib.md5(
                            f"fav-{fav.origin}-{fav.destination}-{day_offset}".encode()
                        ).hexdigest()[:16]

                        # Информация о рейсе для description
                        airline_code = cheapest.get("airline", "")
                        airline = city_resolver.get_airline_name(airline_code) if airline_code else ""
                        transfers = cheapest.get("transfers", 0)
                        transfers_str = format_transfers(transfers)
                        time = format_time(cheapest.get("departure_at", ""))

                        desc_parts = []
                        if airline:
                            desc_parts.append(airline)
                        desc_parts.append(transfers_str)
                        if time:
                            desc_parts.append(time)
                        description = " | ".join(desc_parts)

                        results.append(
                            InlineQueryResultArticle(
                                id=result_id,
                                title=f"⭐ {dest_name} ({date_display})",
                                description=f"💰 {price_str} | {description}",
                                input_message_content=InputTextMessageContent(
                                    message_text=msg_text,
                                    parse_mode="HTML",
                                    disable_web_page_preview=True,
                                ),
                                reply_markup=ticket_keyboard(
                                    ticket_link, fav.origin, fav.destination,
                                    _build_search_query(fav.origin, fav.destination, date_str),
                                    depart_date=date_str,
                                ),
                            )
                        )
                except Exception as e:
                    logger.error("Error searching favorite route %s-%s: %s", fav.origin, fav.destination, e)

    # 2️⃣ Показываем дешёвые направления (на ближайшую неделю)
    directions = await aviasales_api.get_city_directions(origin)

    # Фильтруем только на ближайшую неделю (7 дней)
    today = datetime.now().date()
    next_week = today + timedelta(days=7)

    filtered_directions = {}
    for dest_code, info in directions.items():
        depart_date_str = info.get("departure_at", "")
        if depart_date_str:
            try:
                depart_date = datetime.strptime(depart_date_str[:10], "%Y-%m-%d").date()
                if today <= depart_date <= next_week:
                    filtered_directions[dest_code] = info
            except Exception:
                filtered_directions[dest_code] = info  # Если ошибка парсинга - берем
        else:
            filtered_directions[dest_code] = info

    directions = filtered_directions
    await _save_search(inline_query.from_user.id, origin, None, None, None, "cheapest", len(directions))

    if len(results) > 0 and offset == 0:
        # Добавляем разделитель если были избранные
        results.append(
            InlineQueryResultArticle(
                id="cheapest_section",
                title="💰 Самые дешёвые популярные направления",
                description=f"Рекомендуемые маршруты из {origin_name}",
                input_message_content=InputTextMessageContent(
                    message_text=(
                        f"<b>💰 Популярные направления из {origin_name}</b>\n\n"
                        "Кликните на маршрут для поиска билетов"
                    ),
                    parse_mode="HTML",
                ),
            )
        )

    # Пагинация для дешёвых направлений
    items = list(directions.items())
    # Берём с учётом смещения, но если это первая страница, пропускаем заголовки
    if offset == 0:
        cheapest_page = items[0:RESULTS_PER_PAGE - len(results)]
    else:
        # Смещение относительно всех результатов
        cheapest_start = offset - (len(results) if offset < len(results) else 0)
        cheapest_page = items[cheapest_start:cheapest_start + RESULTS_PER_PAGE]

    for i, (dest_code, info) in enumerate(cheapest_page):
        item_id = hashlib.md5(f"{origin}-{dest_code}-{i}".encode()).hexdigest()[:16]
        search_link = aviasales_api.build_search_link(origin, dest_code)
        dest_name = city_resolver.get_city_name(dest_code)
        price = format_price(info.get("price", 0))

        # Извлекаем дату вылета для передачи в клавиатуру
        dir_depart_date = None
        dir_depart_str = info.get("departure_at", "")
        if dir_depart_str and len(dir_depart_str) >= 10:
            dir_depart_date = dir_depart_str[:10]

        msg_lines = [
            f"   🛫 Поиск: <code>{origin}-{dest_name}</code>",
            f"   💰 Цена: {price}",
        ]
        airline_code = info.get("airline", "")
        if airline_code:
            msg_lines.append(f"   ✈️ {city_resolver.get_airline_name(airline_code)}")
        msg_lines.append(f"\n   🔗 <a href=\"{search_link}\">Найти и купить на Aviasales</a>")

        results.append(
            InlineQueryResultArticle(
                id=item_id,
                title=f"💰 {dest_name}",
                description=format_direction_inline_description(info),
                input_message_content=InputTextMessageContent(
                    message_text="\n".join(msg_lines),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                ),
                reply_markup=ticket_keyboard(
                    search_link, origin, dest_code,
                    _build_search_query(origin, dest_code),
                    depart_date=dir_depart_date,
                ),
                thumb_url="https://twemoji.maxcdn.com/v/latest/72x72/1f4b0.png",  # 💰
            )
        )

    next_offset = str(offset + RESULTS_PER_PAGE) if len(results) == RESULTS_PER_PAGE else ""

    await inline_query.answer(
        results,
        cache_time=60,
        is_personal=True,
        next_offset=next_offset,
        switch_pm_text="🤖 Открыть бот",
        switch_pm_parameter="start",
    )


async def _handle_favorites_search(inline_query: InlineQuery, offset: int):
    """Показывает избранные маршруты пользователя с актуальными ценами."""
    favorites = await get_favorites(inline_query.from_user.id)

    if not favorites:
        results = [
            InlineQueryResultArticle(
                id="no_favorites",
                title="Нет избранных маршрутов",
                description="Добавьте маршруты через кнопку 'В избранное' в поиске",
                input_message_content=InputTextMessageContent(
                    message_text=(
                        "<b>У вас пока нет избранных маршрутов</b>\n\n"
                        "Чтобы добавить маршрут в избранное:\n"
                        "1. Выполните поиск (например, 'Москва-Стамбул')\n"
                        "2. Нажмите кнопку <b>«В избранное»</b> ⭐\n\n"
                        "Или используйте инлайн-поиск по интересующему маршруту."
                    ),
                    parse_mode="HTML",
                ),
            )
        ]
        await inline_query.answer(results, cache_time=60, is_personal=True)
        return
    page = favorites[offset:offset + RESULTS_PER_PAGE]

    results = []

    for i, fav in enumerate(page):
        origin_name = city_resolver.get_city_name(fav.origin)
        dest_name = city_resolver.get_city_name(fav.destination)

        # Получаем актуальную цену из API
        price_str = "загрузка..."
        try:
            tickets = await aviasales_api.search_all_options(
                origin=fav.origin,
                destination=fav.destination,
            )
            if tickets:
                cheapest = min(tickets, key=lambda t: t.get("price", float("inf")))
                current_price = cheapest.get("price", 0)
                if current_price > 0:
                    price_str = format_price(current_price)
                else:
                    price_str = "нет цены"
            else:
                price_str = "нет билетов"
        except Exception as e:
            logger.error("Error fetching price for favorite %s-%s: %s", fav.origin, fav.destination, e)
            price_str = "ошибка"

        fav_id = hashlib.md5(f"fav-{fav.origin}-{fav.destination}-{i}".encode()).hexdigest()[:16]

        # Формируем текст сообщения с информацией о маршруте
        text = (
            f"   🔍 Поиск: <code>{origin_name}-{dest_name}</code>\n"
            f"   💰 Цена: <b>{price_str}</b>\n\n"
            f"   🔗 <a href=\"https://www.aviasales.ru/search?origin={fav.origin}&destination={fav.destination}\">Найти и купить на Aviasales</a>"
        )

        # Сохраняем поиск в историю
        await _save_search(
            inline_query.from_user.id,
            fav.origin,
            fav.destination,
            None,
            None,
            "favorite",
            1
        )

        # Избранное без даты — подписка будет "на ближайший месяц"
        results.append(
            InlineQueryResultArticle(
                id=fav_id,
                title=f"⭐ {origin_name} → {dest_name}",
                description=f"👉 {price_str}",
                input_message_content=InputTextMessageContent(
                    message_text=text,
                    parse_mode="HTML",
                ),
                reply_markup=ticket_keyboard(
                    aviasales_api.build_search_link(fav.origin, fav.destination),
                    fav.origin,
                    fav.destination,
                    _build_search_query(fav.origin, fav.destination),
                ),
                thumb_url="https://twemoji.maxcdn.com/v/latest/72x72/2b50.png",  # ⭐
            )
        )

    next_offset = str(offset + RESULTS_PER_PAGE) if len(page) == RESULTS_PER_PAGE else ""

    await inline_query.answer(
        results,
        cache_time=60,
        is_personal=True,
        next_offset=next_offset,
        switch_pm_text="🤖 Открыть бот",
        switch_pm_parameter="start",
    )


async def _save_search(telegram_id: int, origin: str, destination: str | None,
                       depart_date, return_date, search_type: str, results_count: int):
    """Сохраняет поиск в историю."""
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(User).where(User.telegram_id == telegram_id)
            )
            user = result.scalar_one_or_none()
            if not user:
                user = User(telegram_id=telegram_id)
                session.add(user)
                await session.flush()

            from datetime import date as date_type
            depart = None
            if depart_date:
                try:
                    depart = datetime.strptime(depart_date, "%Y-%m-%d").date()
                except ValueError:
                    pass
            ret = None
            if return_date:
                try:
                    ret = datetime.strptime(return_date, "%Y-%m-%d").date()
                except ValueError:
                    pass

            entry = SearchHistory(
                user_id=user.id,
                origin=origin,
                destination=destination,
                depart_date=depart,
                return_date=ret,
                search_type=search_type,
                results_count=results_count,
            )
            session.add(entry)
            await session.commit()
    except Exception as e:
        logger.error("Failed to save search history: %s", e)
