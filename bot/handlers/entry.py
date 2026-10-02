import html
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.services.entry_rules import KDMID_SOURCE_URL, find_entry_rule
from bot.services.international_discovery import country_name_ru

logger = logging.getLogger(__name__)
router = Router()


@router.callback_query(F.data.startswith("entry:"))
async def show_entry_rules(callback: CallbackQuery):
    country_code = (callback.data or "").split(":", 1)[1].strip().upper()
    if len(country_code) != 2:
        await callback.answer("Не удалось определить страну.", show_alert=True)
        return

    country_name = country_name_ru(country_code)
    await callback.answer("Проверяю официальный визовый режим МИД РФ…")

    try:
        rule = await find_entry_rule(country_name, country_code)
    except Exception as exc:
        logger.warning("KDMID entry lookup failed for %s: %s", country_code, exc)
        await callback.message.answer(
            f"<b>ℹ️ Въезд в {html.escape(country_name)} для граждан РФ</b>\n\n"
            "Сейчас не удалось получить официальный набор МИД. "
            "Проверьте правила непосредственно на сайте Консульского департамента МИД РФ.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Официальный источник МИД РФ", url=KDMID_SOURCE_URL)
            ]]),
        )
        return

    if rule is None:
        await callback.message.answer(
            f"<b>ℹ️ Въезд в {html.escape(country_name)} для граждан РФ</b>\n\n"
            "В официальном наборе МИД не удалось однозначно найти эту страну. "
            "Перед поездкой проверьте требования по ссылке ниже.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Официальный источник МИД РФ", url=KDMID_SOURCE_URL)
            ]]),
        )
        return

    regime = rule.ordinary_passport.strip()
    if len(regime) > 2800:
        regime = regime[:2797].rstrip() + "…"

    cache_note = (
        "\n\n⚠️ Сейчас показана последняя сохранённая копия официальных данных: "
        "обновление источника временно недоступно."
        if rule.stale
        else ""
    )
    await callback.message.answer(
        f"<b>ℹ️ {html.escape(rule.country)} — въезд для граждан РФ</b>\n\n"
        f"<b>Обычный загранпаспорт:</b>\n{html.escape(regime)}"
        f"{cache_note}\n\n"
        "Это официальный <b>визовый режим</b>. Он не всегда включает все практические "
        "условия поездки: срок действия паспорта, обратный билет, страховку, анкеты, "
        "таможенные или санитарные требования. Их стоит проверить перед вылетом.\n\n"
        "<i>Источник: Консульский департамент МИД России.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Официальный источник МИД РФ", url=KDMID_SOURCE_URL)
        ]]),
        disable_web_page_preview=True,
    )
