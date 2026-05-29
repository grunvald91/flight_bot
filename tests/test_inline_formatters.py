from bot.services.city_resolver import city_resolver, CityResolver
from bot.utils.formatters import format_ticket_inline_title


def _install_resolver():
    resolver = CityResolver()
    resolver._index_data(
        [
            {
                "code": "AER",
                "name": "Сочи",
                "name_translations": {"en": "Sochi"},
            },
            {
                "code": "LED",
                "name": "Санкт-Петербург",
                "name_translations": {"en": "Saint Petersburg"},
            },
        ],
        "cities",
    )
    city_resolver.cities = dict(resolver.cities)
    city_resolver.cities_en = dict(resolver.cities_en)
    city_resolver.iata_to_name = dict(resolver.iata_to_name)
    city_resolver.airports = dict(resolver.airports)
    city_resolver.airlines = dict(resolver.airlines)
    city_resolver.aliases = dict(resolver.aliases)


def test_format_ticket_inline_title_prefers_day_label_when_present():
    _install_resolver()

    title = format_ticket_inline_title(
        {
            "origin": "AER",
            "destination": "LED",
            "transfers": 0,
            "inline_day_label": "Сегодня",
        }
    )

    assert title.startswith("Сегодня")
    assert "Сочи" in title
    assert "Санкт-Петербург" in title
