"""Middleware для логирования всех запросов в БД."""
import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, InlineQuery

from database.crud import log_request

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
            logger.error("Logging middleware error: %s", e)

        return await handler(event, data)

    async def _log_event(self, event):
        if isinstance(event, Message):
            user = event.from_user
            if not user:
                return
            text = event.text or ""
            if text.startswith("/"):
                req_type = "command"
            else:
                req_type = "message"
            await log_request(
                telegram_id=user.id,
                username=user.username,
                first_name=user.first_name,
                request_type=req_type,
                request_data=text[:500],
            )

        elif isinstance(event, CallbackQuery):
            user = event.from_user
            if not user:
                return
            await log_request(
                telegram_id=user.id,
                username=user.username,
                first_name=user.first_name,
                request_type="callback",
                request_data=event.data[:500] if event.data else None,
            )

        elif isinstance(event, InlineQuery):
            user = event.from_user
            if not user:
                return
            await log_request(
                telegram_id=user.id,
                username=user.username,
                first_name=user.first_name,
                request_type="inline",
                request_data=event.query[:500] if event.query else None,
            )
