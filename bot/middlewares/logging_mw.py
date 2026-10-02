"""Middleware для логирования всех запросов в БД."""
import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, InlineQuery

from database.crud import log_request
from bot.services.privacy import request_log_identity

logger = logging.getLogger(__name__)


class RequestLoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        try:
            await self._log_event(event)
        except Exception as e:
            logger.error("Logging middleware failed; details suppressed")

        return await handler(event, data)

    async def _log_event(self, event):
        if isinstance(event, Message):
            user = event.from_user
            if not user:
                return
            if (event.text or "").split(maxsplit=1)[0].split("@", 1)[0] == "/profile":
                return  # Do not persist identifiable metadata for profile entry.
            if (event.text or "").startswith("/"):
                req_type = "command"
            else:
                req_type = "message"
            await log_request(
                telegram_id=request_log_identity(user.id),
                username=None,
                first_name=None,
                request_type=req_type,
                request_data=None,
            )

        elif isinstance(event, CallbackQuery):
            user = event.from_user
            if not user:
                return
            if (event.data or "").startswith(("tutubook:", "profile:")):
                return
            await log_request(
                telegram_id=request_log_identity(user.id),
                username=None,
                first_name=None,
                request_type="callback",
                request_data=None,
            )

        elif isinstance(event, InlineQuery):
            user = event.from_user
            if not user:
                return
            await log_request(
                telegram_id=request_log_identity(user.id),
                username=None,
                first_name=None,
                request_type="inline",
                request_data=None,
            )
