import json
import logging
import re
from pathlib import Path

import aiohttp

from bot.config import TRAVELPAYOUTS_BASE_URL

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent.parent / "data"
DASH_TRANSLATION = str.maketrans({
    "‐": "-",
    "‑": "-",
    "‒": "-",
    "–": "-",
    "—": "-",
    "−": "-",
    "﹘": "-",
    "﹣": "-",
    "－": "-",
})
TRIM_CHARS = " \t\n\r,.;:!?()[]{}<>\"'`"


class CityResolver:
    def __init__(self):
        self.cities: dict[str, str] = {}
        self.cities_en: dict[str, str] = {}
        self.iata_to_name: dict[str, str] = {}
        self.iata_to_country: dict[str, str] = {}
        self.airports: dict[str, str] = {}
        self.airlines: dict[str, str] = {}
        self.aliases: dict[str, str] = {}

    async def load(self):
        """Load reference data from local cache first, then API."""
        cities_file = DATA_DIR / "cities.json"
        airlines_file = DATA_DIR / "airlines.json"

        if cities_file.exists():
            await self._load_from_file(cities_file, "cities")
        else:
            await self._fetch_and_save("cities")

        if airlines_file.exists():
            await self._load_from_file(airlines_file, "airlines")
        else:
            await self._fetch_and_save("airlines")

    async def _load_from_file(self, filepath: Path, data_type: str):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._index_data(data, data_type)
            logger.info("Loaded %s from cache: %s", data_type, filepath)
        except Exception as e:
            logger.error("Failed to load %s from file: %s", data_type, e)
            await self._fetch_and_save(data_type)

    async def _fetch_and_save(self, data_type: str):
        url_map = {
            "cities": f"{TRAVELPAYOUTS_BASE_URL}/data/ru/cities.json",
            "airlines": f"{TRAVELPAYOUTS_BASE_URL}/data/ru/airlines.json",
        }
        url = url_map.get(data_type)
        if not url:
            return

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        DATA_DIR.mkdir(parents=True, exist_ok=True)
                        filepath = DATA_DIR / f"{data_type}.json"
                        with open(filepath, "w", encoding="utf-8") as f:
                            json.dump(data, f, ensure_ascii=False, indent=2)
                        self._index_data(data, data_type)
                        logger.info("Fetched and saved %s: %d items", data_type, len(data))
                    else:
                        logger.error("Failed to fetch %s: HTTP %d", data_type, resp.status)
        except Exception as e:
            logger.error("Failed to fetch %s: %s", data_type, e)

    @staticmethod
    def _normalize_query(query: str) -> str:
        if not query:
            return ""
        normalized = query.translate(DASH_TRANSLATION).replace("ё", "е").replace("Ё", "Е")
        normalized = normalized.strip().lower().strip(TRIM_CHARS)
        normalized = re.sub(r"\s+", " ", normalized)
        normalized = re.sub(r"\s*-\s*", "-", normalized)
        return normalized

    def _index_name(self, mapping: dict[str, str], name: str, code: str):
        normalized = self._normalize_query(name)
        if not normalized:
            return

        mapping[normalized] = code
        if "-" in normalized:
            mapping.setdefault(normalized.replace("-", " "), code)
        if " " in normalized:
            mapping.setdefault(normalized.replace(" ", "-"), code)

    def _refresh_aliases(self):
        self.aliases.clear()
        alias_to_name = {
            "спб": "санкт-петербург",
            "питер": "санкт-петербург",
            "санкт петербург": "санкт-петербург",
            "петербург": "санкт-петербург",
            "мск": "москва",
            "екб": "екатеринбург",
        }

        for alias, canonical in alias_to_name.items():
            alias_key = self._normalize_query(alias)
            canonical_key = self._normalize_query(canonical)
            code = self.cities.get(canonical_key) or self.cities_en.get(canonical_key)
            if code:
                self.aliases[alias_key] = code

    def _index_data(self, data: list[dict], data_type: str):
        if data_type == "cities":
            for city in data:
                code = (city.get("code") or "").strip().upper()
                if not code:
                    continue

                name_ru = city.get("name", "")
                name_en = city.get("name_translations", {}).get("en", "")
                country_code = (city.get("country_code") or "").strip().upper()

                if name_ru:
                    self._index_name(self.cities, name_ru, code)
                    self.iata_to_name[code] = name_ru
                if name_en:
                    self._index_name(self.cities_en, name_en, code)
                if country_code:
                    self.iata_to_country[code] = country_code

            self._refresh_aliases()

        elif data_type == "airlines":
            for airline in data:
                code = (airline.get("code") or "").strip().upper()
                name = airline.get("name_translations", {}).get("ru") or airline.get("name", "")
                if code and name:
                    self.airlines[code] = name

    def resolve(self, query: str) -> str | None:
        """Resolve city/airport query into IATA code."""
        if not query:
            return None

        raw = query.strip()
        if not raw:
            return None

        upper_query = raw.upper()
        if 2 <= len(upper_query) <= 3 and upper_query.isalpha() and upper_query.isascii():
            return upper_query if upper_query in self.iata_to_name else None

        explicit_iata = re.search(r"\b([A-Za-z]{3})\b", raw)
        if explicit_iata:
            code = explicit_iata.group(1).upper()
            if code in self.iata_to_name:
                return code

        normalized = self._normalize_query(raw)
        if not normalized:
            return None

        if normalized in self.aliases:
            return self.aliases[normalized]
        if normalized in self.cities:
            return self.cities[normalized]
        if normalized in self.cities_en:
            return self.cities_en[normalized]

        for mapping in (self.cities, self.cities_en):
            for name, code in mapping.items():
                if name.startswith(normalized):
                    return code

        for mapping in (self.cities, self.cities_en):
            for name, code in mapping.items():
                if f" {normalized}" in name or f"{normalized} " in name:
                    return code

        return None

    def get_city_name(self, iata: str) -> str:
        """Get Russian city name by IATA code."""
        return self.iata_to_name.get(iata, iata)

    def get_airline_name(self, code: str) -> str:
        """Get airline name by IATA code."""
        return self.airlines.get(code, code)

    def get_country_code(self, iata: str) -> str | None:
        """Get ISO 3166-1 alpha-2 country code for a city IATA code."""
        return self.iata_to_country.get((iata or "").upper())

    async def update(self):
        """Refresh reference data from API."""
        await self._fetch_and_save("cities")
        await self._fetch_and_save("airlines")
        logger.info("Reference data updated")


city_resolver = CityResolver()
