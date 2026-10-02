import hashlib
import json
import secrets
from datetime import datetime, timedelta

from sqlalchemy import delete, exists, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from database.database import async_session_maker, engine
from database.models import PassengerProfile, PassengerWebLaunch, RequestLog, TelegramInitDataReplay, User
from bot.services.passenger_crypto import ProfileCrypto, SCHEMA_VERSION, _decode
from bot.services.privacy import request_log_identity


async def ensure_checkout_unique_index() -> None:
    """Migrate pre-existing PostgreSQL tables; create_all does not add new indexes."""
    async with engine.begin() as conn:
        if conn.dialect.name != "postgresql":
            raise RuntimeError("Protected checkout requires PostgreSQL")
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_passenger_checkout_provider_attempt "
            "ON passenger_web_launches (telegram_id, checkout_hash) "
            "WHERE checkout_state IN ('INVOKING', 'UNKNOWN', 'SUCCEEDED')"
        ))
        # Existing rows remain unreadable until an explicit, separately approved
        # PP2-to-PP3 migration. No implicit plaintext fallback or overwrite.
        for definition in ("profile_id VARCHAR(32)", "wrapped_dek BYTEA", "kek_id VARCHAR(64)"):
            await conn.execute(text(f"ALTER TABLE passenger_profiles ADD COLUMN IF NOT EXISTS {definition}"))
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_passenger_profiles_profile_id "
            "ON passenger_profiles (profile_id)"
        ))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _checkout_hash(payload: dict | None) -> str | None:
    if not payload or not payload.get("checkout_ref"):
        return None
    # result_id/card_id are search-session handles and can change for the same flight.
    # The provider attempt lock must survive repeated searches and fresh launch links.
    ref = payload["checkout_ref"]
    stable = (
        "offer_hash", "departure_geo_city_id", "arrival_geo_city_id", "departure_at",
        "return_departure_at", "service_class", "passengers_full", "passengers_child", "passengers_infant",
    )
    normalized = {key: ref[key] for key in stable if key in ref}
    normalized.setdefault("passengers_full", 1)
    normalized.setdefault("passengers_child", 0)
    normalized.setdefault("passengers_infant", 0)
    normalized.setdefault("service_class", "Y")
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _hash(canonical)


async def create_web_launch(
    telegram_id: int,
    purpose: str,
    payload: dict | None = None,
    *,
    ttl_minutes: int = 10,
) -> str:
    if purpose not in {"profile", "booking"}:
        raise ValueError("Unsupported web launch purpose")
    raw_token = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    checkout_hash = _checkout_hash(payload)
    async with async_session_maker() as session:
        if checkout_hash:
            unresolved = await session.scalar(
                select(PassengerWebLaunch.id).where(
                    PassengerWebLaunch.telegram_id == telegram_id,
                    PassengerWebLaunch.checkout_hash == checkout_hash,
                    PassengerWebLaunch.checkout_state.in_(["INVOKING", "UNKNOWN", "SUCCEEDED"]),
                ).limit(1)
            )
            if unresolved:
                raise RuntimeError("Previous booking outcome is unresolved")
        session.add(PassengerWebLaunch(
            token_hash=_hash(raw_token),
            telegram_id=telegram_id,
            purpose=purpose,
            payload_json=payload,
            checkout_hash=checkout_hash,
            checkout_state="DRAFT" if purpose == "booking" else None,
            expires_at=now + timedelta(minutes=ttl_minutes),
        ))
        await session.execute(delete(TelegramInitDataReplay).where(TelegramInitDataReplay.expires_at < now))
        await session.execute(
            delete(PassengerWebLaunch).where(
                PassengerWebLaunch.expires_at < now - timedelta(days=1),
                or_(
                    PassengerWebLaunch.checkout_state.is_(None),
                    PassengerWebLaunch.checkout_state.in_(["DRAFT", "READY", "FAILED_DEFINITE"]),
                ),
            )
        )
        await session.commit()
    return raw_token


async def exchange_web_launch(
    raw_token: str,
    telegram_id: int,
    init_data_hash: str,
    raw_session_id: str,
    raw_csrf: str,
    *,
    session_minutes: int = 15,
) -> dict | None:
    now = datetime.utcnow()
    try:
        async with async_session_maker() as session:
            session.add(TelegramInitDataReplay(
                init_hash=init_data_hash,
                expires_at=now + timedelta(minutes=10),
            ))
            result = await session.execute(
                update(PassengerWebLaunch)
                .where(
                    PassengerWebLaunch.token_hash == _hash(raw_token),
                    PassengerWebLaunch.telegram_id == telegram_id,
                    PassengerWebLaunch.consumed_at.is_(None),
                    PassengerWebLaunch.expires_at > now,
                )
                .values(
                    consumed_at=now,
                    session_hash=_hash(raw_session_id),
                    csrf_hash=_hash(raw_csrf),
                    session_expires_at=now + timedelta(minutes=session_minutes),
                )
                .returning(
                    PassengerWebLaunch.id,
                    PassengerWebLaunch.telegram_id,
                    PassengerWebLaunch.purpose,
                    PassengerWebLaunch.payload_json,
                )
            )
            row = result.first()
            if not row:
                await session.rollback()
                return None
            await session.commit()
            return {"launch_id": row[0], "telegram_id": row[1], "purpose": row[2], "payload": row[3] or {}}
    except IntegrityError:
        return None


