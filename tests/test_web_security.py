"""Boundary checks that do not require a live database or passenger credentials."""
import asyncio
import os

import httpx


os.environ.setdefault("BOT_TOKEN", "123456:test-token")
from bot.passenger_web import EXPECTED_ORIGIN, app


def test_streamed_oversize_profile_request_is_rejected_without_content_length():
    async def check():
        async def stream():
            yield b"x" * 9000

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=EXPECTED_ORIGIN) as client:
            response = await client.post(
                "/api/profile", content=stream(),
                headers={"Origin": EXPECTED_ORIGIN, "Content-Type": "application/json"},
            )
        assert response.status_code == 413

    asyncio.run(check())


def test_small_streamed_json_reaches_validation_without_leaking_input():
    async def check():
        async def stream():
            yield b'{"document_number":"SYNTHETICDOC"}'

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=EXPECTED_ORIGIN) as client:
            response = await client.post(
                "/api/profile", content=stream(),
                headers={"Origin": EXPECTED_ORIGIN, "Content-Type": "application/json"},
            )
        assert response.status_code == 422
        assert "SYNTHETICDOC" not in response.text

    asyncio.run(check())
