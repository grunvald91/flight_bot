import re
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from bot.handlers import month
from bot.services.aviasales_api import AviasalesAPI
from bot.services.city_resolver import city_resolver


def _dep(days_ahead, hour=10):
    day = datetime.now().date() + timedelta(days=days_ahead)
    return f"{day.isoformat()}T{hour:02d}:00:00+03:00"


def _ticket(price, days_ahead=3, transfers=0, airline="SU", hour=10):
    return {
        "price": price,
        "airline": airline,
        "transfers": transfers,
        "departure_at": _dep(days_ahead, hour),
        "origin": "AER",
        "destination": "LED",
    }


class _FakeApi:
    def __init__(self, tickets):
        self.tickets = tickets
        self.calls = []

    async def search_cheapest_in_window(self, origin, destination, start_date=None, days=30):
        self.calls.append((origin, destination, days))
        # как и настоящий метод: отсортировано от дешёвых к дорогим
        return sorted((dict(t) for t in self.tickets), key=lambda t: t["price"])

    def build_ticket_link(self, ticket, origin, destination, depart_date=None, return_date=None):
        return f"https://example.test/search?o={origin}&d={destination}&p={ticket['price']}"


@pytest.fixture(autouse=True)
def _cities(monkeypatch):
    """Справочник городов в тестах не загружается: трёхбуквенные коды считаем распознанными."""
    monkeypatch.setattr(
        city_resolver, "resolve", lambda q: q.upper() if len(q) == 3 and q.isalpha() else None,
    )
    monkeypatch.setattr(city_resolver, "get_city_name", lambda code: code)
    monkeypatch.setattr(city_resolver, "get_airline_name", lambda code: code)


@pytest.fixture(autouse=True)
def prefs(monkeypatch):
    """Память выбора «Без пересадок» подменена словарём; fail=True имитирует сбой записи."""
    store = SimpleNamespace(data={}, fail=False)

    async def load(user_id):
        return store.data.get(user_id, False)

    async def save(user, value):
        if store.fail:
            return False
        store.data[user.id] = value
        return True

    monkeypatch.setattr(month, "load_direct_only", load)
    monkeypatch.setattr(month, "save_direct_only", save)
    return store


def _use_api(monkeypatch, tickets):
    api = _FakeApi(tickets)
    monkeypatch.setattr(month, "aviasales_api", api)
    return api


def _buttons(keyboard):
    return [button for row in keyboard.inline_keyboard for button in row]


def _by_text(keyboard, fragment):
    return [b for b in _buttons(keyboard) if fragment in b.text]


def _ranks(text):
    """Номера билетов в выдаче: строки вида «12. <цена> · ...»."""
    return [int(m.group(1)) for line in text.splitlines() if (m := re.match(r"^(\d+)\. ", line))]


# ── Выдача ──

@pytest.mark.asyncio
async def test_results_are_ranked_from_cheapest(monkeypatch):
    api = _use_api(monkeypatch, [_ticket(9000), _ticket(5000, days_ahead=20), _ticket(7000, transfers=1)])

    text, _ = await month._render("AER", "LED", direct_only=False, page=0)

    body = [line for line in text.splitlines() if re.match(r"^\d+\. ", line)]
    assert _ranks(text) == [1, 2, 3]
    assert ("5 000" in body[0], "7 000" in body[1], "9 000" in body[2]) == (True, True, True)
    assert "от дешёвых к дорогим" in text
    assert "Всего вариантов: 3" in text
    assert api.calls == [("AER", "LED", 30)]


@pytest.mark.asyncio
async def test_toggle_off_by_default_and_flips_on_press(monkeypatch):
    _use_api(monkeypatch, [_ticket(5000)])

    _, keyboard = await month._render("AER", "LED", direct_only=False, page=0)

    toggle = _by_text(keyboard, "Без пересадок")[0]
    assert "ВЫКЛ" in toggle.text
    assert toggle.callback_data == "mt:AER:LED:1"


