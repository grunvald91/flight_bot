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
