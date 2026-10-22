import re
from types import SimpleNamespace

import pytest

from bot.handlers import browse
from bot.services.fast_browse import IndicativeResults, visible_options


def _option(index, transfers, price=None):
    return {
        "origin": "MOW",
        "destination": f"D{index:02d}",
        "city": f"Город{index}",
        "country": "Страна",
        "country_code": "TR",
        "price": price if price is not None else 5000 + index,
        "transfers": transfers,
        "departure_at": "2026-10-03T10:00:00+03:00",
    }


def _payload(options, owner_id=42):
    return {
        "options": options,
        "meta": {"kind": "browse", "owner_id": owner_id, "chat_id": owner_id,
                 "origin": "MOW", "departure_date": "2026-10-03", "unresolved": 0},
    }


def _mixed(count=6):
    """Чётные варианты прямые, нечётные с пересадкой."""
    return [_option(i, 0 if i % 2 == 0 else 1) for i in range(count)]


def _buttons(keyboard):
    return [button for row in keyboard.inline_keyboard for button in row]


def _callbacks(keyboard):
    return [b.callback_data for b in _buttons(keyboard) if b.callback_data]


def _ranks(text):
    return [int(m.group(1)) for line in text.splitlines() if (m := re.match(r"^<b>(\d+)\.</b>", line))]


# ── Отбор вариантов ──

def test_visible_options_without_filter_keeps_everything():
    options = _mixed()

    assert [i for i, _ in visible_options(options, False)] == [0, 1, 2, 3, 4, 5]


def test_visible_options_direct_only_keeps_original_indexes():
    options = _mixed()

    assert [i for i, _ in visible_options(options, True)] == [0, 2, 4]


def test_unknown_transfers_are_not_counted_as_direct():
    options = [_option(0, None), _option(1, 0)]

    assert [i for i, _ in visible_options(options, True)] == [1]


# ── Текст и кнопки списка ──

def test_list_without_filter_is_unchanged_and_toggle_is_off():
    payload = _payload(_mixed())

    text = browse._format_page(payload, 0)
    keyboard = browse._page_keyboard("abc", payload, 0)

    assert _ranks(text) == [1, 2, 3, 4, 5, 6]
    assert "Только без пересадок" not in text
    toggle = _buttons(keyboard)[0]
    assert "ВЫКЛ" in toggle.text and toggle.callback_data == "brdirect:abc:1"


def test_list_with_filter_shows_only_direct_and_renumbers():
    payload = _payload(_mixed())

    text = browse._format_page(payload, 0, True)

    assert _ranks(text) == [1, 2, 3]
    assert "Только без пересадок" in text
    assert "Вариантов: <b>3</b> из 6" in text
    assert "Город1" not in text and "Город0" in text and "Город2" in text


def test_filtered_buttons_point_to_the_original_destinations():
    payload = _payload(_mixed())

    keyboard = browse._page_keyboard("abc", payload, 0, True)

    callbacks = _callbacks(keyboard)
    assert [c for c in callbacks if c.startswith("brsel:")] == ["brsel:abc:0", "brsel:abc:2", "brsel:abc:4"]
    labels = [b.text for b in _buttons(keyboard) if (b.callback_data or "").startswith("brsel:")]
    assert labels == ["1. Город0", "2. Город2", "3. Город4"]
    toggle = _buttons(keyboard)[0]
    assert "ВКЛ" in toggle.text and toggle.callback_data == "brdirect:abc:0"


def test_filter_with_no_direct_options_explains_and_keeps_toggle():
    payload = _payload([_option(0, 1), _option(1, 2)])

    text = browse._format_page(payload, 0, True)
    keyboard = browse._page_keyboard("abc", payload, 0, True)

    assert "нет" in text and "Выключите" in text
    assert _ranks(text) == []
    assert _buttons(keyboard)[0].callback_data == "brdirect:abc:0"
    assert not [c for c in _callbacks(keyboard) if c.startswith("brsel:")]


