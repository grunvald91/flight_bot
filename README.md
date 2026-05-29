# Flight Bot — поиск авиабилетов через Aviasales

Телеграм-бот для поиска авиабилетов и слежения за ценой через Travelpayouts API (Aviasales).

**Бот:** [@a_ticketbot](https://t.me/a_ticketbot)

## Возможности

- 🔍 Inline-поиск: `@a_ticketbot Москва-Стамбул 15.03`
- ⭐ Избранные маршруты
- 🔔 Слежение за ценой с настройкой минимального снижения per-подписка
- 🔕 Тихий режим — можно глушить уведомления для одной подписки, оставив слежение
- 📜 История поисков
- 🛠 Админка со статистикой использования (пользователи, подписки, топ маршрутов)

## Стек

- Python 3.11+
- aiogram 3
- SQLAlchemy 2 (async) + asyncpg
- APScheduler
- PostgreSQL

## Локальный запуск

```bash
cp .env.example .env
# заполнить BOT_TOKEN, TRAVELPAYOUTS_TOKEN, DB_*
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python -m bot.main
```

## Деплой на сервер

В проде — systemd unit `flight_bot.service`. Автомиграция новых колонок выполняется при старте через `init_db()`.

```bash
systemctl restart flight-bot
journalctl -u flight-bot -f
```

## Структура

```
bot/
  handlers/      # роутеры: common, search, inline_mode, favorites, subscriptions, settings, admin, stats, history
  keyboards/     # инлайн-клавиатуры
  services/      # aviasales_api, city_resolver, scheduler, notifications
  utils/         # парсеры, форматтеры
  middlewares/   # логирование запросов
database/
  models.py      # SQLAlchemy
  crud.py        # запросы
  database.py    # engine + автомиграция
data/            # справочники городов и авиакомпаний
tests/           # unit-тесты парсера и форматтеров
```

## Админка

Доступ — по `telegram_id` из env `ADMIN_IDS` (csv, по умолчанию владелец).

Команда `/admin` или кнопка «🛠 Админка» в главном меню. Разделы:
- 📊 Общая сводка
- 👥 Статистика пользователей
- 👤 Список всех пользователей с карточкой по клику
- 🔔 Подписки + топ маршрутов
- ⭐ Избранное + топ маршрутов
- 🔥 Топ маршрутов в запросах
- 🏆 Топ юзеров по числу запросов
- 🆕 Лента последних запросов

## Лицензия

Личный проект.
