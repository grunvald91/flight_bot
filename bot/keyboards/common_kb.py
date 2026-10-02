from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


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
    notify_enabled: bool = True,
) -> InlineKeyboardMarkup:
    """Карточка подписки: уведомления / пауза / мин.снижение / удаление."""
    notif_text = "🔔 Уведомления: вкл" if notify_enabled else "🔕 Уведомления: выкл"
    pause_text = "▶️ Возобновить слежение" if is_paused else "⏸ Пауза слежения"

    buttons: list[list[InlineKeyboardButton]] = []

    if origin and destination:
        buttons.append([
            InlineKeyboardButton(
                text="🔍 Искать билеты",
                switch_inline_query_current_chat=f"{origin}-{destination}",
            ),
        ])

    buttons.extend([
        [InlineKeyboardButton(text=notif_text, callback_data=f"subact:notif:{sub_id}")],
        [InlineKeyboardButton(text=pause_text, callback_data=f"subact:pause:{sub_id}")],
        [InlineKeyboardButton(text="✏️ Мин. снижение цены", callback_data=f"subact:mindrop:{sub_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"subact:delask:{sub_id}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="sublist:open")],
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def subscriptions_list_keyboard(
    items: list[tuple[int, str, bool, bool]],
    page: int,
    page_size: int,
    total: int,
) -> InlineKeyboardMarkup:
    """Список подписок одной клавиатурой: по кнопке-чипу на каждую.

    items: (sub_id, label, is_paused, notify_enabled)
    """
    rows: list[list[InlineKeyboardButton]] = []

    for sub_id, label, is_paused, notify_enabled in items:
        status_icon = "⏸" if is_paused else ("🔔" if notify_enabled else "🔕")
        rows.append([
            InlineKeyboardButton(
                text=f"{status_icon} {label}",
                callback_data=f"subcard:{sub_id}",
            )
        ])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"sublist:p:{page - 1}"))
    if (page + 1) * page_size < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"sublist:p:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="⚙️ Общие настройки", callback_data="submgr:open")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:back")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_delete_confirm_keyboard(sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Да, удалить", callback_data=f"subact:delyes:{sub_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"subcard:{sub_id}")],
    ])


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


def main_menu_keyboard(user_id: int | None = None) -> InlineKeyboardMarkup:
    """Главное меню бота. Для админов добавляет кнопку '🛠 Админка'."""
    rows = [
        [InlineKeyboardButton(text="🔍 Найти билеты", switch_inline_query_current_chat="")],
        [InlineKeyboardButton(text="🟣 Поиск на Туту", callback_data="tutu:start")],
        [InlineKeyboardButton(text="⚡ Быстрый обзор направлений", callback_data="menu:browse")],
        [InlineKeyboardButton(text="🌍 Куда дешевле за границу", callback_data="menu:discover")],
        [InlineKeyboardButton(text="👤 Профиль пассажира", callback_data="profile:open")],
        [InlineKeyboardButton(text="⭐ Избранные маршруты", callback_data="menu:favorites")],
        [InlineKeyboardButton(text="🔔 Слежение за ценой", callback_data="menu:subscriptions")],
        [
            InlineKeyboardButton(text="📜 История", callback_data="menu:history"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu:settings"),
        ],
    ]

    if user_id is not None:
        try:
            from bot.handlers.admin import ADMIN_IDS  # local import to avoid cycle
            if user_id in ADMIN_IDS:
                rows.append([InlineKeyboardButton(text="🛠 Админка", callback_data="adm:root")])
        except Exception:
            pass

    return InlineKeyboardMarkup(inline_keyboard=rows)


def persistent_menu_keyboard() -> ReplyKeyboardMarkup:
    """Compact primary actions; the full set remains in the inline main menu."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Найти билеты"), KeyboardButton(text="🟣 Поиск на Туту")],
            [KeyboardButton(text="⚡ Быстрый"), KeyboardButton(text="🌍 Глубокий")],
            [KeyboardButton(text="🏠 Меню")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите функцию",
    )
