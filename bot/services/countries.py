"""Разбор названия страны из текста пользователя («Турция», «ОАЭ», «Turkey»)."""
from babel import Locale

from bot.services.city_resolver import city_resolver
from bot.services.international_discovery import SPECIAL_COUNTRIES_RU, country_name_ru

_RU = Locale.parse("ru")
_EN = Locale.parse("en")

# Названия, которыми страну называют в разговоре, но которых нет в справочнике
_ALIASES = {
    "оаэ": "AE", "эмираты": "AE", "арабские эмираты": "AE",
    "сша": "US", "америка": "US", "штаты": "US", "соединенные штаты": "US",
    "великобритания": "GB", "англия": "GB", "британия": "GB", "uk": "GB",
    "корея": "KR", "южная корея": "KR", "республика корея": "KR",
    "голландия": "NL", "нидерланды": "NL",
    "чехия": "CZ", "молдавия": "MD", "белоруссия": "BY", "беларусь": "BY",
    "киргизия": "KG", "кыргызстан": "KG", "лаос": "LA", "вьетнам": "VN",
    "бирма": "MM", "мьянма": "MM", "турция": "TR", "россия": "RU",
    "абхазия": "AB", "косово": "XK", "таиланд": "TH", "тайланд": "TH",
    "доминикана": "DO", "гонконг": "HK", "макао": "MO", "палестина": "PS", "македония": "MK",
    "северная корея": "KP", "кндр": "KP", "объединенные арабские эмираты": "AE",
    "босния": "BA", "босния и герцеговина": "BA", "юар": "ZA", "южная африка": "ZA",
    "сейшелы": "SC", "сейшельские острова": "SC", "республика конго": "CG",
    "др конго": "CD", "демократическая республика конго": "CD", "turkey": "TR", "uae": "AE", "usa": "US", "america": "US", "кипр": "CY", "мальдивы": "MV", "шри ланка": "LK",
}

_names: dict[str, str] | None = None


def _normalize(text: str) -> str:
    return " ".join(
        "".join(ch if ch.isalnum() else " " for ch in (text or "").lower().replace("ё", "е")).split()
    )


def _index() -> dict[str, str]:
    global _names
    if _names is None:
        index: dict[str, str] = {}
        for locale in (_RU, _EN):
            for code, name in locale.territories.items():
                if len(code) == 2 and code.isalpha() and code.isupper():
                    index.setdefault(_normalize(name), code)
        for code, name in SPECIAL_COUNTRIES_RU.items():
            index[_normalize(name)] = code
        for name, code in _ALIASES.items():
            index[_normalize(name)] = code
        _names = index
    return _names


def cities_in_country(country_code: str) -> list[str]:
    """IATA-коды городов страны из справочника."""
    code = (country_code or "").upper()
    return [iata for iata, country in city_resolver.iata_to_country.items() if country == code]


def resolve_country(text: str) -> str | None:
    """Код страны по её названию или None, если это не страна (или в ней нет аэропортов)."""
    code = _index().get(_normalize(text))
    if code and cities_in_country(code):
        return code
    return None


def country_label(country_code: str) -> str:
    return country_name_ru(country_code)
