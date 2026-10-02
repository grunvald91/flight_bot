import logging
from datetime import datetime, timedelta, date

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.config import DAILY_CHANNEL_HOUR, DAILY_CHANNEL_MINUTE, MIN_PRICE_DROP, TIMEZONE
from bot.services.aviasales_api import aviasales_api
from bot.services.notifications import notify_price_drop
from bot.services.city_resolver import city_resolver
from bot.services.channel_publisher import publish_tomorrow_channels
from database.crud import get_all_active_subscriptions, update_subscription_price, deactivate_subscription, get_user_settings

logger = logging.getLogger(__name__)


async def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    scheduler.add_job(
        check_price_subscriptions,
        "interval",
        hours=1,
        args=[bot],
        id="check_prices",
        replace_existing=True,
    )

    scheduler.add_job(
        cleanup_cache,
        "interval",
        hours=6,
        id="cleanup_cache",
        replace_existing=True,
    )

    scheduler.add_job(
        update_reference_data,
        "interval",
        hours=24,
        id="update_refs",
        replace_existing=True,
    )

    scheduler.add_job(
        publish_tomorrow_channels,
        "cron",
        hour=DAILY_CHANNEL_HOUR,
        minute=DAILY_CHANNEL_MINUTE,
        args=[bot],
        id="daily_cheapest_channels",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    return scheduler


async def check_price_subscriptions(bot: Bot):
    """Проверяет цены для всех активных подписок."""
    logger.info("Checking price subscriptions...")

    subs = await get_all_active_subscriptions()
    if not subs:
        return

    today = date.today()

    for sub, telegram_id in subs:
        try:
            # Автоматически деактивируем подписки с прошедшей датой
            if sub.depart_date and sub.depart_date + timedelta(days=1) < today:
                logger.info("Deactivating expired subscription %d (depart_date=%s)", sub.id, sub.depart_date)
                await deactivate_subscription(sub.id)
                continue

            # Определяем даты для запроса
            all_tickets = []

            if sub.depart_date:
                # Конкретная дата: запрашиваем ±1 день (3 дня)
                for delta in [-1, 0, 1]:
                    search_date = sub.depart_date + timedelta(days=delta)
                    if search_date < today:
                        continue
                    date_str = search_date.strftime("%Y-%m-%d")
                    tickets = await aviasales_api.search_prices_for_dates(
                        origin=sub.origin,
                        destination=sub.destination,
                        depart_date=date_str,
                    )
                    all_tickets.extend(tickets)
                    logger.debug("Sub %d: date %s -> %d tickets", sub.id, date_str, len(tickets))
            else:
                # Без даты: ближайшие 30 дней
                end_date = today + timedelta(days=30)
                end_str = end_date.strftime("%Y-%m-%d")
                today_str = today.strftime("%Y-%m-%d")
                tickets = await aviasales_api.search_prices_for_dates(
                    origin=sub.origin,
                    destination=sub.destination,
                    depart_date=today_str,
                )
                # Фильтруем только ближайшие 30 дней
                for t in tickets:
                    dep_str = (t.get("departure_at") or t.get("depart_date", ""))[:10]
                    if dep_str and dep_str <= end_str:
                        all_tickets.append(t)
                    elif not dep_str:
                        all_tickets.append(t)
                logger.debug("Sub %d: no date, 30-day window -> %d tickets", sub.id, len(all_tickets))

            if not all_tickets:
                continue

            # Получаем настройки пользователя для фильтрации
            user_settings = await get_user_settings(telegram_id)

            global_notifications = not (user_settings and user_settings.notifications_enabled is False)
            sub_notifications = getattr(sub, "notify_enabled", True) is not False
            notifications_enabled = global_notifications and sub_notifications

            # Фильтрация по прямым рейсам если включена настройка
            filtered_tickets = all_tickets
            if user_settings and user_settings.direct_flights_only:
                filtered_tickets = [t for t in all_tickets if t.get("transfers", 0) == 0]
                logger.debug("Sub %d: filtered to direct flights only -> %d tickets", sub.id, len(filtered_tickets))

                if not filtered_tickets:
                    logger.debug("Sub %d: no direct flights found, skipping notification", sub.id)
                    continue

            cheapest = min(filtered_tickets, key=lambda t: t.get("price", float("inf")))
            current_price = cheapest.get("price", 0)

            if current_price <= 0:
                continue

            should_notify = False

            if sub.last_price and current_price < sub.last_price:
                drop = sub.last_price - current_price
                threshold = sub.min_price_drop or MIN_PRICE_DROP
                if drop >= threshold:
                    should_notify = True

            if sub.max_price and current_price <= sub.max_price:
                should_notify = True

            if should_notify and notifications_enabled:
                await notify_price_drop(bot, telegram_id, sub, current_price, cheapest)
            elif should_notify:
                logger.debug("Sub %d: signal found but notifications are disabled", sub.id)

            await update_subscription_price(sub.id, current_price)

        except Exception as e:
            logger.error("Error checking subscription %d: %s", sub.id, e)

    logger.info("Price check complete: %d subscriptions", len(subs))


async def cleanup_cache():
    """Удаляет просроченные записи кэша."""
    await aviasales_api.cleanup_expired_cache()


async def update_reference_data():
    """Обновляет справочники городов и авиакомпаний."""
    await city_resolver.update()
