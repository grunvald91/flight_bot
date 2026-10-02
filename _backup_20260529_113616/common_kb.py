from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def ticket_keyboard(
    search_link: str,
    origin: str,
    destination: str,
    search_query: str = "",
    depart_date: str | None = None,
    return_date: str | None = None,
) -> InlineKeyboardMarkup:
    """Клавиатура под карточкой билета."""
    query = search_query or f"{origin}-{destination}"
    dd = depart_date or ""
    rd = return_date or ""
    sub_cb = f"sub:{origin}:{destination}:{dd}:{rd}"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Купить на Aviasales", url=search_link),
            ],
            [
                InlineKeyboardButton(
                    text="В избранное",
                    callback_data=f"fav:{origin}:{destination}",
                ),
                InlineKeyboardButton(
                    text="Следить за ценой",
                    callback_data=sub_cb,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Повторить поиск",
                    switch_inline_query_current_chat=query,
                ),
            ],
        ]
    )


def favorites_keyboard(origin: str, destination: str) -> InlineKeyboardMarkup:
    """Клавиатура для элемента избранного."""
    sub_cb = f"sub:{origin}:{destination}::"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Искать билеты",
                    switch_inline_query_current_chat=f"{origin}-{destination}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Следить за ценой",
                    callback_data=sub_cb,
                ),
                InlineKeyboardButton(
                    text="Удалить",
                    callback_data=f"delfav:{origin}:{destination}",
                ),
            ],
        ]
    )


def connections_shortcut_keyboard(query: str) -> InlineKeyboardMarkup:
    """Клавиатура для открытия отдельного поиска пересадочных рейсов."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Показать с пересадками",
                    switch_inline_query_current_chat=query,
                ),
            ],
        ]
    )


def subscription_keyboard(
    sub_id: int,
    origin: str = "",
    destination: str = "",
    is_paused: bool = False,
) -> InlineKeyboardMarkup:
    """Клавиатура для карточки подписки."""
    buttons = []
    if origin and destination:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="Искать билеты",
                    switch_inline_query_current_chat=f"{origin}-{destination}",
                ),
            ]
        )

    pause_text = "▶️ Возобновить" if is_paused else "⏸ Пауза"
    pause_data = f"sub:resume:{sub_id}" if is_paused else f"sub:pause:{sub_id}"
    buttons.append(
        [
            InlineKeyboardButton(
                text=pause_text,
                callback_data=pause_data,
            ),
            InlineKeyboardButton(
                text="⚙️ Настройки",
                callback_data="menu:settings",
            ),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text="🗑 Удалить",
                callback_data=f"unsub:{sub_id}",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def history_entry_keyboard(
    origin: str,
    destination: str | None,
    depart_date: str | None = None,
) -> InlineKeyboardMarkup:
    """Клавиатура для записи истории поиска."""
    if destination:
        query = f"{origin}-{destination}"
        if depart_date:
            query += f" {depart_date}"
    else:
        query = origin

    buttons = [
        [
            InlineKeyboardButton(
                text="Повторить поиск",
                switch_inline_query_current_chat=query,
            )
        ],
    ]
    if destination:
        sub_cb = f"sub:{origin}:{destination}::"
        buttons.append(
            [
                InlineKeyboardButton(
                    text="В избранное",
                    callback_data=f"fav:{origin}:{destination}",
                ),
                InlineKeyboardButton(
                    text="Следить за ценой",
                    callback_data=sub_cb,
                ),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Главное меню бота."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Поиск билетов", switch_inline_query_current_chat=""),
            ],
            [
                InlineKeyboardButton(text="✨ Избранное", callback_data="menu:favorites"),
                InlineKeyboardButton(text="🔔 Подписки", callback_data="menu:subscriptions"),
            ],
            [
                InlineKeyboardButton(text="📜 История поиска", callback_data="menu:history"),
                InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu:settings"),
            ],
        ]
    )
