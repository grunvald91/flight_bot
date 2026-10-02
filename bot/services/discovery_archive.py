"""Restart-safe flight results and minimal provider link refs (no passenger data).

The user chooses when to open a provider link; only the provider can decide if
the fare still exists. No bot-imposed age limit or automatic re-search.
"""

import json
import os
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import time
from typing import Callable

_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DB = Path(__file__).resolve().parents[2] / "private_cache" / "discovery.sqlite3"
_AVIA_REF_FIELDS = (
    "transport", "product_type", "search_results_url", "offer_hash",
    "departure_geo_city_id", "arrival_geo_city_id", "departure_avia_id",
    "arrival_avia_id", "departure_at", "service_class", "passengers_full",
    "passengers_child", "passengers_infant", "is_round_trip",
    "return_departure_at", "is_multi_pnr",
)
_PART_FIELDS = (
    "offer_hash", "departure_geo_city_id", "arrival_geo_city_id",
    "departure_at", "service_class", "from", "to", "flights",
    "passengers_full", "passengers_child", "passengers_infant",
)
_SUMMARY_FIELDS = (
    "from", "to", "departure_at", "arrival_at", "price_amount",
    "price_currency", "service_class",
)
_SEGMENT_FIELDS = ("from", "to", "departure_at", "arrival_at", "flight", "carrier")
_SENSITIVE_KEYS = frozenset(("passport", "passport_number", "document", "document_number",
                             "birth_date", "first_name", "last_name", "email", "phone",
                             "contact", "passenger_name", "passenger_data"))
_NESTED_FLIGHT_FIELDS = frozenset(("name", "city", "code", "iata", "id", "airport",
                                   "from", "to", "flights", "flight", "number",
                                   "voyage_no", "carrier", "airline", "departure_at",
                                   "arrival_at", "offer_hash", "service_class"))


def _redact_sensitive(value):
    if isinstance(value, dict):
        return {key: _redact_sensitive(item) for key, item in value.items()
                if isinstance(key, str) and key.lower() not in _SENSITIVE_KEYS}
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value if isinstance(value, (str, int, float, bool, type(None))) else None


def _flight_value(value):
    if isinstance(value, dict):
        return {key: _flight_value(item) for key, item in value.items()
                if key in _NESTED_FLIGHT_FIELDS}
    if isinstance(value, list):
        return [_flight_value(item) for item in value]
    return value if isinstance(value, (str, int, float, bool, type(None))) else None


def _scalar(value):
    return value if isinstance(value, (str, int, float, bool, type(None))) else None


def _link_ref(ref: dict) -> dict:
    if not isinstance(ref, dict):
        return {}
    result = {name: _redact_sensitive(ref[name]) for name in _AVIA_REF_FIELDS if name in ref}
    if isinstance(ref.get("checkout_parts"), list):
        result["checkout_parts"] = [
            {name: (_flight_value(part[name]) if name in ("from", "to", "flights")
                    else _redact_sensitive(part[name]))
             for name in _PART_FIELDS if name in part}
            for part in ref["checkout_parts"] if isinstance(part, dict)
        ]
    return result


def _offer_summary(summary: dict) -> dict:
    if not isinstance(summary, dict):
        return {}
    result = {name: _scalar(summary[name]) for name in _SUMMARY_FIELDS if name in summary}
    result["segments"] = [
        {name: _scalar(segment[name]) for name in _SEGMENT_FIELDS if name in segment}
        for segment in summary.get("segments", []) if isinstance(segment, dict)
    ]
    return result


def can_open_result(payload: dict, callback) -> bool:
    """Scope private searches to their owner; legacy display-only rows remain readable."""
    meta = payload.get("meta") or {}
    owner = meta.get("owner_id")
    chat = meta.get("chat_id")
    user = getattr(getattr(callback, "from_user", None), "id", None)
    chat_now = getattr(getattr(getattr(callback, "message", None), "chat", None), "id", None)
    if owner is not None:
        return user == owner or (chat is not None and user == chat == chat_now)
    if chat is not None:
        return chat_now == chat
    return not any(offer.get("checkout_ref") for offer in payload.get("offers") or [])


