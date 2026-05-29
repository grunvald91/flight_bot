from datetime import datetime

from bot.services.city_resolver import city_resolver


def format_price(price: int | float) -> str:
    """12450 -> '12 450 руб.'"""
    return f"{int(price):,}".replace(",", " ") + " руб."


def format_date_short(date_str: str | None) -> str:
    """2026-03-15 -> '15 мар'"""
    if not date_str:
        return ""
    months = {
        1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
        7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек",
    }
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        return f"{dt.day} {months[dt.month]}"
    except (ValueError, KeyError):
        return date_str[:10]


def format_date_full(date_str: str | None) -> str:
    """2026-03-15T08:30:00 -> '15 марта 2026'"""
    if not date_str:
        return ""
    months = {
        1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
        7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
    }
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        return f"{dt.day} {months[dt.month]} {dt.year}"
    except (ValueError, KeyError):
        return date_str[:10]


def format_time(date_str: str | None) -> str:
    """2026-03-15T08:30:00 -> '08:30'"""
    if not date_str:
        return ""
    try:
        if "T" in date_str:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            return dt.strftime("%H:%M")
    except ValueError:
        pass
    return ""


def format_transfers(transfers: int) -> str:
    if transfers == 0:
        return "Прямой рейс"
    elif transfers == 1:
        return "1 пересадка"
    elif transfers < 5:
        return f"{transfers} пересадки"
    else:
        return f"{transfers} пересадок"


def format_ticket_inline_title(ticket: dict) -> str:
    """Заголовок для inline-результата (только маршрут, без цены)."""
    origin = city_resolver.get_city_name(ticket.get("origin", ""))
    dest = city_resolver.get_city_name(ticket.get("destination", ""))
    day_label = ticket.get("inline_day_label")

    # Добавляем иконку для визуального различия
    transfers = ticket.get("transfers", 0)
    if transfers == 0:
        icon = "🛫"  # Прямой рейс
    else:
        icon = "↗️"  # Рейс с пересадками

    route_title = f"{icon} {origin} → {dest}"
    if day_label:
        return f"{day_label} · {route_title}"
    return route_title


def format_ticket_inline_description(ticket: dict) -> str:
    """Описание для inline-результата (цена + детали рейса)."""
    price = format_price(ticket.get("price", 0))
    airline_code = ticket.get("airline", "")
    airline = city_resolver.get_airline_name(airline_code) if airline_code else ""
    transfers = format_transfers(ticket.get("transfers", 0))
    depart = format_date_short(ticket.get("departure_at", ""))
    time = format_time(ticket.get("departure_at", ""))

    parts = [f"💰 {price}"]
    if airline:
        parts.append(airline)
    parts.append(transfers)
    if depart:
        date_time = depart
        if time:
            date_time += f", {time}"
        parts.append(date_time)

    return " | ".join(parts)


def format_ticket_message(ticket: dict, search_link: str) -> str:
    """Полное сообщение при выборе inline-результата."""
    origin_code = ticket.get("origin", "")
    dest_code = ticket.get("destination", "")
    origin = city_resolver.get_city_name(origin_code)
    dest = city_resolver.get_city_name(dest_code)
    price = format_price(ticket.get("price", 0))
    airline_code = ticket.get("airline", "")
    airline = city_resolver.get_airline_name(airline_code) if airline_code else "—"
    depart_full = format_date_full(ticket.get("departure_at", ""))
    depart_short = ticket.get("departure_at", "")[:10]  # YYYY-MM-DD
    time = format_time(ticket.get("departure_at", ""))
    transfers = format_transfers(ticket.get("transfers", 0))

    ret_date = ticket.get("return_at", "")
    return_line = ""
    ret_short = ""
    if ret_date:
        ret_formatted = format_date_full(ret_date)
        ret_time = format_time(ret_date)
        ret_short = ret_date[:10]
        return_line = f"\n   Обратно: {ret_formatted}"
        if ret_time:
            return_line += f", {ret_time}"

    # Форматируем поисковый запрос для копирования
    search_query = f"{origin}-{dest}"
    if depart_short and depart_short != "":
        # Преобразуем дату из YYYY-MM-DD в ДД.MM
        try:
            dt = datetime.strptime(depart_short, "%Y-%m-%d")
            date_short = f"{dt.day:02d}.{dt.month:02d}"
            search_query += f" {date_short}"
            if ret_short:
                ret_dt = datetime.strptime(ret_short, "%Y-%m-%d")
                date_ret = f"{ret_dt.day:02d}.{ret_dt.month:02d}"
                search_query += f"-{date_ret}"
        except Exception:
            pass

    lines = [
        f"   🔍 Поиск: <code>{search_query}</code>",
        f"   💰 Цена: {price}",
        f"   ✈️ {airline}",
        f"   📅 {depart_full}",
    ]
    if time:
        lines[3] += f", {time}"
    lines.append(f"   🔄 {transfers}")
    if return_line:
        lines.append(return_line)
    lines.append(f"\n   🔗 <a href=\"{search_link}\">Найти и купить на Aviasales</a>")

    return "\n".join(lines)


def format_direction_inline_title(dest_code: str, info: dict) -> str:
    """Заголовок для направления (только город, без цены)."""
    dest = city_resolver.get_city_name(dest_code)
    return dest


def format_direction_inline_description(info: dict) -> str:
    """Описание для направления (цена + детали)."""
    price = format_price(info.get("price", 0))
    airline_code = info.get("airline", "")
    airline = city_resolver.get_airline_name(airline_code) if airline_code else ""
    transfers = format_transfers(info.get("transfers", 0))
    depart = format_date_short(info.get("departure_at", ""))

    parts = [f"💰 {price}"]
    if airline:
        parts.append(airline)
    parts.append(transfers)
    if depart:
        parts.append(depart)

    return " | ".join(parts)