@pytest.mark.asyncio
async def test_toggle_on_leaves_only_direct_flights(monkeypatch):
    _use_api(monkeypatch, [_ticket(4000, transfers=1), _ticket(9000), _ticket(6000, transfers=2)])

    text, keyboard = await month._render("AER", "LED", direct_only=True, page=0)

    assert "Всего вариантов: 1" in text
    assert "9 000" in text and "4 000" not in text and "6 000" not in text
    assert "Только без пересадок" in text
    toggle = _by_text(keyboard, "Без пересадок")[0]
    assert "ВКЛ" in toggle.text
    assert toggle.callback_data == "mt:AER:LED:0"  # повторное нажатие выключает


@pytest.mark.asyncio
async def test_connecting_flights_are_shown_with_transfer_count_when_toggle_off(monkeypatch):
    _use_api(monkeypatch, [_ticket(4000, transfers=1), _ticket(9000)])

    text, _ = await month._render("AER", "LED", direct_only=False, page=0)

    assert "1 пересадка" in text
    assert "прямой" in text


@pytest.mark.asyncio
async def test_no_direct_flights_explains_and_keeps_toggle(monkeypatch):
    _use_api(monkeypatch, [_ticket(4000, transfers=1)])

    text, keyboard = await month._render("AER", "LED", direct_only=True, page=0)

    assert "Прямых билетов" in text and "Выключите" in text
    assert _by_text(keyboard, "Без пересадок")


@pytest.mark.asyncio
async def test_no_tickets_at_all(monkeypatch):
    _use_api(monkeypatch, [])

    text, keyboard = await month._render("AER", "LED", direct_only=False, page=0)

    assert "не нашёл" in text
    assert _by_text(keyboard, "Другое направление")


@pytest.mark.asyncio
async def test_ticket_link_is_html_escaped(monkeypatch):
    _use_api(monkeypatch, [_ticket(5000)])

    text, _ = await month._render("AER", "LED", direct_only=False, page=0)

    assert "o=AER&amp;d=LED" in text
    assert "o=AER&d=LED" not in text


# ── Страницы ──

@pytest.mark.asyncio
async def test_pagination_covers_every_ticket_in_price_order(monkeypatch):
    _use_api(monkeypatch, [_ticket(1000 + i) for i in range(25)])

    pages_text = []
    for page in range(3):
        text, _ = await month._render("AER", "LED", direct_only=False, page=page)
        pages_text.append(text)

    ranks = []
    for text in pages_text:
        ranks += _ranks(text)
    assert ranks == list(range(1, 26))
    assert "1 000" in pages_text[0] and "1 024" in pages_text[2]  # дешёвые в начале, дорогие в конце


@pytest.mark.asyncio
async def test_pagination_buttons_at_the_edges(monkeypatch):
    _use_api(monkeypatch, [_ticket(1000 + i) for i in range(25)])

    _, first = await month._render("AER", "LED", direct_only=False, page=0)
    _, middle = await month._render("AER", "LED", direct_only=False, page=1)
    _, last = await month._render("AER", "LED", direct_only=False, page=2)

    assert not _by_text(first, "◀️") and _by_text(first, "▶️")
    assert _by_text(middle, "◀️") and _by_text(middle, "▶️")
    assert _by_text(last, "◀️") and not _by_text(last, "▶️")
    assert _by_text(middle, "2 / 3")


@pytest.mark.asyncio
async def test_page_beyond_the_end_shows_last_page(monkeypatch):
    _use_api(monkeypatch, [_ticket(1000 + i) for i in range(12)])

    text, keyboard = await month._render("AER", "LED", direct_only=False, page=99)

    assert _ranks(text) == [11, 12]
    assert _by_text(keyboard, "2 / 2")


@pytest.mark.asyncio
async def test_toggle_from_a_later_page_returns_to_first_page(monkeypatch):
    _use_api(monkeypatch, [_ticket(1000 + i) for i in range(25)])

    _, keyboard = await month._render("AER", "LED", direct_only=False, page=2)

    assert _by_text(keyboard, "Без пересадок")[0].callback_data == "mt:AER:LED:1"