def _display_offer(offer: dict, summary: dict | None = None) -> dict:
    """Persist visible fields and narrowly allowlisted non-PII provider link data."""
    price = offer.get("price") or {}
    discovery = offer.get("_discovery") or {}
    review = offer.get("review_summary") or {}
    result = {
        "offer_id": offer.get("offer_id"),
        "_source_index": offer.get("_source_index"),
        "price": {"amount": price.get("amount"), "currency": price.get("currency")},
        "_discovery": {"city": discovery.get("city"), "country": discovery.get("country")},
        "search_results_url": _scalar(offer.get("search_results_url")),
        "review_summary": {"label": review.get("label")},
        "departure_at": offer.get("departure_at"),
        "arrival_at": offer.get("arrival_at"),
        "duration_min": offer.get("duration_min"),
        "carriers": [str(value) for value in (offer.get("carriers") or [])],
        "checkout_ref": _link_ref(offer.get("checkout_ref") or {}),
        "booking_summary": _offer_summary(summary or {}),
    }
    result["legs"] = [{
        "from": _flight_value(leg.get("from")),
        "to": _flight_value(leg.get("to")),
        "segments": [{"voyage_no": segment.get("voyage_no")}
                     for segment in (leg.get("segments") or []) if isinstance(segment, dict)],
    } for leg in (offer.get("legs") or []) if isinstance(leg, dict)]
    result["variants"] = []
    for variant in (offer.get("variants") or []):
        if not isinstance(variant, dict):
            continue
        conditions = variant.get("conditions") or {}
        baggage = conditions.get("baggage") or {}
        cabin = conditions.get("cabin_baggage") or {}
        result["variants"].append({"conditions": {
            "fare_family": conditions.get("fare_family"),
            "refundable": conditions.get("refundable"),
            "changeable": conditions.get("changeable"),
            "baggage": {name: baggage.get(name) for name in ("pieces", "kg")},
            "cabin_baggage": {name: cabin.get(name) for name in ("pieces", "kg", "dimensions")},
        }})
    return result


class DiscoveryArchive:
    def __init__(self, path: Path = _DB, *, ttl_seconds: int = 86400,
                 clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.ttl_seconds = ttl_seconds  # legacy constructor argument; not used to expire results
        self.clock = clock

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute("CREATE TABLE IF NOT EXISTS search_archive ("
                     "key TEXT PRIMARY KEY, expires_at REAL NOT NULL, payload TEXT NOT NULL)")
        return conn

    def put(self, key: str, payload: dict) -> None:
        if not _KEY.fullmatch(key):
            raise ValueError("Invalid archive key")
        summaries = payload.get("booking_summaries") or []
        display = {
            "offers": [_display_offer(offer, summaries[index] if index < len(summaries) else None)
                       for index, offer in enumerate(payload.get("offers", []))],
            "meta": {name: (payload.get("meta") or {}).get(name)
                     for name in ("origin", "departure_date", "unverified", "owner_id", "chat_id")},
        }
        encoded = json.dumps(display, ensure_ascii=False, allow_nan=False)
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("INSERT OR REPLACE INTO search_archive (key, expires_at, payload) VALUES (?, ?, ?)",
                             (key, 0, encoded))

    def put_browse(self, key: str, payload: dict) -> None:
        """Store only a scoped, display-only snapshot of indicative destinations."""
        if not _KEY.fullmatch(key):
            raise ValueError("Invalid archive key")
        meta = payload.get("meta") or {}
        if type(meta.get("owner_id")) is not int or type(meta.get("chat_id")) is not int:
            raise ValueError("Browse results require owner and chat scope")
        fields = ("origin", "destination", "city", "country", "price",
                  "departure_at", "transfers", "airline", "flight_number", "duration")
        display = {
            "options": [{name: _scalar(row[name]) for name in fields if name in row}
                        for row in payload.get("options", []) if isinstance(row, dict)],
            "meta": {"kind": "browse", **{name: _scalar(meta.get(name)) for name in
                     ("owner_id", "chat_id", "origin", "departure_date",
                      "unresolved", "source_rows")}},
        }
        encoded = json.dumps(display, ensure_ascii=False, allow_nan=False)
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("INSERT OR REPLACE INTO search_archive (key, expires_at, payload) VALUES (?, ?, ?)",
                             (key, 0, encoded))

    def get(self, key: str) -> dict | None:
        if not _KEY.fullmatch(key):
            return None
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT payload FROM search_archive WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None


discovery_archive = DiscoveryArchive()
