import logging
from datetime import datetime, date
from typing import NamedTuple

from sqlalchemy import select, delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.database import async_session_maker
from database.models import User, SearchHistory, Favorite, PriceSubscription, RequestLog, UserSettings

logger = logging.getLogger(__name__)


async def get_or_create_user(telegram_id: int, username: str | None = None, first_name: str | None = None) -> User:
    async with async_session_maker() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            user = User(telegram_id=telegram_id, username=username, first_name=first_name)
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user


async def add_favorite(telegram_id: int, origin: str, destination: str,
                       price: int | None = None, airline: str | None = None, link: str | None = None) -> bool:
    """Добавить маршрут в избранное. Возвращает True если добавлен, False если уже есть."""
    async with async_session_maker() as session:
        user = await _get_user(session, telegram_id)
        if not user:
            return False

        existing = await session.execute(
            select(Favorite).where(
                Favorite.user_id == user.id,
                Favorite.origin == origin,
                Favorite.destination == destination,
            )
        )
        if existing.scalar_one_or_none():
            return False

        session.add(Favorite(
            user_id=user.id,
            origin=origin,
            destination=destination,
            price=price,
            airline=airline,
            link=link,
        ))
        await session.commit()
        return True


async def remove_favorite(telegram_id: int, origin: str, destination: str) -> bool:
    async with async_session_maker() as session:
        user = await _get_user(session, telegram_id)
        if not user:
            return False

        result = await session.execute(
            delete(Favorite).where(
                Favorite.user_id == user.id,
                Favorite.origin == origin,
                Favorite.destination == destination,
            )
        )
        await session.commit()
        return result.rowcount > 0


async def get_favorites(telegram_id: int) -> list[Favorite]:
    async with async_session_maker() as session:
        user = await _get_user(session, telegram_id)
        if not user:
            return []

        result = await session.execute(
            select(Favorite).where(Favorite.user_id == user.id).order_by(Favorite.created_at.desc())
        )
        return list(result.scalars().all())


async def add_subscription(telegram_id: int, origin: str, destination: str,
                           max_price: int | None = None,
                           min_price_drop: int | None = None,
                           depart_date: date | None = None,
                           return_date: date | None = None) -> bool:
    """Подписаться на мониторинг цен. Возвращает True если создана, False если уже есть."""
    async with async_session_maker() as session:
        user = await _get_user(session, telegram_id)
        if not user:
            return False

        # Проверка дублей: учитываем depart_date (можно подписаться на разные даты)
        query = select(PriceSubscription).where(
            PriceSubscription.user_id == user.id,
            PriceSubscription.origin == origin,
            PriceSubscription.destination == destination,
            PriceSubscription.is_active == True,
        )
        if depart_date is not None:
            query = query.where(PriceSubscription.depart_date == depart_date)
        else:
            query = query.where(PriceSubscription.depart_date.is_(None))

        existing = await session.execute(query)
        if existing.scalar_one_or_none():
            return False

        session.add(PriceSubscription(
            user_id=user.id,
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            return_date=return_date,
            max_price=max_price,
            min_price_drop=min_price_drop,
            is_paused=False,
        ))
        await session.commit()
        return True


async def remove_subscription(sub_id: int, telegram_id: int) -> bool:
    async with async_session_maker() as session:
        user = await _get_user(session, telegram_id)
        if not user:
            return False

        result = await session.execute(
            update(PriceSubscription)
            .where(
                PriceSubscription.id == sub_id,
                PriceSubscription.user_id == user.id,
                PriceSubscription.is_active == True,
            )
            .values(is_active=False, is_paused=False)
        )
        await session.commit()
        return result.rowcount > 0


async def get_subscription(sub_id: int, telegram_id: int) -> PriceSubscription | None:
    async with async_session_maker() as session:
        user = await _get_user(session, telegram_id)
        if not user:
            return None

        result = await session.execute(
            select(PriceSubscription).where(
                PriceSubscription.id == sub_id,
                PriceSubscription.user_id == user.id,
                PriceSubscription.is_active == True,
            )
        )
        return result.scalar_one_or_none()


