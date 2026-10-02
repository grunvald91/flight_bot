import logging
from datetime import datetime

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from database.crud import get_request_stats

logger = logging.getLogger(__name__)

ADMIN_ID = 437862772

router = Router()


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    args = message.text.split()
    days = 7
    if len(args) > 1:
        try:
            days = int(args[1])
        except ValueError:
            pass

    stats = await get_request_stats(days=days)

    lines = [f"<b>Статистика за {days} дн.</b>\n"]
    lines.append(f"Всего запросов: <b>{stats['total']}</b>")
    lines.append(f"Уникальных пользователей: <b>{stats['unique_users']}</b>\n")

    # По типам
    if stats["by_type"]:
        lines.append("<b>По типам:</b>")
        type_names = {"command": "Команды", "inline": "Инлайн", "callback": "Кнопки", "message": "Сообщения"}
        for t, cnt in stats["by_type"].items():
            name = type_names.get(t, t)
            lines.append(f"  {name}: {cnt}")
        lines.append("")

    # Популярные маршруты
    if stats["popular_routes"]:
        lines.append("<b>Популярные запросы:</b>")
        for r in stats["popular_routes"][:10]:
            lines.append(f"  <code>{r['query']}</code> — {r['count']} раз")
        lines.append("")

    # Топ пользователей
    if stats["top_users"]:
        lines.append("<b>Топ пользователей:</b>")
        for u in stats["top_users"][:10]:
            name = u["first_name"] or u["username"] or str(u["telegram_id"])
            uname = f" @{u['username']}" if u["username"] else ""
            lines.append(f"  {name}{uname} — {u['count']} запр. (id: {u['telegram_id']})")
        lines.append("")

    # Последние запросы
    if stats["recent"]:
        lines.append("<b>Последние запросы:</b>")
        for r in stats["recent"][:20]:
            time_str = r.created_at.strftime("%d.%m %H:%M") if r.created_at else "?"
            name = r.first_name or r.username or str(r.telegram_id)
            data = r.request_data or ""
            if len(data) > 40:
                data = data[:40] + "…"
            lines.append(f"  {time_str} | {name} | {r.request_type} | <code>{data}</code>")

    text = "\n".join(lines)
    # Telegram ограничение — 4096 символов
    if len(text) > 4000:
        text = text[:4000] + "\n\n<i>...обрезано</i>"

    await message.answer(text, parse_mode="HTML")
