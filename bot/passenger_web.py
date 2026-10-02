import asyncio
import logging
import os
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from bot.config import BOT_TOKEN, PASSENGER_WEBAPP_URL
from bot.services.passenger_crypto import ProfileCrypto
from bot.services.passenger_profile_store import (
    authenticate_web_session,
    begin_booking_invocation,
    delete_passenger_profile,
    ensure_checkout_unique_index,
    exchange_web_launch,
    finish_booking,
    get_profile_envelope,
    prepare_booking,
    purge_expired_web_data,
    rotate_session_csrf,
    save_profile_envelope,
)
from bot.services.telegram_web_auth import validate_telegram_init_data
from bot.services.tutu_mcp import tutu_mcp
from database.database import init_db

logger = logging.getLogger("passenger_web")
EXPECTED_ORIGIN = f"{urlparse(PASSENGER_WEBAPP_URL).scheme}://{urlparse(PASSENGER_WEBAPP_URL).netloc}"
COOKIE_NAME = "__Host-aticket_profile_session"
SESSION_SECONDS = 15 * 60
ALLOWED_PAYMENT_HOSTS = {
    item.strip().lower()
    for item in os.getenv(
        "TUTU_PAYMENT_HOSTS",
        "tutu.ru,www.tutu.ru,avia.tutu.ru,checkout.tutu.ru,secure.tutu.ru",
    ).split(",")
    if item.strip()
}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(RequestValidationError)
async def safe_request_validation_error(_request: Request, _error: RequestValidationError):
    # FastAPI's default 422 response can echo the rejected passport field as `input`.
    return JSONResponse({"detail": "Проверьте формат заполненных полей"}, status_code=422)


crypto: ProfileCrypto | None = None
_rate: dict[str, deque[float]] = defaultdict(deque)
_purge_task: asyncio.Task | None = None


class SessionExchange(BaseModel):
    token: str
    init_data: str


class ProfileInput(BaseModel):
    first_name: str
    last_name: str
    middle_name: str | None = None
    birthday: str
    sex: str
    document_type: str
    document_number: str
    expires_at: str
    email: str
    phone: str
    storage_consent: bool


class BookingConfirmation(BaseModel):
    confirmed: bool
    confirmation_token: str
    reviewed_profile: ProfileInput


@app.on_event("startup")
async def startup() -> None:
    global crypto, _purge_task
    crypto = ProfileCrypto.from_environment()
    await init_db()
    await ensure_checkout_unique_index()
    await purge_expired_web_data()
    _purge_task = asyncio.create_task(_purge_loop())


async def _purge_loop() -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            await purge_expired_web_data()
        except Exception:
            logger.error("Passenger profile retention purge failed; details suppressed")


