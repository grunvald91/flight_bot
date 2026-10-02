import pytest

from bot.handlers import browse, discover, entry
from bot.services import entry_rules
from bot.services.discovery_archive import DiscoveryArchive


def test_parse_kdmid_csv_for_ordinary_passport():
    text = (
        "№;Страна въезда;Режим въезда с дипломатическим паспортом;"
        "Режим въезда со служебным паспортом;"
        "Режим въезда с общегражданским паспортом\n"
        '1;Турция;без визы;без визы;"без визы до 60 дней"\n'
        '2;ФРГ;требуется виза;требуется виза;"требуется виза"\n'
    )
    rows = entry_rules._parse_csv(text)

    assert rows == [
        {"country": "Турция", "ordinary_passport": "без визы до 60 дней"},
        {"country": "ФРГ", "ordinary_passport": "требуется виза"},
    ]


@pytest.mark.asyncio
async def test_find_entry_rule_uses_country_code_alias(monkeypatch):
    async def fake_get_rules():
        return (
            [
                {"country": "ФРГ", "ordinary_passport": "требуется виза"},
                {"country": "Турция", "ordinary_passport": "без визы"},
            ],
            None,
            False,
        )

    monkeypatch.setattr(entry_rules, "get_rules", fake_get_rules)
    rule = await entry_rules.find_entry_rule("Германия", "DE")

    assert rule is not None
    assert rule.country == "ФРГ"
    assert rule.ordinary_passport == "требуется виза"


def test_fast_browse_keyboard_has_entry_button():
    option = {
        "origin": "MOW",
        "destination": "IST",
        "country_code": "TR",
    }
    keyboard = browse._selected_keyboard("abc", 0, option, "2026-10-03")
    buttons = [button for row in keyboard.inline_keyboard for button in row]

    assert any(button.callback_data == "entry:TR" for button in buttons)


def test_deep_discovery_keyboard_has_entry_button():
    offer = {
        "_discovery": {"country_code": "TR"},
        "search_results_url": "https://example.com",
    }
    keyboard = discover._discovery_keyboard("abc", [offer], 0)
    buttons = [button for row in keyboard.inline_keyboard for button in row]

    assert any(button.callback_data == "entry:TR" for button in buttons)


def test_fast_browse_list_shows_entry_button_for_each_destination():
    payload = {
        "options": [
            {
                "origin": "MOW",
                "destination": "IST",
                "city": "Стамбул",
                "country": "Турция",
                "country_code": "TR",
                "price": 9000,
                "transfers": 0,
            },
            {
                "origin": "MOW",
                "destination": "EVN",
                "city": "Ереван",
                "country": "Армения",
                "country_code": "AM",
                "price": 10000,
                "transfers": 0,
            },
        ],
        "meta": {"departure_date": "2026-10-03"},
    }

    keyboard = browse._page_keyboard("abc", payload, 0)
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    ]

    assert "entry:TR" in callbacks
    assert "entry:AM" in callbacks
    assert "brsel:abc:0" in callbacks
    assert "brsel:abc:1" in callbacks


@pytest.mark.asyncio
async def test_kazakhstan_verified_override_does_not_depend_on_remote_dataset(monkeypatch):
    async def fail_get_rules():
        raise AssertionError("remote dataset should not be called for verified override")

    monkeypatch.setattr(entry_rules, "get_rules", fail_get_rules)
    rule = await entry_rules.find_entry_rule("Казахстан", "KZ")

    assert rule is not None
    assert "Виза не нужна" in rule.ordinary_passport
    assert "90" in rule.ordinary_passport
    assert "внутреннему российскому паспорту" in rule.details
    assert "3 рабочих дней" in rule.details
    assert "gov.kz" in rule.source_url


def test_actual_kdmid_english_headers_parse():
    raw = (
        'country,diplomatic,service,citizen\n'
        '"Казахстан","без визы","без визы","без визы"\n'
        '"Турция","без визы","без визы","без визы до 30 дней"\n'
    ).encode("utf-8")

    text = entry_rules._decode_csv_bytes(raw)
    rows = entry_rules._parse_csv(text)

    assert rows == [
        {"country": "Казахстан", "ordinary_passport": "без визы"},
        {"country": "Турция", "ordinary_passport": "без визы до 30 дней"},
    ]


def test_fast_browse_archive_keeps_entry_buttons_after_paging(tmp_path):
    archive = DiscoveryArchive(tmp_path / "browse.sqlite3")
    payload = {
        "options": [
            {
                "origin": "MOW",
                "destination": "IST",
                "city": "Стамбул",
                "country": "Турция",
                "country_code": "TR",
                "price": 9000,
                "transfers": 0,
            },
            {
                "origin": "MOW",
                "destination": "ALA",
                "city": "Алматы",
                "country": "Казахстан",
                "country_code": "KZ",
                "price": 10000,
                "transfers": 0,
            },
        ],
        "meta": {
            "owner_id": 123,
            "chat_id": 123,
            "origin": "MOW",
            "departure_date": "2026-10-03",
            "unresolved": 0,
            "source_rows": 2,
        },
    }

    archive.put_browse("paging-test", payload)
    restored = archive.get("paging-test")

    assert restored["options"][0]["country_code"] == "TR"
    assert restored["options"][1]["country_code"] == "KZ"

    keyboard = browse._page_keyboard("paging-test", restored, 0)
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert "entry:TR" in callbacks
    assert "entry:KZ" in callbacks


def test_entry_card_date_is_human_readable():
    assert entry._format_checked_at("2026-10-02T15:47:18+03:00") == "02.10.2026"
    assert entry._format_checked_at("2026-10-02") == "02.10.2026"


def test_entry_card_simplifies_common_visa_rule():
    lines = entry._simple_entry_lines(
        "без визы до 60 дней (Соглашение между Правительствами от 12.05.2010)"
    )
    text = "\n".join(lines)

    assert "Виза не нужна" in text
    assert "до 60 дней" in text
    assert "Соглашение" not in text


def test_kazakhstan_card_is_short_and_practical():
    rule = entry_rules.VERIFIED_COUNTRY_RULES["KZ"]
    lines = entry._simple_entry_lines(rule.ordinary_passport, rule.details)
    text = "\n".join(lines)

    assert "Виза не нужна" in text
    assert "90 дней" in text
    assert "внутренний российский паспорт" in text
    assert "3 рабочих дней" in text
    assert "РВП" not in text
