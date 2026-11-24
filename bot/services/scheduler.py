import logging
import asyncio
from datetime import datetime, timedelta, date
import math
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.config import DAILY_CHANNEL_HOUR, DAILY_CHANNEL_MINUTE, MIN_PRICE_DROP, TIMEZONE
from bot.services.aviasales_api import aviasales_api
from bot.services.notifications import notify_price_drop
from bot.services.city_resolver import city_resolver
from bot.services.channel_publisher import publish_tomorrow_channels
from database.crud import get_all_active_subscriptions, update_subscription_price, deactivate_subscription, get_user_settings, get_subscription

logger = logging.getLogger(__name__)
_price_check_lock = asyncio.Lock()


async def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    scheduler.add_job(
        check_price_subscriptions,
        "interval",
        hours=1,
        args=[bot],
        id="check_prices",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=900,
        next_run_time=datetime.now(ZoneInfo(TIMEZONE)) + timedelta(seconds=30),
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

    # A failed daily run is not retried by APScheduler. Recheck unposted channels
    # throughout the remaining day and immediately after service startup.
    scheduler.add_job(
        retry_missed_channel_publications,
        "interval",
        minutes=15,
        args=[bot],
        id="retry_missed_daily_channels",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=900,
    )
    scheduler.add_job(
        retry_missed_channel_publications,
        "date",
        run_date=datetime.now(ZoneInfo(TIMEZONE)) + timedelta(seconds=10),
        args=[bot],
        id="recover_daily_channels_on_startup",
        replace_existing=True,
        misfire_grace_time=900,
    )

    return scheduler


def _channel_publication_due(now: datetime) -> bool:
    """Never publish tomorrow's fares before the configured local posting time."""
    return (now.hour, now.minute) >= (DAILY_CHANNEL_HOUR, DAILY_CHANNEL_MINUTE)


async def retry_missed_channel_publications(bot: Bot) -> dict[str, int]:
    now = datetime.now(ZoneInfo(TIMEZONE))
    if not _channel_publication_due(now):
        return {}
    # Existing publication state provides per-channel, per-date idempotency.
    return await publish_tomorrow_channels(bot)


async def _subscription_tickets(sub, today: date, direct: bool) -> list[dict]:
    end = today + timedelta(days=30)
    dates = ([sub.depart_date.isoformat()] if sub.depart_date else
             sorted({(today + timedelta(days=i)).strftime("%Y-%m") for i in range(30)}))
    return_day = sub.return_date.isoformat() if sub.return_date else None
    result = []
    for day in dates:
        tickets = await aviasales_api.search_prices_for_dates(
            origin=sub.origin, destination=sub.destination, depart_date=day,
            return_date=return_day, one_way=not bool(return_day), direct=direct, limit=1000, strict=True,
        )
        for ticket in tickets:
            if not isinstance(ticket, dict):
                continue
            dep = str(ticket.get("departure_at") or ticket.get("depart_date") or "")[:10]
            ret = str(ticket.get("return_at") or ticket.get("return_date") or "")[:10]
            if sub.depart_date:
                if dep != sub.depart_date.isoformat():
                    continue
            elif not today.isoformat() <= dep < end.isoformat():
                continue
            if return_day and ret != return_day:
                continue
            if not return_day and ret:
                continue
            if direct and (ticket.get("transfers", ticket.get("number_of_changes")) != 0
                           or (return_day and ticket.get("return_transfers") != 0)):
                continue
            try:
                raw = ticket.get("price", ticket.get("value"))
                if isinstance(raw, bool):
                    continue
                price = float(raw)
                if math.isfinite(price) and price > 0:
                    result.append({**ticket, "price": math.ceil(price), "departure_at": dep})
            except (TypeError, ValueError, OverflowError):
                continue
    return result


def _should_notify(sub, current_price: int, threshold: int) -> bool:
    previous = sub.last_price
    drop = previous is not None and previous - current_price >= threshold
    crossed = bool(sub.max_price and current_price <= sub.max_price
                   and (previous is None or previous > sub.max_price))
    return bool(drop or crossed)


async def check_subscription(bot: Bot, sub_id: int, telegram_id: int, *, manual=False) -> str:
    # Serialize checks so manual and scheduled jobs never notify from the same baseline.
    if manual and _price_check_lock.locked():
        return "busy"
    async with _price_check_lock:
        sub = await get_subscription(sub_id, telegram_id)
        if not sub:
            return "missing"
        if sub.is_paused:
            return "paused"
        today = datetime.now(ZoneInfo(TIMEZONE)).date()
        if sub.depart_date and sub.depart_date < today:
            await deactivate_subscription(sub.id)
            return "expired"
        if manual and sub.last_checked_at and datetime.utcnow() - sub.last_checked_at < timedelta(minutes=1):
            return "recent"
        settings = await get_user_settings(telegram_id)
        direct = bool(settings and settings.direct_flights_only)
        tickets = await _subscription_tickets(sub, today, direct)
        if not tickets:
            return "no_data"
        cheapest = min(tickets, key=lambda t: t["price"])
        current_price = cheapest["price"]
        # Reload after the external request, in case the owner muted or deleted it.
        sub = await get_subscription(sub_id, telegram_id)
        if not sub or sub.is_paused:
            return "missing" if not sub else "paused"
        settings = await get_user_settings(telegram_id)
        if bool(settings and settings.direct_flights_only) != direct:
            return "settings_changed"
        threshold = sub.min_price_drop or getattr(settings, "min_price_drop", None) or MIN_PRICE_DROP
        notifications_enabled = (getattr(settings, "notifications_enabled", True) is not False
                                 and getattr(sub, "notify_enabled", True) is not False)
        should_notify = _should_notify(sub, current_price, threshold)
        if should_notify and notifications_enabled:
            if not await notify_price_drop(bot, telegram_id, sub, current_price, cheapest):
                return "delivery_failed"
        await update_subscription_price(sub.id, current_price)
        return "ok"


async def check_price_subscriptions(bot: Bot):
    logger.info("Checking price subscriptions...")
    subs = await get_all_active_subscriptions()
    for sub, telegram_id in subs:
        try:
            status = await check_subscription(bot, sub.id, telegram_id)
            if status == "delivery_failed":
                logger.warning("Delivery failed for subscription %d", sub.id)
        except Exception as exc:
            logger.error("Error checking subscription %d: %s", sub.id, exc)
    logger.info("Price check complete: %d subscriptions", len(subs))


async def cleanup_cache():
    """Удаляет просроченные записи кэша."""
    await aviasales_api.cleanup_expired_cache()


async def update_reference_data():
    """Обновляет справочники городов и авиакомпаний."""
    await city_resolver.update()