@app.on_event("shutdown")
async def shutdown() -> None:
    if _purge_task:
        _purge_task.cancel()
        try:
            await _purge_task
        except asyncio.CancelledError:
            pass


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    try:
        if int(request.headers.get("content-length", "0")) > 8192:
            return JSONResponse({"detail": "Запрос слишком большой"}, status_code=413)
    except ValueError:
        return JSONResponse({"detail": "Некорректная длина запроса"}, status_code=400)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.headers.get("origin") != EXPECTED_ORIGIN:
            return JSONResponse({"detail": "Недопустимый источник запроса"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Межсайтовый запрос отклонён"}, status_code=403)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return JSONResponse({"detail": "Требуется application/json"}, status_code=415)
        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 8192:
                return JSONResponse({"detail": "Запрос слишком большой"}, status_code=413)
            chunks.append(chunk)
        request._body = b"".join(chunks)
    try:
        response = await call_next(request)
    except Exception:
        logger.error("Passenger web request failed; details suppressed")
        response = JSONResponse({"detail": "Внутренняя ошибка. Повторите позже."}, status_code=500)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors https://web.telegram.org https://*.telegram.org"
    )
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    return response


def _profile_crypto() -> ProfileCrypto:
    if crypto is None:
        raise RuntimeError("Passenger profile crypto is not initialized")
    return crypto


def _rate_limit(key: str, limit: int = 20) -> None:
    now = time.time()
    bucket = _rate[key]
    while bucket and bucket[0] <= now - 60:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(429, "Слишком много запросов. Подождите минуту.")
    bucket.append(now)


async def _session(request: Request, *, csrf: bool = False, rate_limit: bool = False) -> dict:
    raw_session = request.cookies.get(COOKIE_NAME, "")
    raw_csrf = request.headers.get("x-csrf-token", "") if csrf else None
    session = await authenticate_web_session(raw_session, raw_csrf)
    if not session:
        raise HTTPException(401, "Сессия истекла. Откройте форму заново из бота.")
    if rate_limit:
        _rate_limit(f"launch:{session['launch_id']}")
    return session


def _clean_text(value: str | None, *, min_len: int = 0, max_len: int = 100) -> str:
    text = " ".join((value or "").strip().split())
    if len(text) < min_len or len(text) > max_len:
        raise HTTPException(422, "Проверьте длину заполненных полей")
    return text


def _validated_profile(data: ProfileInput) -> dict:
    if not data.storage_consent:
        raise HTTPException(422, "Нужно подтвердить сохранение данных в зашифрованном профиле")
    first_name = _clean_text(data.first_name, min_len=2, max_len=60).upper()
    last_name = _clean_text(data.last_name, min_len=2, max_len=60).upper()
    middle_name = _clean_text(data.middle_name, max_len=60).upper() or None
    name_re = re.compile(r"^[A-Z][A-Z '\-]{1,59}$")
    for value in (first_name, last_name):
        if not name_re.fullmatch(value):
            raise HTTPException(422, "Имя и фамилия должны быть латиницей точно как в загранпаспорте")
    if middle_name and not name_re.fullmatch(middle_name):
        raise HTTPException(422, "Отчество должно быть латиницей как в документе или оставлено пустым")
    if data.sex not in {"MALE", "FEMALE"}:
        raise HTTPException(422, "Выберите пол пассажира")
    if data.document_type != "FOREIGN_PASSPORT":
        raise HTTPException(422, "Сейчас поддерживается только загранпаспорт")
    try:
        birthday = date.fromisoformat(data.birthday)
        expires_at = date.fromisoformat(data.expires_at)
    except ValueError as exc:
        raise HTTPException(422, "Проверьте даты") from exc
    today = date.today()
    if birthday >= today or birthday.year < today.year - 120:
        raise HTTPException(422, "Проверьте дату рождения")
    if expires_at <= today:
        raise HTTPException(422, "Срок действия загранпаспорта уже истёк")
    document_number = re.sub(r"\s+", "", data.document_number).upper()
    if not re.fullmatch(r"[A-Z0-9<]{4,30}", document_number):
        raise HTTPException(422, "Проверьте номер загранпаспорта")
    email = _clean_text(data.email, min_len=5, max_len=254).lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(422, "Проверьте email")
    phone = re.sub(r"[\s()\-]", "", data.phone)
    if not re.fullmatch(r"\+?[0-9]{10,15}", phone):
        raise HTTPException(422, "Телефон должен содержать от 10 до 15 цифр")
    return {
        "version": 1,
        "passenger": {
            "first_name": first_name,
            "last_name": last_name,
            "middle_name": middle_name,
            "birthday": birthday.isoformat(),
            "sex": data.sex,
            "voyager_type": "ADULT",
            "document": {
                "document_type": "FOREIGN_PASSPORT",
                "number": document_number,
                "expires_at": expires_at.isoformat(),
            },
        },
        "customer": {
            "first_name": first_name,
            "last_name": last_name,
            "middle_name": middle_name,
            "email": email,
            "phone": phone,
            "time_zone": "Europe/Moscow",
        },
        "consent": {"storage": True, "version": 1, "accepted_at": datetime.utcnow().isoformat()},
    }


def _flatten_profile(profile: dict) -> dict:
    passenger = profile.get("passenger") or {}
    document = passenger.get("document") or {}
    customer = profile.get("customer") or {}
    return {
        "first_name": passenger.get("first_name", ""),
        "last_name": passenger.get("last_name", ""),
        "middle_name": passenger.get("middle_name") or "",
        "birthday": passenger.get("birthday", ""),
        "sex": passenger.get("sex", ""),
        "document_type": document.get("document_type", "FOREIGN_PASSPORT"),
        "document_number": document.get("number", ""),
        "expires_at": document.get("expires_at", ""),
        "email": customer.get("email", ""),
        "phone": customer.get("phone", ""),
    }


def _mask_profile(profile: dict) -> dict:
    flat = _flatten_profile(profile)
    number = flat["document_number"]
    return {
        "name": f"{flat['last_name']} {flat['first_name'][:1]}." + (f" {flat['middle_name'][:1]}." if flat["middle_name"] else ""),
        "birth_year": flat["birthday"][:4],
        "document": f"••••••{number[-4:]}",
        "expires_at": flat["expires_at"],
        "email": re.sub(r"(^.).*(@.*$)", r"\1•••\2", flat["email"]),
        "phone": f"••••{flat['phone'][-4:]}",
    }


async def _load_profile(telegram_id: int) -> tuple[dict, int] | None:
    row = await get_profile_envelope(telegram_id)
    if not row:
        return None
    try:
        return _profile_crypto().decrypt(row[0], owner_id=telegram_id), row[1]
    except Exception as exc:
        logger.error("Could not decrypt passenger profile; details suppressed")
        raise HTTPException(500, "Не удалось открыть профиль. Обратитесь к администратору.") from exc


def _safe_avia_offer(checkout_ref: dict) -> dict:
    if checkout_ref.get("is_multi_pnr") or checkout_ref.get("checkout_parts"):
        raise HTTPException(409, "Отдельные билеты нельзя оформить одним подтверждением. Перейдите на Туту.")
    allowed = {
        "offer_hash", "departure_geo_city_id", "arrival_geo_city_id", "departure_at",
        "service_class", "passengers_full", "passengers_child", "passengers_infant",
        "return_departure_at", "card_id", "result_id", "departure_avia_id", "arrival_avia_id",
    }
    clean = {key: value for key, value in checkout_ref.items() if key in allowed and value is not None}
    required = {"offer_hash", "departure_geo_city_id", "arrival_geo_city_id", "departure_at"}
    if not required.issubset(clean):
        raise HTTPException(409, "Предложение не содержит данных для безопасного оформления")
    if int(clean.get("passengers_full", 1)) != 1 or int(clean.get("passengers_child", 0)) or int(clean.get("passengers_infant", 0)):
        raise HTTPException(409, "Автооформление сейчас поддерживает одного взрослого пассажира")
    return clean


def _booking_snapshot(profile: dict, payload: dict) -> dict:
    summary = payload.get("offer_summary") or {}
    if not summary.get("from") or not summary.get("to") or summary.get("price_amount") is None:
        raise HTTPException(409, "Не удалось подтвердить маршрут и цену. Повторите поиск.")
    segments = summary.get("segments") or []
    if not segments or any(not (s.get("from") and s.get("to") and s.get("flight") and s.get("departure_at")) for s in segments):
        raise HTTPException(409, "Не удалось подтвердить все рейсы маршрута. Оформите билет на Туту.")
    offer = _safe_avia_offer(payload.get("checkout_ref") or {})
    if summary.get("departure_at") != offer["departure_at"]:
        raise HTTPException(409, "Время выбранного предложения изменилось. Повторите поиск.")
    return {"passenger": profile["passenger"], "customer": profile["customer"], "offer": offer, "summary": summary}


def _validate_payment_url(value: str) -> str:
    if not value or any(ord(char) < 32 for char in value):
        raise ValueError("Invalid payment URL")
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid payment URL port") from exc
    host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or port not in {None, 443} or host not in ALLOWED_PAYMENT_HOSTS:
        raise ValueError("Untrusted payment URL")
    return value


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index():
    path = Path(__file__).parent / "templates" / "passenger_profile.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/assets/app.css")