@pytest.mark.asyncio
async def test_all_callback_data_fit_telegram_limit(monkeypatch):
    _use_api(monkeypatch, [_ticket(1000 + i) for i in range(500)])

    _, keyboard = await month._render("AER", "LED", direct_only=True, page=25)

    assert all(len((b.callback_data or "").encode()) <= 64 for b in _buttons(keyboard))


# ── Разбор кнопок ──

@pytest.mark.parametrize(
    "data, expected",
    [
        ("mo:AER:LED:0:0", ("AER", "LED", False, 0)),
        ("mo:MOW:IST:1:12", ("MOW", "IST", True, 12)),
        ("mo:AER:LED:2:0", None),
        ("mo:AER:LED:1:-1", None),
        ("mo:aer:LED:1:0", None),
        ("mo:AERO:LED:1:0", None),
        ("mo:AER:LED:1", None),
        ("mo:AER:LED:1:0:9", None),
        ("mo:AER:LED:1:²", None),          # не-ASCII цифра раньше роняла обработчик
        ("mo:AER\n:LED:1:0", None),
        ("mo:AER:LED:1:99999", None),
        ("xx:AER:LED:1:0", None),
        ("", None),
    ],
)
def test_parse_callback(data, expected):
    assert month._parse_callback(data) == expected


class _FakeMessage:
    def __init__(self):
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
        self.answered = 0

    async def answer(self, text=None, show_alert=False):
        self.answered += 1
        if show_alert:
            self.alerts.append(text)
        elif text:
            self.notices.append(text)


@pytest.mark.asyncio
async def test_pressing_toggle_redraws_the_same_message_with_direct_flights_only(monkeypatch):
    _use_api(monkeypatch, [_ticket(4000, transfers=1), _ticket(9000)])
    callback = _FakeCallback("mo:AER:LED:1:0")

    await month.cb_month_view(callback)

    assert len(callback.message.edits) == 1
    text, kwargs = callback.message.edits[0]
    assert "9 000" in text and "4 000" not in text
    assert "ВКЛ" in _by_text(kwargs["reply_markup"], "Без пересадок")[0].text


@pytest.mark.asyncio
async def test_broken_button_is_rejected_without_searching(monkeypatch):
    api = _use_api(monkeypatch, [_ticket(5000)])
    callback = _FakeCallback("mo:garbage")

    await month.cb_month_view(callback)

    assert callback.alerts and "/month" in callback.alerts[0]
    assert api.calls == [] and callback.message.edits == []


# ── Сценарий выбора направления ──

class _FakeState:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.state = None

    async def clear(self):
        self.data, self.state = {}, None

    async def set_state(self, value):
        self.state = value

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return dict(self.data)


class _FakeUserMessage(_FakeMessage):
    def __init__(self, text, user_id=42):
        super().__init__()
        self.text = text
        self.from_user = SimpleNamespace(id=user_id, username="u", first_name="U")


@pytest.mark.asyncio
async def test_command_starts_origin_step():
    message, state = _FakeUserMessage("/month"), _FakeState({"stale": 1})

    await month.cmd_month(message, state)

    assert state.state == month.MonthSearchStates.choosing_origin
    assert state.data == {}
    assert "город вылета" in message.answers[0][0]


@pytest.mark.asyncio
async def test_unknown_origin_is_asked_again():
    message, state = _FakeUserMessage("Нарния"), _FakeState()

    await month.month_origin(message, state)

    assert state.state is None
    assert "не найден" in message.answers[0][0]