def test_paging_is_computed_over_the_filtered_list():
    # 30 прямых и 30 с пересадкой вперемешку: в фильтре 30 вариантов, 3 страницы
    options = [_option(i, 0 if i % 2 == 0 else 1) for i in range(60)]
    payload = _payload(options)

    first = browse._page_keyboard("abc", payload, 0, True)
    last = browse._page_keyboard("abc", payload, 2, True)

    assert "brpage:abc:1" in _callbacks(first) and "brpage:abc:-1" not in _callbacks(first)
    assert "brpage:abc:1" in _callbacks(last) and "brpage:abc:3" not in _callbacks(last)
    assert any(b.text == "3/3" for b in _buttons(last))
    assert _ranks(browse._format_page(payload, 2, True)) == list(range(21, 31))


def test_back_to_list_returns_to_the_page_where_the_option_is():
    options = [_option(i, 0 if i % 2 == 0 else 1) for i in range(60)]

    assert browse._back_page(options, 24, False) == 2   # 24-й в полном списке
    assert browse._back_page(options, 24, True) == 1    # 13-й среди прямых
    assert browse._back_page(options, 25, True) == 0    # с пересадкой, в фильтре его нет


def test_selected_card_keeps_the_old_back_button_by_default():
    keyboard = browse._selected_keyboard("abc", 24, _option(24, 0), "2026-10-03")

    assert "brpage:abc:2" in _callbacks(keyboard)


# ── Обработчики ──

class _FakeArchive:
    def __init__(self, payload=None):
        self.payload = payload
        self.saved = {}

    def get(self, key):
        return self.payload

    def put_browse(self, key, payload):
        self.saved[key] = payload


class _FakeMessage:
    def __init__(self):
        self.chat = SimpleNamespace(id=42)
        self.edits = []
        self.answers = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return self


class _FakeCallback:
    def __init__(self, data, user_id=42):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id, username="u", first_name="U")
        self.message = _FakeMessage()
        self.alerts = []
        self.notices = []

    async def answer(self, text=None, show_alert=False):
        if show_alert:
            self.alerts.append(text)
        elif text:
            self.notices.append(text)


@pytest.fixture
def prefs(monkeypatch):
    store = SimpleNamespace(data={}, fail=False)

    async def load(user_id):
        return store.data.get(user_id, False)

    async def save(user, value):
        if store.fail:
            return False
        store.data[user.id] = value
        return True

    monkeypatch.setattr(browse, "load_direct_only", load)
    monkeypatch.setattr(browse, "save_direct_only", save)
    return store


def _use_archive(monkeypatch, options, owner_id=42):
    archive = _FakeArchive(_payload(options, owner_id))
    monkeypatch.setattr(browse, "discovery_archive", archive)
    return archive


@pytest.mark.asyncio
async def test_toggle_saves_the_choice_and_redraws_the_same_list(monkeypatch, prefs):
    _use_archive(monkeypatch, _mixed())
    callback = _FakeCallback("brdirect:abc:1")

    await browse.browse_direct_toggle(callback)

    assert prefs.data[42] is True
    text, kwargs = callback.message.edits[0]
    assert _ranks(text) == [1, 2, 3]
    assert _buttons(kwargs["reply_markup"])[0].callback_data == "brdirect:abc:0"
    assert callback.notices == []


@pytest.mark.asyncio
async def test_toggle_off_restores_the_full_list(monkeypatch, prefs):
    _use_archive(monkeypatch, _mixed())
    prefs.data[42] = True

    callback = _FakeCallback("brdirect:abc:0")
    await browse.browse_direct_toggle(callback)

    assert prefs.data[42] is False
    assert _ranks(callback.message.edits[0][0]) == [1, 2, 3, 4, 5, 6]