async def pause_subscription(sub_id: int, telegram_id: int, paused: bool) -> bool:
    async with async_session_maker() as session:
        user = await _get_user(session, telegram_id)
        if not user:
            return False

        result = await session.execute(
            update(PriceSubscription)
            .where(
                PriceSubscription.id == sub_id,
                PriceSubscription.user_id == user.id,
                PriceSubscription.is_active == True,
            )
            .values(is_paused=paused)
        )
        await session.commit()
        return result.rowcount > 0


async def set_subscription_notifications(sub_id: int, telegram_id: int, enabled: bool) -> bool:
    """Включает/выключает уведомления для одной подписки (слежение продолжается)."""
    async with async_session_maker() as session:
        user = await _get_user(session, telegram_id)
        if not user:
            return False

        result = await session.execute(
            update(PriceSubscription)
            .where(
                PriceSubscription.id == sub_id,
                PriceSubscription.user_id == user.id,
                PriceSubscription.is_active == True,
            )
            .values(notify_enabled=enabled)
        )
        await session.commit()
        return result.rowcount > 0


async def update_subscription_min_drop(sub_id: int, telegram_id: int, min_drop: int | None) -> bool:
    """Устанавливает или сбрасывает min_price_drop для подписки."""
    async with async_session_maker() as session:
        user = await _get_user(session, telegram_id)
        if not user:
            return False

        result = await session.execute(
            update(PriceSubscription)
            .where(
                PriceSubscription.id == sub_id,
                PriceSubscription.user_id == user.id,
                PriceSubscription.is_active == True,
            )
            .values(min_price_drop=min_drop)
        )
        await session.commit()
        return result.rowcount > 0


async def pause_all_subscriptions(telegram_id: int, paused: bool) -> int:
    async with async_session_maker() as session:
        user = await _get_user(session, telegram_id)
        if not user:
            return 0

        result = await session.execute(
            update(PriceSubscription)
            .where(
                PriceSubscription.user_id == user.id,
                PriceSubscription.is_active == True,
            )
            .values(is_paused=paused)
        )
        await session.commit()
        return int(result.rowcount or 0)


async def get_subscriptions(telegram_id: int) -> list[PriceSubscription]:
    async with async_session_maker() as session:
        user = await _get_user(session, telegram_id)
        if not user:
            return []

        result = await session.execute(
            select(PriceSubscription).where(
                PriceSubscription.user_id == user.id,
                PriceSubscription.is_active == True,
            ).order_by(PriceSubscription.created_at.desc())
        )
        return list(result.scalars().all())


async def get_all_active_subscriptions() -> list[tuple[PriceSubscription, int]]:
    """Все активные подписки с telegram_id пользователя. Для scheduler."""
    async with async_session_maker() as session:
        result = await session.execute(
            select(PriceSubscription, User.telegram_id)
            .join(User, PriceSubscription.user_id == User.id)
            .where(
                PriceSubscription.is_active == True,
                PriceSubscription.is_paused == False,
            )
        )
        return [(row[0], row[1]) for row in result.all()]


async def deactivate_subscription(sub_id: int):
    """Деактивировать подписку (например, дата прошла)."""
    async with async_session_maker() as session:
        await session.execute(
            update(PriceSubscription)
            .where(PriceSubscription.id == sub_id)
            .values(is_active=False, is_paused=False)
        )
        await session.commit()


async def update_subscription_price(sub_id: int, price: int):
    async with async_session_maker() as session:
        await session.execute(
            update(PriceSubscription)
            .where(PriceSubscription.id == sub_id)
            .values(last_price=price, last_checked_at=datetime.utcnow())
        )
        await session.commit()


