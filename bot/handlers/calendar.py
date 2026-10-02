from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from bot.keyboards.calendar_kb import calendar_keyboard

router = Router()


@router.callback_query(F.data == "calnoop")
async def calendar_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("calnav:"))
async def calendar_navigate(callback: CallbackQuery):
    try:
        _, purpose, year_month = (callback.data or "").split(":", 2)
        year, month = (int(value) for value in year_month.split("-", 1))
    except (TypeError, ValueError):
        await callback.answer("Не удалось открыть месяц", show_alert=True)
        return

    await callback.message.edit_reply_markup(
        reply_markup=calendar_keyboard(
            purpose,
            year,
            month,
            include_no_date=purpose == "search",
        )
    )
    await callback.answer()


@router.callback_query(F.data.startswith("calcancel:"))
async def calendar_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Поиск отменён.")
    await callback.answer()
