"""Bounded, persisted international fare watches. Prices are indicative Aviasales data."""
import asyncio
import html
import logging
import math
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, delete, update
from sqlalchemy.exc import IntegrityError

from bot.config import TIMEZONE, MIN_PRICE_DROP
from bot.services.aviasales_api import aviasales_api
from bot.services.city_resolver import city_resolver
from bot.utils.formatters import format_price
from database.crud import get_user_settings
from database.database import async_session_maker
from database.models import SmartTravelAlert

logger = logging.getLogger(__name__)
NIGHTS = (0, 3, 5, 7, 10, 14)
HORIZONS = (7, 14, 30)
MAX_RULES = 3
MAX_GROUPS = 10  # Pilot quota: at most 300 date searches per hourly run, before pagination.
_gate = asyncio.Lock()
_limit = asyncio.Semaphore(3)


async def list_rules(telegram_id):
    async with async_session_maker() as session:
        return list((await session.scalars(select(SmartTravelAlert)
            .where(SmartTravelAlert.telegram_id == telegram_id)
            .order_by(SmartTravelAlert.id))).all())


async def get_rule(telegram_id, rule_id):
    async with async_session_maker() as session:
        return await session.scalar(select(SmartTravelAlert).where(
            SmartTravelAlert.telegram_id == telegram_id, SmartTravelAlert.id == rule_id))


def group_key(rule):
    return rule.origin, rule.nights, rule.horizon, rule.direct


async def create_rule(telegram_id, origin, nights, horizon, budget, direct):
    if (not re.fullmatch(r"[A-Z]{3}", origin) or not city_resolver.get_country_code(origin)
        or type(nights) is not int or nights not in NIGHTS or type(horizon) is not int or horizon not in HORIZONS
        or type(budget) is not int or not 100 <= budget <= 10_000_000 or type(direct) is not bool):
        return "invalid"
    async with _gate, async_session_maker() as session:
        rows = list((await session.scalars(select(SmartTravelAlert))).all())
        own = [r for r in rows if r.telegram_id == telegram_id]
        if any(group_key(r) == (origin, nights, horizon, direct) and r.budget == budget for r in own):
            return "duplicate"
        if len(own) >= MAX_RULES:
            return "user_limit"
        # Count paused rules as well: resuming must never exceed the pilot quota.
        groups = {group_key(r) for r in rows}
        if (origin, nights, horizon, direct) not in groups and len(groups) >= MAX_GROUPS:
            return "capacity"
        session.add(SmartTravelAlert(telegram_id=telegram_id, origin=origin, nights=nights,
                                    horizon=horizon, budget=budget, direct=direct))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return "duplicate"
        return "ok"


async def change_rule(telegram_id, rule_id, *, paused=None, remove=False):
    async with async_session_maker() as session:
        condition = (SmartTravelAlert.telegram_id == telegram_id, SmartTravelAlert.id == rule_id)
        stmt = (delete(SmartTravelAlert).where(*condition) if remove else
                update(SmartTravelAlert).where(*condition).values(paused=paused))
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount > 0


def choose_fares(rows, country, departure, returning, country_for, direct):
    """Only exact dates, foreign destinations and positive finite RUB prices."""
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        dest = row.get("destination")
        if not isinstance(dest, str) or not re.fullmatch(r"[A-Z]{3}", dest):
            continue
        foreign = country_for(dest)
        if not foreign or foreign == country:
            continue
        dep = str(row.get("departure_at") or row.get("depart_date") or "")[:10]
        ret = str(row.get("return_at") or row.get("return_date") or "")[:10]
        if dep != departure or ret != (returning or ""):
            continue
        if direct and (row.get("transfers", row.get("number_of_changes")) != 0
                       or (returning and row.get("return_transfers") != 0)):
            continue
        try:
            raw = row.get("price", row.get("value"))
            if isinstance(raw, bool):
                continue
            price = float(raw)
            if not math.isfinite(price) or price <= 0:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        result.append({"destination": dest, "departure": departure, "returning": returning,
                       "price": math.ceil(price), "country": foreign})
    return result


