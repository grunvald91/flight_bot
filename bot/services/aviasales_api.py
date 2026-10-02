import logging
from datetime import datetime, timedelta

import aiohttp

from bot.config import TRAVELPAYOUTS_TOKEN, TRAVELPAYOUTS_BASE_URL, CACHE_TTL_MINUTES, AFFILIATE_MARKER
from database.database import async_session_maker
from database.models import PriceCache
from sqlalchemy import select, delete

logger = logging.getLogger(__name__)


class AviasalesAPI:
    def __init__(self):
        self.base_url = TRAVELPAYOUTS_BASE_URL
        self.token = TRAVELPAYOUTS_TOKEN
        self.headers = {
            "X-Access-Token": self.token,
            "Accept-Encoding": "gzip",
        }

    async def _request(self, endpoint: str, params: dict = None) -> dict | list | None:
        url = f"{self.base_url}{endpoint}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        logger.error("API error %d: %s - %s", resp.status, endpoint, await resp.text())
                        return None
        except Exception as e:
            logger.error("API request failed: %s - %s", endpoint, str(e))
            return None

    async def _cached_request(self, cache_key: str, endpoint: str, params: dict = None) -> dict | list | None:
        async with async_session_maker() as session:
            result = await session.execute(
                select(PriceCache).where(
                    PriceCache.cache_key == cache_key,
                    PriceCache.expires_at > datetime.utcnow(),
                )
            )
            cached = result.scalar_one_or_none()
            if cached:
                logger.debug("Cache hit: %s", cache_key)
                return cached.response_json

        data = await self._request(endpoint, params)
        if data is not None:
            async with async_session_maker() as session:
                old = await session.execute(
                    select(PriceCache).where(PriceCache.cache_key == cache_key)
                )
                existing = old.scalar_one_or_none()
                if existing:
                    existing.response_json = data
                    existing.created_at = datetime.utcnow()
                    existing.expires_at = datetime.utcnow() + timedelta(minutes=CACHE_TTL_MINUTES)
                else:
                    session.add(PriceCache(
                        cache_key=cache_key,
                        response_json=data,
                        created_at=datetime.utcnow(),
                        expires_at=datetime.utcnow() + timedelta(minutes=CACHE_TTL_MINUTES),
                    ))
                await session.commit()

        return data

    async def search_prices_for_dates(
        self,
        origin: str,
        destination: str,
        depart_date: str | None = None,
        return_date: str | None = None,
        one_way: bool = True,
        direct: bool = False,
        limit: int = 50,
        unique: bool = False,
        sorting: str = "price",
    ) -> list[dict]:
        """Поиск цен на конкретные даты. Основной эндпоинт для inline-поиска по маршруту."""
        params = {
            "origin": origin,
            "destination": destination,
            "currency": "rub",
            "sorting": sorting,
            "direct": str(direct).lower(),
            "one_way": str(one_way).lower(),
            "unique": str(unique).lower(),
            "limit": limit,
            "token": self.token,
        }
        if depart_date:
            params["departure_at"] = depart_date
        if return_date:
            params["return_at"] = return_date

        cache_key = f"prices_v2:{origin}-{destination}:{depart_date or 'any'}:{return_date or 'none'}:{direct}:{unique}:{sorting}"
        data = await self._cached_request(cache_key, "/aviasales/v3/prices_for_dates", params)

        if data and isinstance(data, dict) and data.get("success"):
            return data.get("data", [])
        return []

    async def search_destinations_on_date(
        self,
        origin: str,
        depart_date: str,
        limit: int = 1000,
    ) -> list[dict]:
        """All reported candidate destinations for an exact date, page by page."""
        if not 1 <= limit <= 1000:
            raise ValueError("Aviasales page size must be between 1 and 1000")
        params = {
            "origin": origin,
            "currency": "rub",
            "sorting": "price",
            "direct": "false",
            "one_way": "true",
            "unique": "true",
            "limit": limit,
            "departure_at": depart_date,
            "token": self.token,
        }
        results = []
        previous_full_page = None
        seen_destinations = set()
        page = 1
        while True:
            params["page"] = page
            cache_key = f"destinations:{origin}:{depart_date}:{limit}:{page}"
            data = await self._cached_request(
                cache_key, "/aviasales/v3/prices_for_dates", params,
            )
            if not isinstance(data, dict) or data.get("success") is not True or not isinstance(data.get("data"), list):
                raise RuntimeError(f"Incomplete destination discovery: page {page} failed")
            currency = data.get("currency")
            if currency is not None and str(currency).lower() != "rub":
                raise RuntimeError(f"Incomplete destination discovery: page {page} currency mismatch")
            rows = data["data"]
            if len(rows) > limit:
                raise RuntimeError(f"Incomplete destination discovery: page {page} exceeds requested size")
            if rows and rows == previous_full_page:
                raise RuntimeError(f"Incomplete destination discovery: repeated page {page}")
            page_destinations = set()
            for row in rows:
                destination = row.get("destination") if isinstance(row, dict) else None
                if not destination:
                    raise RuntimeError(f"Incomplete destination discovery: invalid row on page {page}")
                if destination in seen_destinations or destination in page_destinations:
                    raise RuntimeError(f"Incomplete destination discovery: page {page} overlap")
                page_destinations.add(destination)
            seen_destinations.update(page_destinations)
            results.extend(rows)
            if not rows:
                metadata = data.get("pagination") or data.get("meta") or {}
                if not isinstance(metadata, dict):
                    raise RuntimeError(f"Incomplete destination discovery: page {page} invalid metadata")
                if any(item.get("has_more") is True or item.get("next_page") not in (None, False, 0, "")
                       for item in (data, metadata)):
                    raise RuntimeError(f"Incomplete destination discovery: page {page} indicates more rows")
                for item in (data, metadata):
                    for key in ("total", "total_count"):
                        if key in item:
                            try:
                                total = int(item[key])
                            except (TypeError, ValueError):
                                raise RuntimeError(f"Incomplete destination discovery: page {page} invalid total")
                            if total != len(results):
                                raise RuntimeError(f"Incomplete destination discovery: page {page} total mismatch")
                return results
            previous_full_page = rows
            page += 1

    async def get_cheapest_tickets(self, origin: str, destination: str | None = None) -> dict:
        """Самые дешёвые билеты по маршруту (данные за 48ч)."""
        params = {
            "origin": origin,
            "currency": "rub",
            "token": self.token,
        }
        if destination:
            params["destination"] = destination

        cache_key = f"cheap:{origin}-{destination or 'any'}"
        data = await self._cached_request(cache_key, "/v1/prices/cheap", params)

        if data and isinstance(data, dict) and data.get("success"):
            return data.get("data", {})
        return {}

    async def get_city_directions(self, origin: str) -> dict:
        """Популярные направления из города с ценами."""
        params = {
            "origin": origin,
            "currency": "rub",
            "token": self.token,
        }

        cache_key = f"directions:{origin}"
        data = await self._cached_request(cache_key, "/v1/city-directions", params)

        if data and isinstance(data, dict) and data.get("success"):
            return data.get("data", {})
        return {}

    async def get_latest_prices(
        self,
        origin: str,
        destination: str | None = None,
        one_way: bool = True,
        limit: int = 30,
        sorting: str = "price",
    ) -> list[dict]:
        """Последние найденные цены с сортировкой."""
        params = {
            "origin": origin,
            "currency": "rub",
            "one_way": str(one_way).lower(),
            "limit": limit,
            "sorting": sorting,
            "token": self.token,
        }
        if destination:
            params["destination"] = destination

        cache_key = f"latest:{origin}-{destination or 'any'}:{sorting}"
        data = await self._cached_request(cache_key, "/v2/prices/latest", params)

        if data and isinstance(data, dict) and data.get("success"):
            return data.get("data", [])
        return []

    async def get_direct_prices(self, origin: str, destination: str) -> dict:
        """Только прямые рейсы."""
        params = {
            "origin": origin,
            "destination": destination,
            "currency": "rub",
            "token": self.token,
        }

        cache_key = f"direct:{origin}-{destination}"
        data = await self._cached_request(cache_key, "/v1/prices/direct", params)

        if data and isinstance(data, dict) and data.get("success"):
            return data.get("data", {})
        return {}

    async def _get_cheap_as_list(self, origin: str, destination: str, depart_date: str | None = None) -> list[dict]:
        """Получает билеты из /v1/prices/cheap и возвращает как плоский список."""
        params = {
            "origin": origin,
            "destination": destination,
            "currency": "rub",
            "token": self.token,
        }
        if depart_date:
            params["depart_date"] = depart_date

        cache_key = f"cheap_list:{origin}-{destination}:{depart_date or 'any'}"
        data = await self._cached_request(cache_key, "/v1/prices/cheap", params)

        result = []
        if data and isinstance(data, dict) and data.get("success"):
            for dest_code, flights in data.get("data", {}).items():
                if isinstance(flights, dict):
                    for key, ticket in flights.items():
                        if isinstance(ticket, dict):
                            ticket.setdefault("origin", origin)
                            ticket.setdefault("destination", dest_code)
                            result.append(ticket)
        return result

    async def search_all_options(
        self,
        origin: str,
        destination: str,
        depart_date: str | None = None,
        return_date: str | None = None,
    ) -> list[dict]:
        """
        Собирает билеты из нескольких источников и возвращает
        уникальные варианты. Показывает ВСЕ рейсы — разные авиакомпании, время, пересадки.
        Запрашивает с unique=true и unique=false, sorting=price и sorting=route
        для максимального покрытия вариантов.
        """
        import asyncio as _aio

        one_way = return_date is None

        # Индексы источников, которые гарантированно прямые
        DIRECT_INDICES = {1, 2, 3, 4}
        # Индексы, где нужна нормализация полей (latest prices, cheap)
        NORMALIZE_INDICES = {5, 6}

        # Запускаем все запросы параллельно для скорости
        tasks = [
            # 0. Все билеты unique=false, sorting=price
            self.search_prices_for_dates(
                origin=origin, destination=destination,
                depart_date=depart_date, return_date=return_date,
                one_way=one_way, direct=False, limit=50,
                unique=False, sorting="price",
            ),
            # 1. Прямые рейсы unique=false, sorting=price
            self.search_prices_for_dates(
                origin=origin, destination=destination,
                depart_date=depart_date, return_date=return_date,
                one_way=one_way, direct=True, limit=30,
                unique=False, sorting="price",
            ),
            # 2. Прямые рейсы unique=true, sorting=price (другие варианты времени)
            self.search_prices_for_dates(
                origin=origin, destination=destination,
                depart_date=depart_date, return_date=return_date,
                one_way=one_way, direct=True, limit=30,
                unique=True, sorting="price",
            ),
            # 3. Прямые рейсы unique=false, sorting=route
            self.search_prices_for_dates(
                origin=origin, destination=destination,
                depart_date=depart_date, return_date=return_date,
                one_way=one_way, direct=True, limit=30,
                unique=False, sorting="route",
            ),
            # 4. Прямые рейсы unique=true, sorting=route
            self.search_prices_for_dates(
                origin=origin, destination=destination,
                depart_date=depart_date, return_date=return_date,
                one_way=one_way, direct=True, limit=30,
                unique=True, sorting="route",
            ),
            # 5. Последние найденные цены (другой источник)
            self.get_latest_prices(
                origin=origin, destination=destination,
                sorting="price", limit=30,
            ),
            # 6. Дешёвые билеты (/v1/prices/cheap) — ещё один источник
            self._get_cheap_as_list(origin, destination, depart_date),
        ]

        results = await _aio.gather(*tasks, return_exceptions=True)

        all_tickets = []

        # Обрабатываем результаты
        for idx, res in enumerate(results):
            if isinstance(res, Exception):
                logger.error("search_all_options task %d failed: %s", idx, res)
                continue
            if not isinstance(res, list):
                continue

            for t in res:
                # Прямые запросы — помечаем transfers=0
                if idx in DIRECT_INDICES:
                    t["transfers"] = 0
                # latest prices / cheap — нормализуем поля
                if idx in NORMALIZE_INDICES:
                    if "value" in t and "price" not in t:
                        t["price"] = t["value"]
                    if "depart_date" in t and "departure_at" not in t:
                        t["departure_at"] = t["depart_date"]
                    if "return_date" in t and "return_at" not in t:
                        t["return_at"] = t["return_date"]
                    if "number_of_changes" in t and "transfers" not in t:
                        t["transfers"] = t["number_of_changes"]

            all_tickets.extend(res)

        # Дедупликация по (цена + время вылета + авиакомпания)
        seen = set()
        unique = []
        for t in all_tickets:
            price = t.get("price") or t.get("value", 0)
            dep = (t.get("departure_at") or t.get("depart_date", ""))[:16]
            airline = t.get("airline", "")
            key = (price, dep, airline)
            if key not in seen:
                seen.add(key)
                t.setdefault("origin", origin)
                t.setdefault("destination", destination)
                t.setdefault("transfers", 0)
                t.setdefault("price", t.get("value", 0))
                unique.append(t)

        # Фильтруем по дате если указана
        if depart_date:
            filtered = [t for t in unique
                        if (t.get("departure_at") or t.get("depart_date", "")).startswith(depart_date)]
            if filtered:
                unique = filtered

        # Сортировка: сначала прямые рейсы, потом по времени вылета
        def sort_key(t):
            transfers = t.get("transfers", 99)
            dep = t.get("departure_at") or t.get("depart_date", "")
            return (transfers, dep)
        unique.sort(key=sort_key)

        return unique

    def build_ticket_link(self, ticket: dict, origin: str, destination: str, depart_date: str | None = None, return_date: str | None = None) -> str:
        """
        Генерация ссылки на билет.
        """
        depart_part = ""
        dep = depart_date or (ticket.get("departure_at") or ticket.get("depart_date", ""))[:10]
        if dep and len(dep) >= 10:
            try:
                day = dep[8:10]
                month = dep[5:7]
                depart_part = f"{day}{month}"
            except (IndexError, ValueError):
                pass

        return_part = ""
        ret = return_date or (ticket.get("return_at") or ticket.get("return_date", ""))[:10]
        if ret and len(ret) >= 10:
            try:
                day = ret[8:10]
                month = ret[5:7]
                return_part = f"{day}{month}"
            except (IndexError, ValueError):
                pass

        link = f"https://www.aviasales.ru/search/{origin}{depart_part}{destination}{return_part}1"
        if AFFILIATE_MARKER:
            link += f"?marker={AFFILIATE_MARKER}"
        return link

    def build_search_link(self, origin: str, destination: str, depart_date: str | None = None, return_date: str | None = None) -> str:
        """Генерация общей ссылки на поиск (без конкретного билета). Поддерживает туда-обратно."""
        depart_part = ""
        if depart_date and len(depart_date) >= 10:
            day = depart_date[8:10]
            month = depart_date[5:7]
            depart_part = f"{day}{month}"

        return_part = ""
        if return_date and len(return_date) >= 10:
            day = return_date[8:10]
            month = return_date[5:7]
            return_part = f"{day}{month}"

        link = f"https://www.aviasales.ru/search/{origin}{depart_part}{destination}{return_part}1"
        if AFFILIATE_MARKER:
            link += f"?marker={AFFILIATE_MARKER}"
        return link

    async def search_direct_on_date(self, origin: str, destination: str, depart_date: str) -> list[dict]:
        """Все прямые рейсы на конкретную дату, дедуплицированные и отсортированные по времени."""
        import asyncio as _aio

        # Запрашиваем с разными параметрами для максимального покрытия
        tasks = [
            self.search_prices_for_dates(
                origin=origin, destination=destination, depart_date=depart_date,
                direct=True, limit=50, unique=False, sorting="price",
            ),
            self.search_prices_for_dates(
                origin=origin, destination=destination, depart_date=depart_date,
                direct=True, limit=50, unique=True, sorting="price",
            ),
            self.search_prices_for_dates(
                origin=origin, destination=destination, depart_date=depart_date,
                direct=True, limit=50, unique=False, sorting="route",
            ),
        ]
        results = await _aio.gather(*tasks, return_exceptions=True)

        all_tickets = []
        for res in results:
            if isinstance(res, list):
                all_tickets.extend(res)

        for t in all_tickets:
            t["transfers"] = 0
            t.setdefault("origin", origin)
            t.setdefault("destination", destination)
            t.setdefault("price", t.get("value", 0))

        seen = set()
        unique = []
        for t in all_tickets:
            key = (t.get("price", 0), t.get("departure_at", "")[:16], t.get("airline", ""))
            if key not in seen:
                seen.add(key)
                unique.append(t)

        unique.sort(key=lambda t: t.get("departure_at", ""))
        return unique

    async def get_nearby_cheapest(self, origin: str, destination: str, depart_date: str, days: int = 1) -> list[tuple[str, dict]]:
        """Самые дешёвые билеты на соседние даты (±days дней). Возвращает список (date_str, ticket)."""
        base_dt = datetime.strptime(depart_date, "%Y-%m-%d")
        results = []
        for delta in range(-days, days + 1):
            if delta == 0:
                continue
            d = base_dt + timedelta(days=delta)
            date_str = d.strftime("%Y-%m-%d")
            # Сначала ищем прямые
            day_tickets = await self.search_prices_for_dates(
                origin=origin, destination=destination, depart_date=date_str, direct=True, limit=10
            )
            for t in day_tickets:
                t["transfers"] = 0
            # Если нет прямых — ищем любые
            if not day_tickets:
                day_tickets = await self.search_prices_for_dates(
                    origin=origin, destination=destination, depart_date=date_str, direct=False, limit=10
                )
            if day_tickets:
                cheapest = min(day_tickets, key=lambda t: t.get("price", 999999))
                cheapest.setdefault("origin", origin)
                cheapest.setdefault("destination", destination)
                cheapest.setdefault("price", cheapest.get("value", 0))
                results.append((date_str, cheapest))
        return results

    async def cleanup_expired_cache(self):
        """Удаляет просроченные записи кэша."""
        async with async_session_maker() as session:
            result = await session.execute(
                delete(PriceCache).where(PriceCache.expires_at < datetime.utcnow())
            )
            await session.commit()
            logger.info("Cache cleanup: removed %d expired entries", result.rowcount)


aviasales_api = AviasalesAPI()
