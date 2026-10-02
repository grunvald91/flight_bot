import calendar
from datetime import date

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

MONTHS_RU = (
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)
WEEKDAYS_RU = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    absolute = year * 12 + month - 1 + delta
    return absolute // 12, absolute % 12 + 1


def calendar_keyboard(
    purpose: str,
    year: int | None = None,
    month: int | None = None,
    *,
    include_no_date: bool = False,
) -> InlineKeyboardMarkup:
    today = date.today()
    year = year or today.year
    month = month or today.month
    first_visible = date(year, month, 1)
    current_month = date(today.year, today.month, 1)

    previous_year, previous_month = _shift_month(year, month, -1)
    next_year, next_month = _shift_month(year, month, 1)
    previous_data = (
        f"calnav:{purpose}:{previous_year:04d}-{previous_month:02d}"
        if first_visible > current_month
        else "calnoop"
    )

    rows: list[list[InlineKeyboardButton]] = [[
        InlineKeyboardButton(text="◀️", callback_data=previous_data),
        InlineKeyboardButton(text=f"{MONTHS_RU[month]} {year}", callback_data="calnoop"),
        InlineKeyboardButton(
            text="▶️",
            callback_data=f"calnav:{purpose}:{next_year:04d}-{next_month:02d}",
        ),
    ]]
    rows.append([
        InlineKeyboardButton(text=weekday, callback_data="calnoop")
        for weekday in WEEKDAYS_RU
    ])

    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
        row = []
        for day in week:
            if day.month != month or day < today:
                row.append(InlineKeyboardButton(text="·", callback_data="calnoop"))
            else:
                row.append(InlineKeyboardButton(
                    text=str(day.day),
                    callback_data=f"caldate:{purpose}:{day.isoformat()}",
                ))
        rows.append(row)

    if include_no_date:
        rows.append([
            InlineKeyboardButton(text="Без даты — самые дешёвые", callback_data="search:nodate")
        ])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data=f"calcancel:{purpose}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
