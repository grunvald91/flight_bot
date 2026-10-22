"""Запоминание переключателя «Без пересадок» для поисков с выдачей-списком.

Один выбор на пользователя, общий для «Дешёвые на месяц» и «Дешёвые за границу».
Сбой базы не должен ломать поиск: чтение при ошибке даёт «выключено», запись сообщает об ошибке.
"""
import logging

from database import crud

logger = logging.getLogger(__name__)


async def load_direct_only(user_id: int) -> bool:
    try:
        return await crud.get_search_direct_only(user_id)
    except Exception:
        logger.exception("Could not load the saved 'direct only' choice for %s", user_id)
        return False


async def save_direct_only(user, value: bool) -> bool:
    """`user` — aiogram User. True, если выбор сохранён."""
    try:
        await crud.set_search_direct_only(
            user.id, value,
            username=getattr(user, "username", None),
            first_name=getattr(user, "first_name", None),
        )
        return True
    except Exception:
        logger.exception("Could not save the 'direct only' choice for %s", getattr(user, "id", None))
        return False
