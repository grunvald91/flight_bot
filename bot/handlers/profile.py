import html
import logging
from urllib.parse import quote

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from bot.config import PASSENGER_WEBAPP_URL
from bot.services.passenger_profile_store import create_web_launch, has_passenger_profile
from bot.services.tutu_mcp import passenger_booking_cache

logger = logging.getLogger(__name__)
router = Router()


def _webapp_markup(url: str, label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, web_app=WebAppInfo(url=url)),
    ]])


async def _profile_message(message: Message, owner_id: int) -> None:
    if message.chat.type != "private":
        await message.answer("Профиль пассажира доступен только в личном чате с ботом.")
        return
    exists = await has_passenger_profile(owner_id)
    token = await create_web_launch(owner_id, "profile")
    url = f"{PASSENGER_WEBAPP_URL}/#token={quote(token, safe='')}"
    status = "Профиль сохранён в зашифрованном виде." if exists else "Профиль ещё не заполнен."
    await message.answer(
        "<b>Защищённый профиль пассажира</b>\n\n"
        f"{status}\n"
        "Данные вводятся только в HTTPS-форме, не отправляются сообщениями в Telegram и "
        "хранятся на сервере в зашифрованном виде. В форме можно изменить или полностью удалить профиль.",
        reply_markup=_webapp_markup(url, "🔐 Открыть защищённую форму"),
    )


@router.message(Command("profile"))
async def cmd_profile(message: Message):
    await _profile_message(message, message.from_user.id)


@router.callback_query(F.data == "profile:open")
async def cb_profile_open(callback: CallbackQuery):
    await callback.answer()
    await _profile_message(callback.message, callback.from_user.id)


@router.callback_query(F.data.startswith("tutubook:"))
async def cb_book_with_profile(callback: CallbackQuery):
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("Защищённая форма доступна только в личном чате с ботом.", show_alert=True)
        return
    key = (callback.data or "").split(":", 1)[1]
    bundle = passenger_booking_cache.get(key)
    if not bundle:
        await callback.answer("Код из старого сообщения недоступен. Откройте новый результат поиска.", show_alert=True)
        return
    await _book_with_profile(callback, bundle)


async def _book_with_profile(callback: CallbackQuery, bundle: dict):
    """Start a new short-lived, one-use protected form for a selected offer."""
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("Защищённая форма доступна только в личном чате с ботом.", show_alert=True)
        return
    checkout_ref = bundle.get("checkout_ref") or {}
    product = checkout_ref.get("product_type") or checkout_ref.get("transport") or "avia"
    if product != "avia":
        await callback.answer("Автооформление пока доступно только для авиабилетов.", show_alert=True)
        return
    if int(checkout_ref.get("passengers_full") or 1) != 1 or int(checkout_ref.get("passengers_child") or 0) or int(checkout_ref.get("passengers_infant") or 0):
        await callback.answer("Сохранённый профиль пока поддерживает оформление одного взрослого.", show_alert=True)
        return

    try:
        token = await create_web_launch(
            callback.from_user.id,
            "booking",
            payload={
                "checkout_ref": checkout_ref,
                "offer_summary": bundle.get("offer_summary") or {},
            },
            ttl_minutes=10,
        )
    except RuntimeError:
        await callback.answer(
            "Предыдущая попытка по этому предложению имеет неопределённый результат. Проверьте email; повтор заблокирован.",
            show_alert=True,
        )
        return
    url = f"{PASSENGER_WEBAPP_URL}/#token={quote(token, safe='')}"
    await callback.answer()
    await callback.message.answer(
        "<b>Оформление с сохранённым пассажиром</b>\n\n"
        "Откройте защищённую форму. Проверьте введённые данные пассажира и контакты; "
        "перед оформлением форма покажет маскированный профиль, рейс и цену. "
        "Неоплаченная бронь будет создана только после отдельного подтверждения внутри формы.",
        reply_markup=_webapp_markup(url, "🔐 Проверить данные и оформить"),
    )
