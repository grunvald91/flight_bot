"""Button-first setup for independent international travel alerts."""
import html
import re
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup

from bot.services import smart_alerts as service
from bot.services.city_resolver import city_resolver
from bot.services.aviasales_api import aviasales_api
from bot.utils.formatters import format_price
from database.crud import get_or_create_user, get_user_settings

router = Router()
router.callback_query.filter(F.message.chat.type == "private")


class SmartStates(StatesGroup):
    origin = State()
    nights = State()
    horizon = State()
    direct = State()
    budget = State()


def keyboard(*pairs):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data)] for text, data in pairs
    ])


def duration(nights):
    return f"{nights} ночей · туда-обратно" if nights else "В одну сторону"


def city(code):
    return html.escape(city_resolver.get_city_name(code))


def status_text(rule):
    return "На паузе" if rule.paused else {
        "new": "Ожидает первой проверки", "ok": "Активно",
        "partial": "Часть дат не проверена", "error": "Источник недоступен",
        "delivery_failed": "Не удалось отправить сообщение",
    }.get(rule.status, "Активно")


async def show_list(message, user_id):
    rules = await service.list_rules(user_id)
    settings = await get_user_settings(user_id)
    text = ("🌍 <b>Умный мониторинг</b>\n"
            "Любая зарубежная страна · проверка каждый час\n"
            "Цены авиабилетов из кэша Aviasales, без проживания.\n")
    if getattr(settings, "notifications_enabled", True) is False:
        text += "\n🔕 Общие оповещения выключены. Включите их в общих настройках.\n"
    pairs = []
    for rule in rules:
        name = city_resolver.get_city_name(rule.origin)
        pairs.append((f"{'⏸' if rule.paused else '🔔'} {name} · {duration(rule.nights)} · {format_price(rule.budget)}", f"smart:card:{rule.id}"))
    pairs.extend([("➕ Создать умное оповещение","smart:new"),
                  ("⚙️ Общие настройки","submgr:open"),("⬅️ Все оповещения","sublist:open")])
    await message.answer(text, reply_markup=keyboard(*pairs))