async def authenticate_web_session(raw_session_id: str, raw_csrf: str | None = None) -> dict | None:
    now = datetime.utcnow()
    async with async_session_maker() as session:
        result = await session.execute(
            select(PassengerWebLaunch).where(
                PassengerWebLaunch.session_hash == _hash(raw_session_id),
                PassengerWebLaunch.session_expires_at > now,
            )
        )
        launch = result.scalar_one_or_none()
        if not launch or (raw_csrf is not None and launch.csrf_hash != _hash(raw_csrf)):
            return None
        return {
            "launch_id": launch.id,
            "telegram_id": launch.telegram_id,
            "purpose": launch.purpose,
            "payload": launch.payload_json or {},
            "checkout_state": launch.checkout_state,
            "result_encrypted": bytes(launch.result_encrypted) if launch.result_encrypted else None,
        }


async def rotate_session_csrf(raw_session_id: str, raw_csrf: str) -> bool:
    now = datetime.utcnow()
    async with async_session_maker() as session:
        result = await session.execute(
            update(PassengerWebLaunch)
            .where(
                PassengerWebLaunch.session_hash == _hash(raw_session_id),
                PassengerWebLaunch.session_expires_at > now,
            )
            .values(csrf_hash=_hash(raw_csrf))
        )
        await session.commit()
        return bool(result.rowcount)


async def get_profile_envelope(telegram_id: int) -> tuple[bytes, int, datetime] | None:
    async with async_session_maker() as session:
        result = await session.execute(
            select(PassengerProfile.encrypted_payload, PassengerProfile.profile_version,
                   PassengerProfile.updated_at, PassengerProfile.profile_id,
                   PassengerProfile.wrapped_dek, PassengerProfile.kek_id, PassengerProfile.key_version)
            .join(User, PassengerProfile.user_id == User.id)
            .where(User.telegram_id == telegram_id)
        )
        row = result.first()
        if not row:
            return None
        data = ProfileCrypto.metadata(bytes(row[0]))
        if (row[3] != data["profile_id"] or bytes(row[4] or b"") != _decode(data["wrapped_dek"])
                or row[5] != data["kek_id"] or row[6] != SCHEMA_VERSION):
            raise ValueError("Passenger profile metadata mismatch; refusing access")
        return bytes(row[0]), int(row[1]), row[2]


async def has_passenger_profile(telegram_id: int) -> bool:
    return await get_profile_envelope(telegram_id) is not None


