import html
import logging
import re
from datetime import datetime

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
    await callback.answer("Проверяю визу и документы для поездки…")

    try:
        rule = await find_entry_rule(country_name, country_code)
    except Exception as exc:
        logger.warning("KDMID entry lookup failed for %s: %s", country_code, exc)
        await callback.message.answer(
            f"<b>ℹ️ {html.escape(country_name)} — виза и документы для граждан РФ</b>\n\n"
            "⚠️ Не удалось загрузить правила. Откройте официальный источник.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Официальный источник МИД РФ", url=KDMID_SOURCE_URL)
            ]]),
        )
        return

    if rule is None:
        await callback.message.answer(
            f"<b>ℹ️ {html.escape(country_name)} — виза и документы для граждан РФ</b>\n\n"
            "⚠️ Правила для этой страны не найдены автоматически.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Официальный источник МИД РФ", url=KDMID_SOURCE_URL)
            ]]),
        )
        return

    lines = [
        f"<b>ℹ️ {html.escape(rule.country)} — виза и документы для граждан РФ</b>",
        "",
        *_simple_entry_lines(
            rule.ordinary_passport,
            rule.details,
            stay=rule.stay,
            verdict=rule.verdict,
        ),
    ]

    checked = _format_checked_at(rule.updated_at)
    if rule.verified:
        if checked:
            lines.extend(["", f"<i>Правила сверены с источниками: {checked}</i>"])
    else:
        if checked:
            lines.extend(["", f"<i>Данные открытой базы МИД РФ, загружены {checked}</i>"])
        lines.append(
            "<i>⚠️ База МИД может отставать от свежих изменений. "
            "Перед покупкой билета сверьтесь по официальной ссылке.</i>"
        )
    if rule.stale:
        lines.append("<i>⚠️ Используется сохранённая копия официальных данных.</i>")

    buttons = [[
        InlineKeyboardButton(
            text="Официальные правила",
            url=rule.source_url or KDMID_SOURCE_URL,
        )
    ]]
    if rule.extra_source_url:
        buttons.append([
            InlineKeyboardButton(text="Подробнее", url=rule.extra_source_url)
        ])

    await callback.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        disable_web_page_preview=True,
    )


_WINDOW_RE = re.compile(
    r"(?:в течение|за|в|каждые|любые|любых|каждом|каждого)\s+180"
    r"|180[\s-]*дневн|период\w*\s+(?:в\s+)?180"
)


def _stay_line(low: str) -> str | None:
    """Срок пребывания из текста открытой базы МИД.

    Если в тексте есть окно в 180 дней и два срока («до 60 дней, не более 90
    дней в течение 180»), показываем оба, а не только первое число.
    """
    if _WINDOW_RE.search(low):
        numbers = [
            int(n)
            for n in re.findall(r"(\d{1,3})\s*(?:дн|сут)", low)
            if int(n) != 180
        ]
        if numbers:
            total = max(numbers)
            single = min(numbers)
            if single < total:
                return (
                    f"до {single} дней за одну поездку, всего до {total} дней "
                    "в течение 180 дней"
                )
            return f"до {total} дней в течение 180 дней"

    stay = re.search(
        r"(?:до|не более|срок(?:ом)? до)\s*(\d{1,3})\s*(?:дн\w*|сут\w*)",
        low,
    )
    if stay:
        return f"до {stay.group(1)} дней"

    long_stay = re.search(
        r"(?:до|не более|срок(?:ом)? до)\s*(\d{1,2})\s*(мес\w*|год\w*|лет)",
        low,
    )
    if long_stay:
        return f"до {long_stay.group(1)} {long_stay.group(2)}"
    return None


def _simple_entry_lines(
    regime: str,
    details: str = "",
    *,
    stay: str = "",
    verdict: str = "",
) -> list[str]:
    raw = " ".join((regime or "").split())
    low = raw.lower().replace("ё", "е")
    lines: list[str] = []

    if verdict:
        lines.append(verdict)
    elif "без виз" in low or "виза не нуж" in low or "виза не треб" in low:
        lines.append("✅ <b>Виза не нужна</b>")
    elif "электрон" in low and "виз" in low:
        lines.append("🟡 <b>Нужна электронная виза</b>")
    elif "по прибыт" in low and "виз" in low:
        lines.append("🟡 <b>Виза оформляется по прибытии</b>")
    elif "виз" in low:
        lines.append("🔴 <b>Нужна виза</b>")
    else:
        lines.append("ℹ️ <b>Есть специальные условия въезда</b>")

    stay_text = (stay or "").strip()
    if stay_text:
        lines.append(f"🕒 <b>Срок:</b> {html.escape(stay_text)}")
    else:
        parsed = _stay_line(low)
        if parsed:
            lines.append(f"🕒 <b>Срок:</b> {parsed}")

    detail_text = (details or "").strip()
    for part in detail_text.splitlines():
        clean = " ".join(part.split()).strip()
        low_part = clean.lower().replace("ё", "е")
        if not clean:
            continue
        if low_part.startswith("документы:"):
            value = clean.split(":", 1)[1].strip()
            if value:
                lines.append(f"🪪 <b>Документы:</b> {html.escape(value)}")
        elif low_part.startswith("уведомление:"):
            value = clean.split(":", 1)[1].strip()
            if value:
                lines.append(f"🏠 <b>По прибытии:</b> {html.escape(value)}")
        elif low_part.startswith("важно:"):
            value = clean.split(":", 1)[1].strip()
            if value:
                lines.append(f"⚠️ <b>Важно:</b> {html.escape(value)}")

    if lines and lines[0].startswith("ℹ️") and raw:
        short = raw
        if len(short) > 220:
            short = short[:217].rstrip() + "…"
        lines.append(f"📌 {html.escape(short)}")

    return lines


def _format_checked_at(value: str | None) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%d.%m.%Y")
    except ValueError:
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError:
            return None
