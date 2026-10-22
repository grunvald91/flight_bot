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
BUNDLED_DATA_PATH = ROOT_DIR / "data" / "kdmid_entry_rules.csv"
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
    "HK": ["Гонконг", "Специальный административный район Гонконг, Китай"],
    "MO": ["Макао", "Специальный административный район Макао, Китай"],
    "PS": ["Палестина", "Государство Палестина", "Палестинские территории"],
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
    # Заполняется только у вручную проверенных стран: готовая строка про срок
    # и итоговый вердикт, чтобы не зависеть от разбора текста открытой базы.
    stay: str = ""
    verdict: str = ""
    verified: bool = False


VERIFIED_COUNTRY_RULES: dict[str, EntryRule] = {
    "TR": EntryRule(
        country="Турция",
        ordinary_passport=(
            "Виза не нужна. До 60 дней за одну поездку; всего не более 90 дней "
            "в течение 180 дней."
        ),
        stay=(
            "до 60 дней подряд, всего до 90 дней в течение 180 дней. "
            "Чтобы использовать остаток, нужно выехать из страны и въехать снова"
        ),
        details=(
            "Документы: загранпаспорт, действителен не менее 120 дней с даты "
            "въезда (рекомендуют 6 месяцев).\n"
            "Важно: за превышение срока штраф и возможный запрет на въезд."
        ),
        source_url="https://www.mfa.gov.tr/consular-info.ru.mfa",
        extra_source_url="https://t-j.ru/turkey-entry-rules/",
        updated_at="2026-10-23",
        verified=True,
    ),
    "TH": EntryRule(
        country="Таиланд",
        ordinary_passport="Виза не нужна. До 30 дней (с 15 сентября 2026 года, раньше было 60).",
        stay=(
            "до 30 дней; можно продлить ещё на 30 дней в иммиграционной "
            "службе Таиланда"
        ),
        details=(
            "Документы: загранпаспорт, действителен не менее 6 месяцев; "
            "электронная карта прибытия TDAC, заполняется онлайн до вылета."
        ),
        source_url="https://tdac.immigration.go.th/",
        extra_source_url="https://zona.media/news/2026/08/31/thai",
        updated_at="2026-10-23",
        verified=True,
    ),
    "AE": EntryRule(
        country="ОАЭ",
        ordinary_passport="Виза не нужна. До 90 дней в течение 180 дней.",
        stay="до 90 дней в течение 180 дней",
        details=(
            "Документы: загранпаспорт, действителен не менее 6 месяцев с даты "
            "въезда. У каждого ребёнка должен быть свой загранпаспорт."
        ),
        source_url="https://icp.gov.ae/",
        extra_source_url="https://t-j.ru/uae-entry-rules/",
        updated_at="2026-10-23",
        verified=True,
    ),
    "EG": EntryRule(
        country="Египет",
        ordinary_passport="Виза оформляется по прибытии (платная) или онлайн как электронная виза.",
        verdict="🟡 <b>Нужна виза: по прибытии или электронная</b>",
        stay=(
            "до 30 дней. На курортах Синая (Шарм-эль-Шейх, Таба) можно без визы "
            "до 15 дней, но нельзя выезжать за пределы Синая"
        ),
        details=(
            "Документы: загранпаспорт, действителен не менее 6 месяцев; "
            "миграционная карта.\n"
            "Важно: визу по прибытии оплачивают наличными долларами до паспортного "
            "контроля; электронная виза оформляется на visa2egypt.gov.eg."
        ),
        source_url="https://visa2egypt.gov.eg/",
        extra_source_url="https://otello.ru/nomer/pravila-vuezda-v-egipet",
        updated_at="2026-10-23",
        verified=True,
    ),
    "ME": EntryRule(
        country="Черногория",
        ordinary_passport=(
            "Безвиз для россиян действует до 31 октября 2026 года включительно. "
            "С 1 ноября 2026 года нужна виза."
        ),
        verdict="🔴 <b>С 1 ноября 2026 года нужна виза</b>",
        stay="до 31 октября 2026 года можно без визы (до 30 дней)",
        details=(
            "Важно: при поездке после 31 октября 2026 года визу нужно "
            "оформить заранее."
        ),
        extra_source_url="https://www.kommersant.ru/doc/8845057",
        updated_at="2026-10-23",
        verified=True,
    ),
    "VN": EntryRule(
        country="Вьетнам",
        ordinary_passport="Виза не нужна. До 45 дней.",
        stay="до 45 дней; на больший срок можно оформить электронную визу (до 90 дней)",
        details=(
            "Документы: загранпаспорт, действителен не менее 6 месяцев; "
            "на границе могут попросить обратный билет и бронь жилья."
        ),
        source_url="https://evisa.immigration.gov.vn/",
        extra_source_url="https://t-j.ru/vietnam-entry-rules/",
        updated_at="2026-10-23",
        verified=True,
    ),
    "CN": EntryRule(
        country="Китай",
        ordinary_passport="Виза не нужна. До 30 дней (безвиз продлён до 31 декабря 2027 года).",
        stay="до 30 дней; безвизовый режим действует до 31 декабря 2027 года",
        details=(
            "Документы: загранпаспорт. Безвиз для туризма, деловых поездок, "
            "визита к родным и транзита."
        ),
        extra_source_url="https://meduza.io/news/2026/05/20/kitay-prodlil-bezvizovyy-rezhim-dlya-rossiyan-do-kontsa-2027-goda",
        updated_at="2026-10-23",
        verified=True,
    ),
    "GE": EntryRule(
        country="Грузия",
        ordinary_passport="Виза не нужна. До 365 дней подряд.",
        stay="до 365 дней подряд",
        details=(
            "Документы: загранпаспорт, действителен на весь срок поездки.\n"
            "Важно: по сообщениям туроператоров, с 1 января 2026 года нужна "
            "медицинская страховка (от 30 000 $), оформите её заранее. "
            "С отметками о въезде в Абхазию или Южную Осетию через территорию "
            "России могут отказать во въезде."
        ),
        extra_source_url="https://profile.ru/news/abroad/medstrahovka-stanet-obyazatelnoj-dlya-vezda-v-gruziju-1788328/amp/",
        updated_at="2026-10-23",
        verified=True,
    ),
    "AM": EntryRule(
        country="Армения",
        ordinary_passport="Виза не нужна. До 180 дней в течение года.",
        stay="до 180 дней в течение года",
        details=(
            "Документы: при перелёте из России подойдёт внутренний паспорт или "
            "загранпаспорт; по суше (например, через Грузию) нужен загранпаспорт."
        ),
        source_url="https://www.mfa.am/ru",
        extra_source_url="https://t-j.ru/armenia-entry-rules/",
        updated_at="2026-10-23",
        verified=True,
    ),
    "AZ": EntryRule(
        country="Азербайджан",
        ordinary_passport="Виза не нужна. До 90 дней в течение календарного года.",
        stay=(
            "до 90 дней в течение календарного года. Схема «выехать и вернуться», "
            "чтобы обнулить срок, не работает"
        ),
        details=(
            "Документы: загранпаспорт, действителен на весь срок поездки.\n"
            "Важно: при пребывании дольше 15 дней нужна временная регистрация."
        ),
        extra_source_url="https://t-j.ru/azerbaijan-entry-rules/",
        updated_at="2026-10-23",
        verified=True,
    ),
    "RS": EntryRule(
        country="Сербия",
        ordinary_passport="Виза не нужна. До 30 дней за одну поездку.",
        stay="до 30 дней за одну поездку, число поездок не ограничено",
        details=(
            "Документы: загранпаспорт (посольство рекомендует срок не менее "
            "90 дней после вылета).\n"
            "Важно: иностранцы регистрируются в течение 24 часов после прибытия "
            "(отель делает это сам); на границе могут спросить обратный билет, "
            "жильё, средства и страховку. Следите за новостями: обсуждался "
            "возможный переход на визы."
        ),
        extra_source_url="https://otello.ru/nomer/vezd-serbiyu",
        updated_at="2026-10-23",
        verified=True,
    ),
    "ID": EntryRule(
        country="Индонезия",
        ordinary_passport="Виза оформляется по прибытии (платная), на 30 дней.",
        stay=(
            "до 30 дней, один раз можно продлить ещё на 30 дней. "
            "Для более долгого срока нужна виза C1 (60 дней)"
        ),
        details=(
            "Документы: загранпаспорт, действителен не менее 6 месяцев; "
            "электронная декларация All Indonesia с QR-кодом.\n"
            "Важно: на Бали туристический сбор 150 000 IDR, платится один раз "
            "за поездку."
        ),
        source_url="https://allindonesia.imigrasi.go.id/",
        extra_source_url="https://t-j.ru/indonesia-entry-rules/",
        updated_at="2026-10-23",
        verified=True,
    ),
    "MV": EntryRule(
        country="Мальдивы",
        ordinary_passport="Виза не нужна. По соглашению до 90 дней.",
        stay=(
            "до 90 дней по соглашению; при въезде обычно ставят 30 дней, "
            "дальше продлевается онлайн через IMUGA"
        ),
        details=(
            "Документы: загранпаспорт, действителен не менее 1 месяца с даты "
            "въезда; электронная декларация туриста на imuga.immigration.gov.mv "
            "за 96 часов до прибытия; обратный билет и оплаченное жильё."
        ),
        source_url="https://imuga.immigration.gov.mv/",
        extra_source_url="https://t-j.ru/maldives-entry-rules/",
        updated_at="2026-10-23",
        verified=True,
    ),
    "KG": EntryRule(
        country="Киргизия",
        ordinary_passport="Виза не нужна. До 30 дней без регистрации, всего до 90 дней в течение 180 дней.",
        stay=(
            "до 30 дней без регистрации; всего не более 90 дней в течение "
            "180 дней"
        ),
        details=(
            "Документы: взрослым достаточно внутреннего паспорта; детям до "
            "14 лет нужен загранпаспорт.\n"
            "Важно: при пребывании дольше 30 дней нужна регистрация."
        ),
        extra_source_url="https://t-j.ru/kirgizia-entry-rules/",
        updated_at="2026-10-23",
        verified=True,
    ),
    "MA": EntryRule(
        country="Марокко",
        ordinary_passport="Виза не нужна. До 90 дней.",
        stay="до 90 дней",
        details=(
            "Документы: загранпаспорт, действителен не менее 6 месяцев после "
            "выезда из Марокко.\n"
            "Важно: срок в 90 дней подтверждён только вторичными источниками, "
            "официальных данных МИД нет."
        ),
        extra_source_url="https://t-j.ru/news/morocco-entry-rules/",
        updated_at="2026-10-23",
        verified=True,
    ),
    "TN": EntryRule(
        country="Тунис",
        ordinary_passport="Виза не нужна. До 90 дней.",
        stay="до 90 дней",
        details=(
            "Документы: загранпаспорт; на границе могут попросить обратный "
            "билет, подтверждение жилья и страховку."
        ),
        extra_source_url="https://travelto.ru/blog/skolko-mozhno-nakhoditsya-v-tunise/",
        updated_at="2026-10-23",
        verified=True,
    ),
    "QA": EntryRule(
        country="Катар",
        ordinary_passport="Виза не нужна. До 90 дней в течение 180 дней.",
        stay="до 90 дней в течение 180 дней",
        details=(
            "Документы: загранпаспорт, действителен не менее 6 месяцев.\n"
            "Важно: при пребывании дольше 30 дней нужна медицинская страховка."
        ),
        extra_source_url="https://t-j.ru/qatar-entry-rules/",
        updated_at="2026-10-23",
        verified=True,
    ),
    "JO": EntryRule(
        country="Иордания",
        ordinary_passport=(
            "Виза не нужна (безвиз с 13 декабря 2025 года). До 30 дней за одну "
            "поездку, не более 90 дней в году."
        ),
        stay="до 30 дней за одну поездку, всего не более 90 дней в течение года",
        details="Важно: безвиз только для туризма, деловых и частных визитов.",
        extra_source_url="https://media.halvacard.ru/news/iordaniia-vvodit-bezviz",
        updated_at="2026-10-23",
        verified=True,
    ),
    "LK": EntryRule(
        country="Шри-Ланка",
        ordinary_passport="Нужно электронное разрешение ETA на 30 дней.",
        verdict="🟡 <b>Нужно электронное разрешение (ETA)</b>",
        stay="до 30 дней; продление платное через иммиграционную службу",
        details=(
            "Документы: загранпаспорт, действителен не менее 6 месяцев; "
            "ETA оформляется на eta.gov.lk до вылета.\n"
            "Важно: по данным на февраль 2026 для россиян ETA бесплатное, "
            "но сборы там меняются, сверьтесь на сайте."
        ),
        source_url="https://eta.gov.lk/",
        extra_source_url="https://t-j.ru/sri-lanka-entry-rules/",
        updated_at="2026-10-23",
        verified=True,
    ),
    "CU": EntryRule(
        country="Куба",
        ordinary_passport="Виза не нужна. До 90 дней в течение 180 дней.",
        stay="до 90 дней в течение 180 дней; можно продлить ещё на 90 дней",
        details=(
            "Документы: загранпаспорт, действителен не менее 6 месяцев; "
            "обязательная туристическая страховка; электронная форма "
            "D'Viajeros за 48 часов до вылета.\n"
            "Важно: прямые рейсы из России приостановлены с февраля 2026 года, "
            "Минэкономразвития советовало воздержаться от поездок."
        ),
        source_url="https://dviajeros.mitrans.gob.cu/",
        extra_source_url="https://t-j.ru/news/cuba-entry-rules/",
        updated_at="2026-10-23",
        verified=True,
    ),
    "KZ": EntryRule(
        country="Казахстан",
        ordinary_passport=(
            "Виза не нужна. До 90 дней; не более 90 дней в каждом 180-дневном периоде."
        ),
        details=(
            "Документы: внутренний российский паспорт или загранпаспорт.\n"
            "Уведомление: гостиница или хозяин жилья уведомляет о прибытии "
            "в течение 3 рабочих дней."
        ),
        source_url=(
            "https://www.gov.kz/memleket/entities/mfa/activities/34747"
            "?lang=ru&parentId=3053"
        ),
        extra_source_url=(
            "https://www.gov.kz/situations/497/1149?hl=ru-RU&lang=ru"
        ),
        updated_at="2026-10-02",
        verified=True,
    ),
}