async def save_profile_envelope(telegram_id: int, envelope: bytes) -> int:
    metadata = ProfileCrypto.metadata(envelope)
    if metadata["purpose"] != "passenger-profile":
        raise ValueError("Not a passenger profile envelope")
    now = datetime.utcnow()
    async with async_session_maker() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if not user:
            user = User(telegram_id=telegram_id)
            session.add(user)
            await session.flush()
        result = await session.execute(
            select(PassengerProfile).where(PassengerProfile.user_id == user.id).with_for_update()
        )
        profile = result.scalar_one_or_none()
        if profile:
            # Legacy PP2 data must be explicitly migrated, never silently replaced.
            ProfileCrypto.metadata(bytes(profile.encrypted_payload))
            profile.encrypted_payload = envelope
            profile.profile_id = metadata["profile_id"]
            profile.wrapped_dek = _decode(metadata["wrapped_dek"])
            profile.kek_id = metadata["kek_id"]
            profile.key_version = SCHEMA_VERSION
            profile.profile_version += 1
            profile.updated_at = now
        else:
            profile = PassengerProfile(
                user_id=user.id,
                encrypted_payload=envelope,
                profile_id=metadata["profile_id"],
                wrapped_dek=_decode(metadata["wrapped_dek"]),
                kek_id=metadata["kek_id"],
                key_version=SCHEMA_VERSION,
                profile_version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(profile)
        await session.commit()
        return profile.profile_version


async def rotate_profile_kek(telegram_id: int, crypto: ProfileCrypto) -> bool:
    """Rewrap one owner profile atomically; old KEK stays configured until rotation completes."""
    async with async_session_maker() as session:
        profile = await session.scalar(
            select(PassengerProfile).join(User, PassengerProfile.user_id == User.id)
            .where(User.telegram_id == telegram_id).with_for_update()
        )
        if profile is None:
            return False
        old = bytes(profile.encrypted_payload)
        data = ProfileCrypto.metadata(old)
        if (profile.profile_id != data["profile_id"] or profile.kek_id != data["kek_id"]
                or bytes(profile.wrapped_dek or b"") != _decode(data["wrapped_dek"])):
            raise ValueError("Passenger profile metadata mismatch; refusing rotation")
        crypto.decrypt(old, owner_id=telegram_id)  # Verify every field before changing any key material.
        rotated = crypto.rewrap(old, owner_id=telegram_id)
        new = ProfileCrypto.metadata(rotated)
        profile.encrypted_payload = rotated
        profile.wrapped_dek = _decode(new["wrapped_dek"])
        profile.kek_id = new["kek_id"]
        # Not a passenger edit: existing confirmation snapshot remains the same.
        await session.commit()
        return True


async def delete_passenger_profile(telegram_id: int) -> bool:
    async with async_session_maker() as session:
        user_id = await session.scalar(select(User.id).where(User.telegram_id == telegram_id))
        deleted = False
        if user_id is not None:
            result = await session.execute(delete(PassengerProfile).where(PassengerProfile.user_id == user_id))
            deleted = bool(result.rowcount)
        await session.execute(
            update(PassengerWebLaunch)
            .where(PassengerWebLaunch.telegram_id == telegram_id)
            .values(session_hash=None, csrf_hash=None, session_expires_at=None, result_encrypted=None)
        )
        await session.execute(delete(RequestLog).where(
            RequestLog.telegram_id.in_([telegram_id, request_log_identity(telegram_id)])))
        await session.commit()
        return deleted


async def prepare_booking(
    launch_id: int,
    telegram_id: int,
    snapshot_hash: str,
    profile_version: int,
    confirmation_hash: str,
) -> bool:
    now = datetime.utcnow()
    async with async_session_maker() as session:
        result = await session.execute(
            update(PassengerWebLaunch)
            .where(
                PassengerWebLaunch.id == launch_id,
                PassengerWebLaunch.telegram_id == telegram_id,
                PassengerWebLaunch.purpose == "booking",
                PassengerWebLaunch.expires_at > now,
                PassengerWebLaunch.checkout_state.in_(["DRAFT", "READY"]),
            )
            .values(
                checkout_state="READY",
                snapshot_hash=snapshot_hash,
                confirmed_profile_version=profile_version,
                confirmation_hash=confirmation_hash,
            )
        )
        await session.commit()
        return bool(result.rowcount)


async def begin_booking_invocation(
    launch_id: int,
    telegram_id: int,
    confirmation_token: str,
    snapshot_hash: str,
    profile_version: int,
) -> bool:
    now = datetime.utcnow()
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                update(PassengerWebLaunch)
                .where(
                    PassengerWebLaunch.id == launch_id,
                    PassengerWebLaunch.telegram_id == telegram_id,
                    PassengerWebLaunch.purpose == "booking",
                    PassengerWebLaunch.expires_at > now,
                    PassengerWebLaunch.checkout_state == "READY",
                    PassengerWebLaunch.confirmation_hash == _hash(confirmation_token),
                    PassengerWebLaunch.snapshot_hash == snapshot_hash,
                    PassengerWebLaunch.confirmed_profile_version == profile_version,
                    exists(
                        select(1).select_from(PassengerProfile)
                        .join(User, User.id == PassengerProfile.user_id)
                        .where(User.telegram_id == telegram_id,
                               PassengerProfile.profile_version == profile_version)
                    ),
                )
                .values(
                    checkout_state="INVOKING",
                    booking_attempted_at=now,
                    booking_status="attempting",
                    confirmation_hash=None,
                )
            )
            await session.commit()
            return bool(result.rowcount)
    except IntegrityError:
        # A different launch for the same offer is already being processed.
        return False


async def finish_booking(launch_id: int, state: str, result_encrypted: bytes | None = None) -> None:
    if state not in {"SUCCEEDED", "UNKNOWN", "FAILED_DEFINITE"}:
        raise ValueError("Invalid checkout terminal state")
    async with async_session_maker() as session:
        await session.execute(
            update(PassengerWebLaunch)
            .where(PassengerWebLaunch.id == launch_id, PassengerWebLaunch.checkout_state == "INVOKING")
            .values(
                checkout_state=state,
                booking_status=state.lower(),
                result_encrypted=result_encrypted,
            )
        )
        await session.commit()


async def purge_expired_web_data() -> None:
    """Bound retention even when no user opens the Mini App."""
    now = datetime.utcnow()
    async with async_session_maker() as session:
        await session.execute(delete(TelegramInitDataReplay).where(TelegramInitDataReplay.expires_at < now))
        await session.execute(
            update(PassengerWebLaunch)
            .where(PassengerWebLaunch.created_at < now - timedelta(hours=2))
            .values(result_encrypted=None, session_hash=None, csrf_hash=None, session_expires_at=None)
        )
        await session.execute(
            delete(PassengerWebLaunch).where(
                PassengerWebLaunch.created_at < now - timedelta(days=7),
                or_(
                    PassengerWebLaunch.checkout_state.is_(None),
                    PassengerWebLaunch.checkout_state.in_(["DRAFT", "READY", "FAILED_DEFINITE"]),
                ),
            )
        )
        await session.commit()