@pytest.mark.asyncio
async def test_toggle_works_even_if_the_choice_cannot_be_saved(monkeypatch, prefs):
    _use_archive(monkeypatch, _mixed())
    prefs.fail = True
    callback = _FakeCallback("brdirect:abc:1")

    await browse.browse_direct_toggle(callback)

    assert _ranks(callback.message.edits[0][0]) == [1, 2, 3]
    assert callback.notices and "запомнить" in callback.notices[0]


@pytest.mark.asyncio
async def test_toggle_is_only_for_the_owner_of_the_search(monkeypatch, prefs):
    _use_archive(monkeypatch, _mixed(), owner_id=42)
    stranger = _FakeCallback("brdirect:abc:1", user_id=99)
    stranger.message.chat = SimpleNamespace(id=555)

    await browse.browse_direct_toggle(stranger)

    assert stranger.alerts and prefs.data == {} and stranger.message.edits == []


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["brdirect:abc", "brdirect:abc:2", "brdirect:abc:x"])
async def test_broken_toggle_button_is_rejected(monkeypatch, prefs, data):
    _use_archive(monkeypatch, _mixed())
    callback = _FakeCallback(data)

    await browse.browse_direct_toggle(callback)

    assert callback.alerts and prefs.data == {} and callback.message.edits == []


@pytest.mark.asyncio
async def test_expired_search_is_reported(monkeypatch, prefs):
    monkeypatch.setattr(browse, "discovery_archive", _FakeArchive(None))
    callback = _FakeCallback("brdirect:abc:1")

    await browse.browse_direct_toggle(callback)

    assert callback.alerts and prefs.data == {}


@pytest.mark.asyncio
async def test_paging_follows_the_saved_choice(monkeypatch, prefs):
    options = [_option(i, 0 if i % 2 == 0 else 1) for i in range(60)]
    _use_archive(monkeypatch, options)
    prefs.data[42] = True

    callback = _FakeCallback("brpage:abc:1")
    await browse.browse_page(callback)

    assert _ranks(callback.message.edits[0][0]) == list(range(11, 21))


@pytest.mark.asyncio
async def test_paging_past_the_end_of_the_filtered_list_is_rejected(monkeypatch, prefs):
    options = [_option(i, 0 if i % 2 == 0 else 1) for i in range(60)]
    _use_archive(monkeypatch, options)
    prefs.data[42] = True

    callback = _FakeCallback("brpage:abc:3")  # в фильтре страниц только 3 (0..2)
    await browse.browse_page(callback)

    assert callback.alerts and callback.message.edits == []


@pytest.mark.asyncio
async def test_card_back_button_follows_the_filter(monkeypatch, prefs):
    options = [_option(i, 0 if i % 2 == 0 else 1) for i in range(60)]
    _use_archive(monkeypatch, options)
    prefs.data[42] = True

    callback = _FakeCallback("brsel:abc:24")
    await browse.browse_select(callback)

    _, kwargs = callback.message.edits[0]
    assert "brpage:abc:1" in _callbacks(kwargs["reply_markup"])


@pytest.mark.asyncio
async def test_new_search_starts_with_the_saved_choice(monkeypatch, prefs):
    archive = _FakeArchive()
    monkeypatch.setattr(browse, "discovery_archive", archive)

    async def fake_load(origin, departure_date):
        return IndicativeResults(_mixed(), source_rows=6, unresolved=0)

    monkeypatch.setattr(browse, "load_indicative_destinations", fake_load)
    prefs.data[42] = True
    message = _FakeMessage()

    await browse._run_browse(message, "MOW", "2026-10-03", owner_id=42)

    text, kwargs = message.edits[0]
    assert _ranks(text) == [1, 2, 3]
    assert _buttons(kwargs["reply_markup"])[0].callback_data == "brdirect:" + next(iter(archive.saved)) + ":0"
    # В архив уходит полный список: фильтр только скрывает варианты, не удаляет их
    assert len(next(iter(archive.saved.values()))["options"]) == 6