@pytest.mark.asyncio
async def test_origin_then_destination_runs_the_search(monkeypatch):
    api = _use_api(monkeypatch, [_ticket(9000), _ticket(5000)])
    state = _FakeState()

    await month.month_origin(_FakeUserMessage("AER"), state)
    assert state.state == month.MonthSearchStates.choosing_destination

    message = _FakeUserMessage("LED")
    await month.month_destination(message, state)

    assert api.calls == [("AER", "LED", 30)]
    assert state.state is None  # состояние не висит: дальше всё на кнопках
    text, kwargs = message.edits[0]
    assert text.index("5 000") < text.index("9 000")
    assert "mt:AER:LED:1" in [b.callback_data for b in _buttons(kwargs["reply_markup"])]


@pytest.mark.asyncio
async def test_destination_without_origin_asks_to_restart():
    message, state = _FakeUserMessage("LED"), _FakeState()

    await month.month_destination(message, state)

    assert "/month" in message.answers[0][0]


# ── Запоминание переключателя «Без пересадок» ──

@pytest.mark.parametrize(
    "data, expected",
    [
        ("mt:AER:LED:1", ("AER", "LED", True)),
        ("mt:MOW:IST:0", ("MOW", "IST", False)),
        ("mt:AER:LED:2", None),
        ("mt:AER:LED", None),
        ("mt:AER:LED:1:0", None),
        ("mt:aer:LED:1", None),
        ("mo:AER:LED:1", None),
    ],
)
def test_parse_toggle(data, expected):
    assert month._parse_toggle(data) == expected


@pytest.mark.asyncio
async def test_search_starts_with_the_last_saved_choice(monkeypatch, prefs):
    _use_api(monkeypatch, [_ticket(4000, transfers=1), _ticket(9000)])
    prefs.data[42] = True
    state = _FakeState()

    await month.month_origin(_FakeUserMessage("AER"), state)
    message = _FakeUserMessage("LED")
    await month.month_destination(message, state)

    text, kwargs = message.edits[0]
    assert "9 000" in text and "4 000" not in text
    assert "Только без пересадок" in text
    toggle = _by_text(kwargs["reply_markup"], "Без пересадок")[0]
    assert "ВКЛ" in toggle.text and toggle.callback_data == "mt:AER:LED:0"


@pytest.mark.asyncio
async def test_saved_choice_is_per_person(monkeypatch, prefs):
    _use_api(monkeypatch, [_ticket(4000, transfers=1), _ticket(9000)])
    prefs.data[42] = True

    state = _FakeState({"month_origin": "AER"})
    other = _FakeUserMessage("LED", user_id=77)  # у этого человека выбора нет
    await month.month_destination(other, state)

    assert "4 000" in other.edits[0][0]


@pytest.mark.asyncio
async def test_pressing_toggle_saves_the_choice_and_redraws(monkeypatch, prefs):
    _use_api(monkeypatch, [_ticket(4000, transfers=1), _ticket(9000)])

    on = _FakeCallback("mt:AER:LED:1", user_id=77)
    await month.cb_month_toggle(on)

    assert prefs.data[77] is True
    text, kwargs = on.message.edits[0]
    assert "9 000" in text and "4 000" not in text
    assert _by_text(kwargs["reply_markup"], "Без пересадок")[0].callback_data == "mt:AER:LED:0"
    assert on.notices == []

    off = _FakeCallback("mt:AER:LED:0", user_id=77)
    await month.cb_month_toggle(off)

    assert prefs.data[77] is False
    assert "4 000" in off.message.edits[0][0]


@pytest.mark.asyncio
async def test_page_buttons_do_not_change_the_saved_choice(monkeypatch, prefs):
    _use_api(monkeypatch, [_ticket(1000 + i) for i in range(25)])

    await month.cb_month_view(_FakeCallback("mo:AER:LED:0:1"))

    assert prefs.data == {}


@pytest.mark.asyncio
async def test_toggle_still_works_when_saving_fails(monkeypatch, prefs):
    _use_api(monkeypatch, [_ticket(4000, transfers=1), _ticket(9000)])
    prefs.fail = True
    callback = _FakeCallback("mt:AER:LED:1")

    await month.cb_month_toggle(callback)

    assert "4 000" not in callback.message.edits[0][0]  # фильтр применился
    assert callback.notices and "запомнить" in callback.notices[0]


