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
    if len(regime) > 2200:
        regime = regime[:2197].rstrip() + "…"

    details = rule.details.strip()
    if len(details) > 1400:
        details = details[:1397].rstrip() + "…"

    cache_note = (
        "\n\n⚠️ Сейчас показана последняя сохранённая копия официальных данных: "
        "обновление источника временно недоступно."
        if rule.stale
        else ""
    )

    lines = [
        f"<b>ℹ️ {html.escape(rule.country)} — въезд для граждан РФ</b>",
        "",
        f"<b>Визовый режим:</b>\n{html.escape(regime)}",
    ]
    if details:
        lines.extend(["", f"<b>Что важно:</b>\n{html.escape(details)}"])
    if rule.updated_at:
        lines.extend(["", f"<i>Проверено: {html.escape(str(rule.updated_at))}</i>"])
    if cache_note:
        lines.append(cache_note.strip())

    buttons = [[
        InlineKeyboardButton(
            text="Официальный источник",
            url=rule.source_url or KDMID_SOURCE_URL,
        )
    ]]
    if rule.extra_source_url:
        buttons.append([
            InlineKeyboardButton(
                text="Уведомление о прибытии",
                url=rule.extra_source_url,
            )
        ])

    await callback.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        disable_web_page_preview=True,
    )