_memory_rules: list[dict] | None = None
_memory_loaded_at: datetime | None = None


def _normalize(value: str) -> str:
    value = value.lower().replace("ё", "е")
    value = re.sub(r"[^а-яa-z0-9]+", " ", value, flags=re.IGNORECASE)
    return " ".join(value.split())


def _decode_csv_bytes(raw: bytes) -> str:
    errors = []
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError as exc:
            errors.append(exc)
            continue
        normalized = text.lower()
        first_line = normalized.splitlines()[0] if normalized.splitlines() else ""
        if (
            ("country" in first_line and "citizen" in first_line)
            or ("страна" in normalized and "общеграждан" in normalized)
        ):
            return text
    if errors:
        raise errors[-1]
    return raw.decode("utf-8-sig")


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
        (
            key
            for key in rows[0].keys()
            if _normalize(str(key)) == "country"
            or "страна" in _normalize(str(key))
        ),
        None,
    )
    passport_key = next(
        (
            key
            for key in rows[0].keys()
            if _normalize(str(key)) == "citizen"
            or "общеграждан" in _normalize(str(key))
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


def _load_bundled_rules() -> tuple[list[dict], datetime | None]:
    try:
        if not BUNDLED_DATA_PATH.exists():
            return [], None
        raw = BUNDLED_DATA_PATH.read_bytes()
        text = _decode_csv_bytes(raw)
        rules = _parse_csv(text)
        modified_at = datetime.fromtimestamp(
            BUNDLED_DATA_PATH.stat().st_mtime,
            tz=timezone.utc,
        )
        return rules, modified_at
    except Exception:
        logger.exception("Could not read bundled KDMID entry-rules snapshot")
        return [], None


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
    text = _decode_csv_bytes(raw)
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

        bundled_rules, bundled_at = _load_bundled_rules()
        if bundled_rules:
            _memory_rules = bundled_rules
            _memory_loaded_at = bundled_at or now
            bundled_is_stale = bool(
                bundled_at is None or now - bundled_at > timedelta(days=7)
            )
            return bundled_rules, bundled_at, bundled_is_stale
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
