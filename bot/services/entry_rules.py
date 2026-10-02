import csv
import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

KDMID_DATA_URL = (
    "https://www.kdmid.ru/opendata/7704206201-country/"
    "data-20230119-structure-20200514.csv"
)
KDMID_SOURCE_URL = "https://www.kdmid.ru/opendata/7704206201-country/"
KDMID_VIEW_URL = "https://www.kdmid.ru/opendata/7704206201-country/doc/view.php"

ROOT_DIR = Path(__file__).resolve().parents[2]
CACHE_PATH = ROOT_DIR / "private_cache" / "kdmid_entry_rules.json"
CACHE_TTL = timedelta(hours=12)

COUNTRY_ALIASES = {
    "AE": ["ОАЭ", "Объединенные Арабские Эмираты", "Объединённые Арабские Эмираты"],
    "US": ["США", "Соединенные Штаты Америки", "Соединённые Штаты Америки"],
    "DE": ["ФРГ", "Германия"],
    "GB": ["Великобритания", "Соединенное Королевство", "Соединённое Королевство"],
    "KR": ["Республика Корея", "Южная Корея"],
    "KP": ["КНДР", "Корейская Народно-Демократическая Республика"],
    "CZ": ["Чехия"],
    "MD": ["Молдавия", "Молдова"],
    "MK": ["Северная Македония"],
    "TW": ["Тайвань, Китай", "Тайвань"],
    "CF": ["ЦАР", "Центральноафриканская Республика"],
    "CD": ["ДР Конго", "Демократическая Республика Конго"],
    "CG": ["Республика Конго", "Конго"],
    "LA": ["Лаос", "Лаосская Народно-Демократическая Республика"],
    "VN": ["Вьетнам"],
    "TR": ["Турция"],
    "CN": ["Китай"],
    "TH": ["Таиланд"],
    "IN": ["Индия"],
    "ID": ["Индонезия"],
    "MV": ["Мальдивы", "Мальдивская Республика"],
    "LK": ["Шри-Ланка"],
    "RS": ["Сербия"],
    "ME": ["Черногория"],
    "GE": ["Грузия"],
    "AM": ["Армения"],
    "AZ": ["Азербайджан"],
    "BY": ["Белоруссия", "Беларусь"],
    "KZ": ["Казахстан"],
    "KG": ["Киргизия", "Кыргызстан"],
    "UZ": ["Узбекистан"],
    "TJ": ["Таджикистан"],
}


@dataclass
class EntryRule:
    country: str
    ordinary_passport: str
    details: str = ""
    source_url: str = KDMID_SOURCE_URL
    extra_source_url: str | None = None
    stale: bool = False
    updated_at: str | None = None


VERIFIED_COUNTRY_RULES: dict[str, EntryRule] = {
    "KZ": EntryRule(
        country="Казахстан",
        ordinary_passport=(
            "Виза не нужна. Для граждан Российской Федерации разрешённое пребывание "
            "без оформления разрешения на временное проживание — до 90 календарных дней; "
            "для граждан государств ЕАЭС действует общий предел 90 дней в каждом "
            "180-дневном периоде."
        ),
        details=(
            "Документы: граждане РФ могут въезжать по внутреннему российскому паспорту "
            "или загранпаспорту.\n"
            "Уведомление: принимающая сторона (например, гостиница, хозяин жилья) должна "
            "уведомить миграционную службу о прибытии иностранца в течение 3 рабочих дней.\n"
            "Если планируется пребывание дольше разрешённого срока, работа, учёба или иное "
            "долгосрочное основание, требуется оформить соответствующий статус/РВП."
        ),
        source_url=(
            "https://www.gov.kz/memleket/entities/mfa/activities/34747"
            "?lang=ru&parentId=3053"
        ),
        extra_source_url=(
            "https://www.gov.kz/situations/497/1149?hl=ru-RU&lang=ru"
        ),
        updated_at="2026-10-02",
    ),
}


_memory_rules: list[dict] | None = None
_memory_loaded_at: datetime | None = None


def _normalize(value: str) -> str:
    value = value.lower().replace("ё", "е")
    value = re.sub(r"[^а-яa-z0-9]+", " ", value, flags=re.IGNORECASE)
    return " ".join(value.split())