@pytest.mark.asyncio
async def test_broken_toggle_button_is_rejected_without_saving(monkeypatch, prefs):
    api = _use_api(monkeypatch, [_ticket(5000)])
    callback = _FakeCallback("mt:garbage")

    await month.cb_month_toggle(callback)

    assert callback.alerts and prefs.data == {}
    assert api.calls == [] and callback.message.edits == []


# ── Метод запроса билетов на месяц ──

class _FakeMonthPricesAPI(AviasalesAPI):
    """Реальный search_cheapest_in_window поверх подменённого запроса цен."""

    def __init__(self, by_month):
        self.by_month = by_month
        self.month_calls = []

    async def search_prices_for_dates(self, origin, destination, depart_date=None, **kwargs):
        self.month_calls.append((depart_date, kwargs.get("one_way"), kwargs.get("limit")))
        return [dict(t) for t in self.by_month.get(depart_date, [])]


def _raw(price, departure_at, transfers=0, airline="SU"):
    return {"price": price, "airline": airline, "transfers": transfers, "departure_at": departure_at}


@pytest.mark.asyncio
async def test_window_sorts_by_price_and_cuts_to_window():
    api = _FakeMonthPricesAPI({
        "2026-04": [
            _raw(12000, "2026-04-20T10:00:00+03:00"),
            _raw(5000, "2026-04-03T10:00:00+03:00"),   # раньше окна
            _raw(7000, "2026-04-25T08:00:00+03:00", transfers=1),
        ],
        "2026-05": [
            _raw(6000, "2026-05-02T10:00:00+03:00"),   # внутри окна
            _raw(4000, "2026-05-20T10:00:00+03:00"),   # позже окна
        ],
    })

    tickets = await api.search_cheapest_in_window("AER", "LED", start_date=date(2026, 4, 7), days=30)

    assert [t["price"] for t in tickets] == [6000, 7000, 12000]
    assert [month_ for month_, _, _ in api.month_calls] == ["2026-04", "2026-05"]


@pytest.mark.asyncio
async def test_window_inside_one_month_makes_one_request():
    api = _FakeMonthPricesAPI({"2026-04": [_raw(5000, "2026-04-10T10:00:00+03:00")]})

    await api.search_cheapest_in_window("AER", "LED", start_date=date(2026, 4, 1), days=10)

    assert [month_ for month_, _, _ in api.month_calls] == ["2026-04"]


@pytest.mark.asyncio
async def test_window_across_new_year_requests_both_months():
    api = _FakeMonthPricesAPI({
        "2026-12": [_raw(8000, "2026-12-28T10:00:00+03:00")],
        "2027-01": [_raw(6000, "2027-01-05T10:00:00+03:00")],
    })

    tickets = await api.search_cheapest_in_window("AER", "LED", start_date=date(2026, 12, 20), days=30)

    assert [month_ for month_, _, _ in api.month_calls] == ["2026-12", "2027-01"]
    assert [t["price"] for t in tickets] == [6000, 8000]


@pytest.mark.asyncio
async def test_window_normalizes_fields_and_dedupes():
    api = _FakeMonthPricesAPI({
        "2026-04": [
            {"value": 9000, "airline": "SU", "number_of_changes": 1,
             "departure_at": "2026-04-10T10:00:00+03:00"},
            {"price": 9000, "airline": "SU", "transfers": 1,
             "departure_at": "2026-04-10T10:00:00+03:00"},
            {"price": 0, "airline": "DP", "departure_at": "2026-04-11T10:00:00+03:00"},
        ],
    })

    tickets = await api.search_cheapest_in_window("AER", "LED", start_date=date(2026, 4, 7), days=10)

    assert len(tickets) == 1
    assert tickets[0]["price"] == 9000
    assert tickets[0]["transfers"] == 1
    assert tickets[0]["origin"] == "AER" and tickets[0]["destination"] == "LED"
