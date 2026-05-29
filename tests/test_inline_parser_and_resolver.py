from bot.services.city_resolver import CityResolver, city_resolver
from bot.utils.parsers import parse_inline_query


def _build_resolver() -> CityResolver:
    resolver = CityResolver()
    resolver._index_data(
        [
            {
                "code": "LED",
                "name": "Санкт-Петербург",
                "name_translations": {"en": "Saint Petersburg"},
            },
            {
                "code": "AER",
                "name": "Сочи",
                "name_translations": {"en": "Sochi"},
            },
            {
                "code": "MOW",
                "name": "Москва",
                "name_translations": {"en": "Moscow"},
            },
        ],
        "cities",
    )
    return resolver


def _install_global_resolver():
    resolver = _build_resolver()
    city_resolver.cities = dict(resolver.cities)
    city_resolver.cities_en = dict(resolver.cities_en)
    city_resolver.iata_to_name = dict(resolver.iata_to_name)
    city_resolver.airports = dict(resolver.airports)
    city_resolver.airlines = dict(resolver.airlines)
    city_resolver.aliases = dict(resolver.aliases)


def test_parse_compound_route_with_spaces_around_dash():
    _install_global_resolver()
    parsed = parse_inline_query("Санкт-Петербург - Сочи")
    assert parsed.query_type == "route"
    assert parsed.raw_origin == "Санкт-Петербург"
    assert parsed.raw_destination == "Сочи"


def test_parse_compound_route_with_em_dash_without_spaces():
    _install_global_resolver()
    parsed = parse_inline_query("Санкт-Петербург—Сочи")
    assert parsed.query_type == "route"
    assert parsed.raw_origin == "Санкт-Петербург"
    assert parsed.raw_destination == "Сочи"


def test_parse_compound_route_with_non_breaking_hyphen():
    _install_global_resolver()
    parsed = parse_inline_query("Санкт‑Петербург-Сочи")
    assert parsed.query_type == "route"
    assert parsed.raw_origin == "Санкт-Петербург"
    assert parsed.raw_destination == "Сочи"


def test_parse_sochi_to_spb_without_spaces():
    _install_global_resolver()
    parsed = parse_inline_query("Сочи-Санкт-Петербург")
    assert parsed.query_type == "route"
    assert parsed.raw_origin == "Сочи"
    assert parsed.raw_destination == "Санкт-Петербург"


def test_parse_route_with_connections_marker_prefix():
    _install_global_resolver()
    parsed = parse_inline_query("пересадки Сочи-Санкт-Петербург")
    assert parsed.query_type == "route"
    assert parsed.connections_only is True
    assert parsed.raw_origin == "Сочи"
    assert parsed.raw_destination == "Санкт-Петербург"


def test_parse_route_with_connections_marker_suffix():
    _install_global_resolver()
    parsed = parse_inline_query("Сочи-Санкт-Петербург пересадки")
    assert parsed.query_type == "route"
    assert parsed.connections_only is True
    assert parsed.raw_origin == "Сочи"
    assert parsed.raw_destination == "Санкт-Петербург"


def test_resolve_accepts_space_dash_and_alias_variants():
    resolver = _build_resolver()
    assert resolver.resolve("Санкт-Петербург") == "LED"
    assert resolver.resolve("Санкт Петербург") == "LED"
    assert resolver.resolve("Санкт—Петербург") == "LED"
    assert resolver.resolve("Санкт‑Петербург") == "LED"
    assert resolver.resolve("СПб") == "LED"
    assert resolver.resolve("Сочи") == "AER"
