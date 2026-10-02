from aiogram.fsm.state import State, StatesGroup


class SearchStates(StatesGroup):
    choosing_origin = State()
    choosing_destination = State()
    choosing_date = State()
    showing_results = State()


class TutuSearchStates(StatesGroup):
    choosing_origin = State()
    choosing_destination = State()
    choosing_date = State()


class InternationalDiscoveryStates(StatesGroup):
    choosing_origin = State()
    choosing_date = State()

class FastBrowseStates(StatesGroup):
    choosing_origin = State()
    choosing_date = State()


class MultiTransportStates(StatesGroup):
    choosing_origin = State()
    choosing_destination = State()
    choosing_date = State()


class HotelSearchStates(StatesGroup):
    choosing_city = State()
    choosing_check_in = State()
    choosing_check_out = State()


class TripPlannerStates(StatesGroup):
    choosing_origin = State()
    choosing_destination = State()
    choosing_departure = State()
    choosing_return = State()