async def app_css():
    return FileResponse(Path(__file__).parent / "static" / "passenger_profile.css", media_type="text/css")


@app.get("/assets/app.js")
async def app_js():
    return FileResponse(Path(__file__).parent / "static" / "passenger_profile.js", media_type="application/javascript")


@app.get("/assets/telegram-web-app.js")
async def telegram_web_app_js() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "telegram-web-app.js", media_type="application/javascript")


@app.post("/api/session/exchange")
async def api_exchange(data: SessionExchange, response: Response, request: Request):
    # The TLS stream dispatcher may hide the real client IP. Keep this a broad
    # coarse backstop and enforce a tighter limit on verified Telegram identity.
    _rate_limit(f"exchange-ip:{request.client.host if request.client else 'unknown'}", 600)
    if len(data.token) < 32 or len(data.token) > 128:
        raise HTTPException(401, "Ссылка запуска недействительна")
    try:
        identity = validate_telegram_init_data(data.init_data, BOT_TOKEN)
    except ValueError:
        raise HTTPException(401, "Подтверждение Telegram не прошло проверку") from None
    _rate_limit(f"exchange-user:{identity.user_id}", 8)
    raw_session = secrets.token_urlsafe(32)
    raw_csrf = secrets.token_urlsafe(24)
    context = await exchange_web_launch(
        data.token,
        identity.user_id,
        identity.init_hash,
        raw_session,
        raw_csrf,
        session_minutes=SESSION_SECONDS // 60,
    )
    if not context:
        raise HTTPException(401, "Ссылка устарела, уже использована или принадлежит другому пользователю")
    response.set_cookie(
        COOKIE_NAME,
        raw_session,
        max_age=SESSION_SECONDS,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return {"ok": True, "csrf": raw_csrf}


@app.get("/api/context")
async def api_context(request: Request):
    session = await _session(request)
    loaded = await _load_profile(session["telegram_id"])
    base = {
        "purpose": session["purpose"],
        "has_profile": loaded is not None,
        "profile_masked": _mask_profile(loaded[0]) if loaded else None,
    }
    if session["purpose"] != "booking":
        return base
    state = session.get("checkout_state") or "DRAFT"
    if state == "SUCCEEDED" and session.get("result_encrypted"):
        result = _profile_crypto().decrypt(
            session["result_encrypted"], owner_id=session["telegram_id"], purpose="checkout-result"
        )
        return {**base, "booking_state": state, "booking_result": result}
    if state in {"INVOKING", "UNKNOWN"}:
        return {**base, "booking_state": "UNKNOWN"}
    if not loaded:
        return {**base, "booking_state": "NEEDS_PROFILE", "offer_summary": session["payload"].get("offer_summary")}
    return {**base, "booking_state": "DRAFT", "offer_summary": session["payload"].get("offer_summary")}


@app.post("/api/session/csrf")
async def api_session_csrf(request: Request):
    await _session(request, rate_limit=True)
    csrf = secrets.token_urlsafe(24)
    if not await rotate_session_csrf(request.cookies.get(COOKIE_NAME, ""), csrf):
        raise HTTPException(401, "Сессия истекла")
    return {"csrf": csrf}


@app.post("/api/booking/prepare")
async def api_prepare_booking(request: Request):
    session = await _session(request, csrf=True, rate_limit=True)
    if session["purpose"] != "booking" or session.get("checkout_state") in {"INVOKING", "UNKNOWN", "SUCCEEDED"}:
        raise HTTPException(409, "Создание или повторное подтверждение брони недоступно")
    loaded = await _load_profile(session["telegram_id"])
    if not loaded:
        raise HTTPException(409, "Сначала сохраните профиль")
    profile, profile_version = loaded
    snapshot = _booking_snapshot(profile, session["payload"])
    snapshot_hash = _profile_crypto().fingerprint(snapshot, owner_id=session["telegram_id"], purpose="booking-snapshot")
    confirmation_token = secrets.token_urlsafe(32)
    prepared = await prepare_booking(
        session["launch_id"], session["telegram_id"], snapshot_hash, profile_version,
        hashlib_sha256(confirmation_token),
    )
    if not prepared:
        raise HTTPException(409, "Состояние оформления изменилось. Откройте форму заново.")
    return {
        "booking_state": "READY",
        "profile_masked": _mask_profile(profile),
        "offer_summary": session["payload"].get("offer_summary"),
        "confirmation_token": confirmation_token,
    }


def hashlib_sha256(value: str) -> str:
    import hashlib
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@app.post("/api/profile")
async def api_save_profile(data: ProfileInput, request: Request):
    session = await _session(request, csrf=True, rate_limit=True)
    profile = _validated_profile(data)
    envelope = _profile_crypto().encrypt(profile, owner_id=session["telegram_id"])
    await save_profile_envelope(session["telegram_id"], envelope)
    return {"ok": True, "profile_masked": _mask_profile(profile)}


@app.delete("/api/profile")
async def api_delete_profile(request: Request, response: Response):
    session = await _session(request, csrf=True, rate_limit=True)
    _rate_limit(f"delete-user:{session['telegram_id']}", 4)
    deleted = await delete_passenger_profile(session["telegram_id"])
    response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict")
    return {"ok": True, "deleted": deleted, "backup_notice": "Удалено из рабочей базы. Ранее созданные резервные копии могут храниться до окончания срока их хранения."}


@app.post("/api/booking/confirm")
async def api_confirm_booking(data: BookingConfirmation, request: Request):
    session = await _session(request, csrf=True, rate_limit=True)
    _rate_limit(f"confirm-user:{session['telegram_id']}", 4)
    if session["purpose"] != "booking" or not data.confirmed:
        raise HTTPException(422, "Нужно явно подтвердить пассажира, контакты, рейс и цену")
    if session.get("checkout_state") == "SUCCEEDED" and session.get("result_encrypted"):
        return _profile_crypto().decrypt(
            session["result_encrypted"], owner_id=session["telegram_id"], purpose="checkout-result"
        )
    if session.get("checkout_state") in {"INVOKING", "UNKNOWN"}:
        raise HTTPException(409, "Исход предыдущего запроса не определён. Автоматический повтор заблокирован; проверьте email и обратитесь в поддержку.")
    loaded = await _load_profile(session["telegram_id"])
    if not loaded:
        raise HTTPException(409, "Сначала сохраните профиль пассажира")
    profile, profile_version = loaded
    reviewed = _validated_profile(data.reviewed_profile)
    if _flatten_profile(reviewed) != _flatten_profile(profile):
        raise HTTPException(409, "Полные данные отличаются от профиля. Исправьте профиль и подтвердите заново.")
    snapshot = _booking_snapshot(profile, session["payload"])
    snapshot_hash = _profile_crypto().fingerprint(snapshot, owner_id=session["telegram_id"], purpose="booking-snapshot")
    started = await begin_booking_invocation(
        session["launch_id"], session["telegram_id"], data.confirmation_token,
        snapshot_hash, profile_version,
    )
    if not started:
        raise HTTPException(409, "Подтверждение устарело или данные изменились. Перечитайте сведения заново.")
    try:
        result = await tutu_mcp.register_checkout_passengers(
            passengers=[profile["passenger"]],
            customer=profile["customer"],
            avia_offer=snapshot["offer"],
        )
        pay_url = _validate_payment_url(str(result.get("pay_url") or ""))
        safe_result = {
            "ok": True,
            "pay_url": pay_url,
            "expires_at": result.get("pay_url_expires_at"),
            "notice": "Ссылка одноразовая. Не пересылайте её и открывайте самостоятельно.",
        }
        encrypted = _profile_crypto().encrypt(
            safe_result, owner_id=session["telegram_id"], purpose="checkout-result"
        )
        await finish_booking(session["launch_id"], "SUCCEEDED", encrypted)
        return safe_result
    except Exception:
        await finish_booking(session["launch_id"], "UNKNOWN")
        logger.error("Sensitive booking result is unknown for launch_id=%s", session["launch_id"])
        raise HTTPException(
            502,
            "Не удалось однозначно определить результат Туту. Повтор заблокирован, чтобы не создать дубль. Проверьте email и обратитесь в поддержку.",
        ) from None


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "bot.passenger_web:app",
        host="127.0.0.1",
        port=int(os.getenv("PASSENGER_WEB_PORT", "8790")),
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        access_log=False,
        log_level="warning",
    )