async def scan_group(key, today):
    origin, nights, horizon, direct = key
    country = city_resolver.get_country_code(origin)
    if not country:
        raise ValueError("Unknown origin country")
    async def scan_day(offset):
        departure = (today + timedelta(days=offset)).isoformat()
        returning = (today + timedelta(days=offset+nights)).isoformat() if nights else None
        try:
            async with _limit:
                rows = await aviasales_api.search_destinations_on_date(
                    origin, departure, direct=direct, return_date=returning)
            return choose_fares(rows, country, departure, returning, city_resolver.get_country_code, direct)
        except Exception:
            logger.warning("Smart alert source failed for %s %s", origin, departure)
            return None
    days = await asyncio.gather(*(scan_day(i) for i in range(horizon)))
    fares = sorted((f for day in days if day is not None for f in day),
                   key=lambda f: (f["price"], f["departure"], f["destination"]))
    return fares, sum(day is None for day in days)


def offer_key(offer):
    return f"{offer['departure']}|{offer['destination']}|{offer['returning'] or ''}"


def next_signal(offer, sent, threshold):
    previous = sent.get(offer_key(offer))
    return previous is None or previous - offer["price"] >= threshold


async def _record(rule, **values):
    async with async_session_maker() as session:
        await session.execute(update(SmartTravelAlert).where(SmartTravelAlert.id == rule.id,
            SmartTravelAlert.telegram_id == rule.telegram_id, SmartTravelAlert.paused == False).values(**values))
        await session.commit()


async def _send(bot, rule, offer, failed):
    city = html.escape(city_resolver.get_city_name(offer["destination"]))
    origin = html.escape(city_resolver.get_city_name(rule.origin))
    link = aviasales_api.build_search_link(rule.origin, offer["destination"], offer["departure"], offer["returning"])
    dates = offer["departure"] + (" — " + offer["returning"] if offer["returning"] else "")
    text = (f"🔔 <b>Найдена выгодная поездка</b>\n{origin} → {city}\n"
            f"{dates}\n<b>{format_price(offer['price'])}</b> · бюджет {format_price(rule.budget)}\n"
            + ("Некоторые даты недоступны для проверки.\n" if failed else "")
            + "Ориентировочная цена из кэша Aviasales. Проверьте наличие перед покупкой.")
    await bot.send_message(rule.telegram_id, text, parse_mode="HTML", disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Проверить билеты", url=link)],
            [InlineKeyboardButton(text="Настроить мониторинг", callback_data=f"smart:card:{rule.id}")],
        ]))


async def check_smart_alerts(bot):
    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    async with async_session_maker() as session:
        rules = list((await session.scalars(select(SmartTravelAlert).where(
            SmartTravelAlert.paused == False).order_by(SmartTravelAlert.id))).all())
    grouped = {}
    for rule in rules:
        grouped.setdefault(group_key(rule), []).append(rule)
    for key, members in grouped.items():
        try:
            fares, failed = await scan_group(key, today)
        except Exception:
            logger.exception("Smart alert scan failed")
            fares, failed = [], key[2]
        for member in members:
            try:
                rule = await get_rule(member.telegram_id, member.id)
                if not rule or rule.paused:
                    continue
                sent = {k:v for k,v in (rule.sent_prices or {}).items() if k.split("|")[0] >= today.isoformat()}
                best = fares[0] if fares else None
                status = "partial" if failed and failed < rule.horizon else "error" if failed else "ok"
                values = {"last_checked_at": datetime.utcnow(), "status": status}
                if status != "error":
                    values.update(last_price=best["price"] if best else None, last_offer=best)
                settings = await get_user_settings(rule.telegram_id)
                threshold = getattr(settings, "min_price_drop", None) or MIN_PRICE_DROP
                if (best and best["price"] <= rule.budget and getattr(settings, "notifications_enabled", True) is not False
                        and next_signal(best, sent, threshold)):
                    try:
                        await _send(bot, rule, best, failed)
                    except Exception:
                        logger.warning("Smart alert delivery failed for rule %d", rule.id)
                        values["status"] = "delivery_failed"
                    else:
                        sent[offer_key(best)] = best["price"]
                values["sent_prices"] = sent
                await _record(rule, **values)
            except Exception:
                logger.exception("Smart alert rule %d failed", member.id)