@router.callback_query(F.data == "smart:list")
async def open_list(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await show_list(callback.message, callback.from_user.id)


@router.callback_query(F.data == "smart:new")
async def start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(SmartStates.origin)
    await callback.answer()
    await callback.message.answer("🌍 Откуда вылетаете? Введите город.",
        reply_markup=keyboard(("Отмена","smart:list")))


@router.message(SmartStates.origin, F.text, F.chat.type == "private")
async def origin(message: Message, state: FSMContext):
    code = city_resolver.resolve(message.text.strip())
    if not code or not re.fullmatch(r"[A-Z]{3}", code) or not city_resolver.get_country_code(code):
        await message.answer("Не нашёл город или страну вылета. Введите другой город.")
        return
    await state.update_data(origin=code)
    await state.set_state(SmartStates.nights)
    await message.answer("Как долго хотите путешествовать?", reply_markup=keyboard(
        *[(duration(n),f"smart:nights:{n}") for n in service.NIGHTS],("Отмена","smart:list")))


@router.callback_query(SmartStates.nights, F.data.regexp(r"^smart:nights:(0|3|5|7|10|14)$"))
async def nights(callback: CallbackQuery, state: FSMContext):
    await state.update_data(nights=int(callback.data.rsplit(":",1)[1]))
    await state.set_state(SmartStates.horizon)
    await callback.answer()
    await callback.message.answer("В каком периоде искать дату вылета?", reply_markup=keyboard(
        *[(f"Ближайшие {n} дней", f"smart:horizon:{n}") for n in service.HORIZONS],("Отмена","smart:list")))


@router.callback_query(SmartStates.horizon, F.data.regexp(r"^smart:horizon:(7|14|30)$"))
async def horizon(callback: CallbackQuery, state: FSMContext):
    await state.update_data(horizon=int(callback.data.rsplit(":",1)[1]))
    await state.set_state(SmartStates.direct)
    await callback.answer()
    await callback.message.answer("Пересадки допустимы?", reply_markup=keyboard(
        ("Любые рейсы","smart:direct:0"),("Только без пересадок","smart:direct:1"),("Отмена","smart:list")))


@router.callback_query(SmartStates.direct, F.data.regexp(r"^smart:direct:[01]$"))
async def direct(callback: CallbackQuery, state: FSMContext):
    await state.update_data(direct=callback.data.endswith(":1"))
    await state.set_state(SmartStates.budget)
    await callback.answer()
    await callback.message.answer("🎯 Максимальная стоимость всех авиабилетов на одного человека в рублях. Выберите сумму или введите свою.",
        reply_markup=keyboard(*[(format_price(n),f"smart:budget:{n}") for n in (5000,10000,15000,25000,50000)],
        ("Отмена","smart:list")))


async def save(message, state, user, budget):
    data = await state.get_data()
    if not all(k in data for k in ("origin","nights","horizon","direct")):
        await state.clear()
        await message.answer("Настройки устарели. Начните заново.",reply_markup=keyboard(("Новый мониторинг","smart:new")))
        return
    await get_or_create_user(user.id, user.username, user.first_name)
    result = await service.create_rule(user.id,data["origin"],data["nights"],data["horizon"],budget,data["direct"])
    await state.clear()
    text = {
        "ok":"✅ Мониторинг сохранён. Поиск выполняется каждый час.",
        "duplicate":"Такой мониторинг уже существует.",
        "user_limit":"Доступно до 3 умных оповещений. Удалите ненужное.",
        "capacity":"Лимит тестового запуска достигнут. Новый поисковый режим пока недоступен.",
        "invalid":"Некорректные параметры.",
    }.get(result,"Не удалось сохранить мониторинг.")
    await message.answer(text)
    await show_list(message,user.id)


@router.callback_query(SmartStates.budget, F.data.regexp(r"^smart:budget:[0-9]{1,8}$"))
async def budget_button(callback: CallbackQuery, state: FSMContext):
    value = int(callback.data.rsplit(":",1)[1])
    if not 100 <= value <= 10_000_000:
        await callback.answer("Некорректная сумма", show_alert=True)
        return
    await callback.answer()
    await save(callback.message,state,callback.from_user,value)


@router.message(SmartStates.budget, F.text, F.chat.type == "private")
async def budget_text(message: Message, state: FSMContext):
    raw = message.text.strip().replace(" ","").replace("\u00a0","")
    if not re.fullmatch(r"[0-9]{1,8}",raw) or not 100 <= int(raw) <= 10_000_000:
        await message.answer("Введите целое число от 100 до 10 000 000 ₽.")
        return
    await save(message,state,message.from_user,int(raw))


async def show_card(message,rule):
    text = (f"🌍 <b>Умное оповещение</b>\nИз {city(rule.origin)} · любая зарубежная страна\n"
            f"{duration(rule.nights)} · вылет в ближайшие {rule.horizon} дней\n"
            f"Бюджет билетов: <b>{format_price(rule.budget)}</b>\n"
            f"{'Без пересадок' if rule.direct else 'Любые рейсы'}\n"
            f"Статус: {status_text(rule)}\n")
    if rule.last_checked_at:
        text += f"Проверка: {rule.last_checked_at.strftime('%d.%m %H:%M')} UTC\n"
    rows = []
    if rule.last_offer:
        offer = rule.last_offer
        text += f"Последняя найденная цена: <b>{format_price(offer['price'])}</b> · {city(offer['destination'])}\n"
        url = aviasales_api.build_search_link(rule.origin, offer["destination"], offer["departure"], offer["returning"])
        rows.append([InlineKeyboardButton(text="Проверить билеты",url=url)])
    text += "Цена ориентировочная, без проживания."
    rows.extend([[InlineKeyboardButton(text="▶️ Возобновить" if rule.paused else "⏸ Пауза", callback_data=f"smart:pause:{rule.id}:{int(not rule.paused)}")],
                 [InlineKeyboardButton(text="🗑 Удалить",callback_data=f"smart:askdelete:{rule.id}")],
                 [InlineKeyboardButton(text="⬅️ Умный мониторинг",callback_data="smart:list")]])
    await message.answer(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^smart:card:[0-9]+$"))
async def card(callback: CallbackQuery,state: FSMContext):
    await state.clear()
    rule = await service.get_rule(callback.from_user.id,int(callback.data.rsplit(":",1)[1]))
    if not rule:
        await callback.answer("Оповещение не найдено",show_alert=True)
        return
    await callback.answer()
    await show_card(callback.message,rule)


@router.callback_query(F.data.regexp(r"^smart:pause:[0-9]+:[01]$"))
async def pause(callback: CallbackQuery):
    _,_,rid,flag = callback.data.split(":")
    ok = await service.change_rule(callback.from_user.id,int(rid),paused=flag=="1")
    await callback.answer("Сохранено" if ok else "Не найдено",show_alert=not ok)
    rule = await service.get_rule(callback.from_user.id,int(rid))
    if rule:
        await show_card(callback.message,rule)


@router.callback_query(F.data.regexp(r"^smart:askdelete:[0-9]+$"))
async def ask_delete(callback: CallbackQuery):
    rid = int(callback.data.rsplit(":",1)[1])
    if not await service.get_rule(callback.from_user.id,rid):
        await callback.answer("Не найдено",show_alert=True)
        return
    await callback.answer()
    await callback.message.answer("Удалить это оповещение?",reply_markup=keyboard(
        ("Удалить",f"smart:delete:{rid}"),("Отмена",f"smart:card:{rid}")))


@router.callback_query(F.data.regexp(r"^smart:delete:[0-9]+$"))
async def remove(callback: CallbackQuery):
    ok = await service.change_rule(callback.from_user.id,int(callback.data.rsplit(":",1)[1]),remove=True)
    await callback.answer("Удалено" if ok else "Не найдено",show_alert=not ok)
    await show_list(callback.message,callback.from_user.id)
