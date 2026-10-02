from datetime import datetime, date
from sqlalchemy import (
    BigInteger, String, Integer, Float, Boolean, DateTime, Date,
    Text, JSON, LargeBinary, ForeignKey, UniqueConstraint, Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    search_history: Mapped[list["SearchHistory"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    favorites: Mapped[list["Favorite"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    subscriptions: Mapped[list["PriceSubscription"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    settings: Mapped["UserSettings"] = relationship(back_populates="user", uselist=False, cascade="all, delete-orphan")


class SearchHistory(Base):
    __tablename__ = "search_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    origin: Mapped[str] = mapped_column(String(10), nullable=False)
    destination: Mapped[str | None] = mapped_column(String(10), nullable=True)
    depart_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    return_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    search_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "route" | "cheapest"
    results_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="search_history")


class Favorite(Base):
    __tablename__ = "favorites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    origin: Mapped[str] = mapped_column(String(10), nullable=False)
    destination: Mapped[str] = mapped_column(String(10), nullable=False)
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    airline: Mapped[str | None] = mapped_column(String(100), nullable=True)
    link: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="favorites")

    __table_args__ = (
        UniqueConstraint("user_id", "origin", "destination", name="uq_user_route"),
    )


class PriceCache(Base):
    __tablename__ = "price_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cache_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    response_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PriceSubscription(Base):
    __tablename__ = "price_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    origin: Mapped[str] = mapped_column(String(10), nullable=False)
    destination: Mapped[str] = mapped_column(String(10), nullable=False)
    depart_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    return_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    max_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_price_drop: Mapped[int | None] = mapped_column(Integer, nullable=True)  # мин. снижение для уведомления
    last_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="subscriptions")


class RequestLog(Base):
    __tablename__ = "request_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_type: Mapped[str] = mapped_column(String(50), nullable=False)  # "command" | "inline" | "callback" | "message"
    request_data: Mapped[str | None] = mapped_column(Text, nullable=True)  # текст запроса / callback_data
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class UserSettings(Base):
    __tablename__ = "user_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)

    # Фильтры рейсов
    direct_flights_only: Mapped[bool] = mapped_column(Boolean, default=False)
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Настройки уведомлений
    min_price_drop: Mapped[int | None] = mapped_column(Integer, nullable=True)  # переопределяет глобальный MIN_PRICE_DROP

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="settings")


class PassengerProfile(Base):
    """Encrypted passenger data. No personal field is stored in plaintext."""

    __tablename__ = "passenger_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    encrypted_payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Nullable only for pre-migration rows; crypto refuses legacy PP2 envelopes.
    profile_id: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    wrapped_dek: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    kek_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    profile_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class PassengerWebLaunch(Base):
    """Short-lived, single-use bridge from a Telegram button to the HTTPS form."""

    __tablename__ = "passenger_web_launches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)
    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    session_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True, index=True)
    csrf_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    checkout_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    checkout_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_profile_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmation_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    booking_attempted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    booking_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    result_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class TelegramInitDataReplay(Base):
    __tablename__ = "telegram_init_data_replays"

    init_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


Index(
    "uq_passenger_checkout_provider_attempt",
    PassengerWebLaunch.telegram_id,
    PassengerWebLaunch.checkout_hash,
    unique=True,
    postgresql_where=PassengerWebLaunch.checkout_state.in_(["INVOKING", "UNKNOWN", "SUCCEEDED"]),
)