def _parse_csv(text: str) -> list[dict]:
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";"

    rows = list(csv.DictReader(io.StringIO(text), delimiter=delimiter))
    if not rows:
        return []

    country_key = next(
        (key for key in rows[0].keys() if "страна" in _normalize(str(key))),
        None,
    )
    passport_key = next(
        (
            key
            for key in rows[0].keys()
            if "общеграждан" in _normalize(str(key))
        ),
        None,
    )
    if not country_key or not passport_key:
        raise ValueError("Unexpected KDMID CSV structure")

    result = []
    for row in rows:
        country = str(row.get(country_key) or "").strip()
        ordinary = str(row.get(passport_key) or "").strip()
        if country and ordinary:
            result.append({"country": country, "ordinary_passport": ordinary})
    return result


def _save_cache(rules: list[dict], fetched_at: datetime) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "fetched_at": fetched_at.isoformat(),
                    "rules": rules,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(CACHE_PATH)
    except Exception:
        logger.exception("Could not save KDMID entry-rules cache")


def _load_disk_cache() -> tuple[list[dict], datetime | None]:
    try:
        if not CACHE_PATH.exists():
            return [], None
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        rules = payload.get("rules") or []
        fetched_at_raw = payload.get("fetched_at")
        fetched_at = (
            datetime.fromisoformat(fetched_at_raw)
            if fetched_at_raw
            else None
        )
        if fetched_at and fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return rules, fetched_at
    except Exception:
        logger.exception("Could not read KDMID entry-rules cache")
        return [], None


async def _download_rules() -> tuple[list[dict], datetime]:
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            KDMID_DATA_URL,
            headers={"User-Agent": "A_TicketBot/1.0"},
        ) as response:
            response.raise_for_status()
            raw = await response.read()
    text = raw.decode("utf-8-sig")
    rules = _parse_csv(text)
    if not rules:
        raise ValueError("KDMID entry-rules dataset is empty")
    fetched_at = datetime.now(timezone.utc)
    _save_cache(rules, fetched_at)
    return rules, fetched_at


async def get_rules() -> tuple[list[dict], datetime | None, bool]:
    global _memory_rules, _memory_loaded_at

    now = datetime.now(timezone.utc)
    if (
        _memory_rules is not None
        and _memory_loaded_at is not None
        and now - _memory_loaded_at < CACHE_TTL
    ):
        return _memory_rules, _memory_loaded_at, False

    disk_rules, disk_at = _load_disk_cache()
    if disk_rules and disk_at and now - disk_at < CACHE_TTL:
        _memory_rules = disk_rules
        _memory_loaded_at = disk_at
        return disk_rules, disk_at, False

    try:
        rules, fetched_at = await _download_rules()
        _memory_rules = rules
        _memory_loaded_at = fetched_at
        return rules, fetched_at, False
    except Exception as exc:
        logger.warning("Could not refresh KDMID entry rules: %s", exc)
        if disk_rules:
            _memory_rules = disk_rules
            _memory_loaded_at = disk_at or now
            return disk_rules, disk_at, True
        raise


def _candidate_names(country_name: str, country_code: str | None) -> list[str]:
    names = []
    if country_code:
        names.extend(COUNTRY_ALIASES.get(country_code.upper(), []))
    if country_name:
        names.append(country_name)
    return [_normalize(name) for name in names if name]


async def find_entry_rule(
    country_name: str,
    country_code: str | None = None,
) -> EntryRule | None:
    code = (country_code or "").upper()
    verified = VERIFIED_COUNTRY_RULES.get(code)
    if verified is not None:
        return verified

    rules, fetched_at, stale = await get_rules()
    candidates = _candidate_names(country_name, country_code)
    if not candidates:
        return None

    normalized_rows = [
        (_normalize(str(row.get("country") or "")), row)
        for row in rules
    ]

    for candidate in candidates:
        for normalized_country, row in normalized_rows:
            if candidate == normalized_country:
                return EntryRule(
                    country=str(row["country"]),
                    ordinary_passport=str(row["ordinary_passport"]),
                    stale=stale,
                    updated_at=fetched_at.isoformat() if fetched_at else None,
                )

    for candidate in candidates:
        for normalized_country, row in normalized_rows:
            if (
                len(candidate) >= 5
                and (
                    candidate in normalized_country
                    or normalized_country in candidate
                )
            ):
                return EntryRule(
                    country=str(row["country"]),
                    ordinary_passport=str(row["ordinary_passport"]),
                    stale=stale,
                    updated_at=fetched_at.isoformat() if fetched_at else None,
                )
    return None