async def get_search_history(telegram_id: int, limit: int = 10) -> list[SearchHistory]:
    async with async_session_maker() as session:
        user = await _get_user(session, telegram_id)
        if not user:
            return []

        result = await session.execute(
            select(SearchHistory)
            .where(SearchHistory.user_id == user.id)
            .order_by(SearchHistory.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def _get_user(session: AsyncSession, telegram_id: int) -> User | None:
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()


# ── Request logging ──

async def log_request(telegram_id: int, username: str | None, first_name: str | None,
                      request_type: str, request_data: str | None = None):
    """Логирует запрос пользователя."""
    try:
        async with async_session_maker() as session:
            session.add(RequestLog(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                request_type=request_type,
                request_data=request_data,
            ))
            await session.commit()
    except Exception as e:
        logger.error("Failed to log request; details suppressed")


async def get_request_stats(days: int = 7, limit: int = 50) -> dict:
    """Получает статистику запросов за N дней."""
    from sqlalchemy import func, desc
    since = datetime.utcnow() - __import__("datetime").timedelta(days=days)

    async with async_session_maker() as session:
        # Общее количество запросов
        total_q = await session.execute(
            select(func.count(RequestLog.id)).where(RequestLog.created_at >= since)
        )
        total = total_q.scalar() or 0

        # Уникальные пользователи
        unique_q = await session.execute(
            select(func.count(func.distinct(RequestLog.telegram_id))).where(RequestLog.created_at >= since)
        )
        unique_users = unique_q.scalar() or 0

        # По типам запросов
        types_q = await session.execute(
            select(RequestLog.request_type, func.count(RequestLog.id))
            .where(RequestLog.created_at >= since)
            .group_by(RequestLog.request_type)
            .order_by(desc(func.count(RequestLog.id)))
        )
        by_type = {row[0]: row[1] for row in types_q.all()}

        # Топ пользователей
        top_users_q = await session.execute(
            select(
                RequestLog.telegram_id,
                RequestLog.username,
                RequestLog.first_name,
                func.count(RequestLog.id).label("cnt"),
            )
            .where(RequestLog.created_at >= since)
            .group_by(RequestLog.telegram_id, RequestLog.username, RequestLog.first_name)
            .order_by(desc("cnt"))
            .limit(20)
        )
        top_users = [
            {"telegram_id": r[0], "username": r[1], "first_name": r[2], "count": r[3]}
            for r in top_users_q.all()
        ]

        # Последние запросы
        recent_q = await session.execute(
            select(RequestLog)
            .where(RequestLog.created_at >= since)
            .order_by(desc(RequestLog.created_at))
            .limit(limit)
        )
        recent = list(recent_q.scalars().all())

        # Популярные маршруты (из request_data инлайн-запросов)
        routes_q = await session.execute(
            select(RequestLog.request_data, func.count(RequestLog.id).label("cnt"))
            .where(
                RequestLog.created_at >= since,
                RequestLog.request_type == "inline",
                RequestLog.request_data.isnot(None),
                RequestLog.request_data != "",
            )
            .group_by(RequestLog.request_data)
            .order_by(desc("cnt"))
            .limit(15)
        )
        popular_routes = [{"query": r[0], "count": r[1]} for r in routes_q.all()]

        return {
            "total": total,
            "unique_users": unique_users,
            "by_type": by_type,
            "top_users": top_users,
            "recent": recent,
            "popular_routes": popular_routes,
        }


# ── Admin aggregates ──

async def get_user_count_stats(days: int | None) -> dict:
    """Возвращает {total, new_in_period, active_in_period}.

    Если days=None → "за всё время" (new = total, active = по логам всем).
    """
    from sqlalchemy import func
    import datetime as _dt

    async with async_session_maker() as session:
        total_q = await session.execute(select(func.count(User.id)))
        total = total_q.scalar() or 0

        if days is None:
            new_in_period = total
        else:
            since = _dt.datetime.utcnow() - _dt.timedelta(days=days)
            new_q = await session.execute(
                select(func.count(User.id)).where(User.created_at >= since)
            )
            new_in_period = new_q.scalar() or 0

        if days is None:
            active_q = await session.execute(
                select(func.count(func.distinct(RequestLog.telegram_id)))
            )
        else:
            since = _dt.datetime.utcnow() - _dt.timedelta(days=days)
            active_q = await session.execute(
                select(func.count(func.distinct(RequestLog.telegram_id)))
                .where(RequestLog.created_at >= since)
            )
        active_in_period = active_q.scalar() or 0

        return {
            "total": total,
            "new_in_period": new_in_period,
            "active_in_period": active_in_period,
        }


async def get_subscription_count_stats() -> dict:
    """Возвращает {total_active, paused, muted, with_max_price, with_date}."""
    from sqlalchemy import func, case

    async with async_session_maker() as session:
        result = await session.execute(
            select(
                func.count(PriceSubscription.id),
                func.sum(case((PriceSubscription.is_paused == True, 1), else_=0)),
                func.sum(case((PriceSubscription.notify_enabled == False, 1), else_=0)),
                func.sum(case((PriceSubscription.max_price.isnot(None), 1), else_=0)),
                func.sum(case((PriceSubscription.depart_date.isnot(None), 1), else_=0)),
            ).where(PriceSubscription.is_active == True)
        )
        row = result.first()
        return {
            "total_active": int(row[0] or 0),
            "paused": int(row[1] or 0),
            "muted": int(row[2] or 0),
            "with_max_price": int(row[3] or 0),
            "with_date": int(row[4] or 0),
        }


async def get_favorite_count_stats() -> dict:
    """Возвращает {total, unique_users, unique_routes}."""
    from sqlalchemy import func

    async with async_session_maker() as session:
        total_q = await session.execute(select(func.count(Favorite.id)))
        users_q = await session.execute(
            select(func.count(func.distinct(Favorite.user_id)))
        )
        routes_q = await session.execute(
            select(func.count(func.distinct(
                func.concat(Favorite.origin, "-", Favorite.destination)
            )))
        )
        return {
            "total": int(total_q.scalar() or 0),
            "unique_users": int(users_q.scalar() or 0),
            "unique_routes": int(routes_q.scalar() or 0),
        }


async def get_top_subscription_routes(limit: int = 10) -> list[dict]:
    """Самые популярные маршруты по числу активных подписок."""
    from sqlalchemy import func, desc

    async with async_session_maker() as session:
        result = await session.execute(
            select(
                PriceSubscription.origin,
                PriceSubscription.destination,
                func.count(PriceSubscription.id).label("cnt"),
            )
            .where(PriceSubscription.is_active == True)
            .group_by(PriceSubscription.origin, PriceSubscription.destination)
            .order_by(desc("cnt"))
            .limit(limit)
        )
        return [
            {"origin": r[0], "destination": r[1], "count": r[2]}
            for r in result.all()
        ]


async def get_top_favorite_routes(limit: int = 10) -> list[dict]:
    """Самые популярные маршруты по числу избранного."""
    from sqlalchemy import func, desc

    async with async_session_maker() as session:
        result = await session.execute(
            select(
                Favorite.origin,
                Favorite.destination,
                func.count(Favorite.id).label("cnt"),
            )
            .group_by(Favorite.origin, Favorite.destination)
            .order_by(desc("cnt"))
            .limit(limit)
        )
        return [
            {"origin": r[0], "destination": r[1], "count": r[2]}
            for r in result.all()
        ]


async def get_users_paginated(offset: int = 0, limit: int = 10) -> tuple[list[dict], int]:
    """Возвращает (users, total). users отсортированы по last_activity DESC, потом по created_at DESC."""
    from sqlalchemy import func, desc

    async with async_session_maker() as session:
        total_q = await session.execute(select(func.count(User.id)))
        total = int(total_q.scalar() or 0)

        sub_count_sq = (
            select(
                PriceSubscription.user_id,
                func.count(PriceSubscription.id).label("sub_cnt"),
            )
            .where(PriceSubscription.is_active == True)
            .group_by(PriceSubscription.user_id)
            .subquery()
        )
        fav_count_sq = (
            select(
                Favorite.user_id,
                func.count(Favorite.id).label("fav_cnt"),
            )
            .group_by(Favorite.user_id)
            .subquery()
        )
        last_req_sq = (
            select(
                RequestLog.telegram_id,
                func.max(RequestLog.created_at).label("last_at"),
                func.count(RequestLog.id).label("req_cnt"),
            )
            .group_by(RequestLog.telegram_id)
            .subquery()
        )

        result = await session.execute(
            select(
                User.id,
                User.telegram_id,
                User.username,
                User.first_name,
                User.created_at,
                sub_count_sq.c.sub_cnt,
                fav_count_sq.c.fav_cnt,
                last_req_sq.c.last_at,
                last_req_sq.c.req_cnt,
            )
            .outerjoin(sub_count_sq, sub_count_sq.c.user_id == User.id)
            .outerjoin(fav_count_sq, fav_count_sq.c.user_id == User.id)
            .outerjoin(last_req_sq, last_req_sq.c.telegram_id == User.telegram_id)
            .order_by(desc(last_req_sq.c.last_at), desc(User.created_at))
            .offset(offset)
            .limit(limit)
        )

        users = [
            {
                "id": r[0],
                "telegram_id": r[1],
                "username": r[2],
                "first_name": r[3],
                "created_at": r[4],
                "sub_count": int(r[5] or 0),
                "fav_count": int(r[6] or 0),
                "last_activity": r[7],
                "req_count": int(r[8] or 0),
            }
            for r in result.all()
        ]
        return users, total


async def get_user_detail(telegram_id: int) -> dict | None:
    """Полная карточка пользователя: профиль, подписки, избранное, последние запросы."""
    from sqlalchemy import func, desc

    async with async_session_maker() as session:
        user = await _get_user(session, telegram_id)
        if not user:
            return None

        subs_q = await session.execute(
            select(PriceSubscription)
            .where(
                PriceSubscription.user_id == user.id,
                PriceSubscription.is_active == True,
            )
            .order_by(desc(PriceSubscription.created_at))
            .limit(20)
        )
        subscriptions = list(subs_q.scalars().all())

        favs_q = await session.execute(
            select(Favorite)
            .where(Favorite.user_id == user.id)
            .order_by(desc(Favorite.created_at))
            .limit(20)
        )
        favorites = list(favs_q.scalars().all())

        recent_q = await session.execute(
            select(RequestLog)
            .where(RequestLog.telegram_id == telegram_id)
            .order_by(desc(RequestLog.created_at))
            .limit(15)
        )
        recent = list(recent_q.scalars().all())

        count_q = await session.execute(
            select(func.count(RequestLog.id), func.max(RequestLog.created_at))
            .where(RequestLog.telegram_id == telegram_id)
        )
        cnt_row = count_q.first()
        total_requests = int(cnt_row[0] or 0)
        last_activity = cnt_row[1]

        return {
            "user": user,
            "subscriptions": subscriptions,
            "favorites": favorites,
            "recent_requests": recent,
            "total_requests": total_requests,
            "last_activity": last_activity,
        }


# ── User Settings ──

async def get_or_create_user_settings(telegram_id: int) -> UserSettings:
    """Получает или создает настройки пользователя."""
    async with async_session_maker() as session:
        user = await _get_user(session, telegram_id)
        if not user:
            raise ValueError(f"User with telegram_id {telegram_id} not found")

        result = await session.execute(
            select(UserSettings).where(UserSettings.user_id == user.id)
        )
        settings = result.scalar_one_or_none()

        if not settings:
            settings = UserSettings(user_id=user.id)
            session.add(settings)
            await session.commit()
            await session.refresh(settings)

        return settings


async def get_user_settings(telegram_id: int) -> UserSettings | None:
    """Получает настройки пользователя."""
    async with async_session_maker() as session:
        user = await _get_user(session, telegram_id)
        if not user:
            return None

        result = await session.execute(
            select(UserSettings).where(UserSettings.user_id == user.id)
        )
        return result.scalar_one_or_none()


class SearchFilters(NamedTuple):
    direct_only: bool      # «Без пересадок»
    cheapest_month: bool   # «Самые дешёвые на месяц» для поиска без даты


async def get_search_filters(telegram_id: int) -> SearchFilters:
    """Фильтры поиска пользователя. Без сохранённых настроек: без пересадок выкл, дешёвые на месяц вкл."""
    settings = await get_user_settings(telegram_id)
    return SearchFilters(
        direct_only=bool(settings and settings.direct_flights_only),
        cheapest_month=not settings or settings.cheapest_month_search is not False,
    )


async def update_user_settings(telegram_id: int, **kwargs) -> bool:
    """Обновляет настройки пользователя."""
    async with async_session_maker() as session:
        user = await _get_user(session, telegram_id)
        if not user:
            return False

        result = await session.execute(
            update(UserSettings)
            .where(UserSettings.user_id == user.id)
            .values(**kwargs, updated_at=datetime.utcnow())
        )

        if result.rowcount == 0:
            # Настройки не существуют, создаем их
            settings = UserSettings(user_id=user.id, **kwargs)
            session.add(settings)

        await session.commit()
        return True
