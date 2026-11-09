# HTML bubbles — Stage 1: international indicative browse

Code: bot/widgets/browse.py and browse.html. Docs:
- https://github.com/TelegramMessenger/widget-sandbox/blob/main/docs/tgw-format.md
- https://github.com/TelegramMessenger/widget-sandbox/blob/main/docs/sdk-v1.md

## Telegram API availability — verified 2026-11-07

- The deployed production bot already has `FLIGHT_BOT_HTML_BUBBLES=1` and `FLIGHT_BOT_WIDGET_API=production` in its systemd drop-in. This enables attempts; it does **not** mean Telegram accepts widgets.
- Live deploy log of 2026-11-06: `sendWidget` returned **HTTP 404 Not Found**. See [GitHub Actions run 42221686724](https://github.com/grunvald91/flight_bot/actions/runs/42221686724). The ordinary listing remains operational.
- As checked 2026-11-07, the public [Telegram Bot API changelog](https://core.telegram.org/bots/api) still documents **10.3**, without `sendWidget`. HTML-widget beta parameters are documented in [widget-sandbox](https://github.com/TelegramMessenger/widget-sandbox/blob/main/docs/tgw-format.md), which explicitly reports a beta test on 2026-10-28.
- The [official Telegram test-environment guide](https://core.telegram.org/bots/features#dedicated-test-environment) states that a test account and a **separate bot created there** are required; a production bot token is not a substitute. The documented test endpoint is `https://api.telegram.org/bot<TOKEN>/test/METHOD_NAME`; widget-sandbox also reports a `/beta/bot<TOKEN>/test/METHOD_NAME` endpoint. Do not silently point the live production bot to either test server.
- A follow-up safe probe on 2026-11-07 could not read the bot token from the running process/configuration, so it **did not reach Telegram**. Those failed runs are not evidence that beta is unavailable. The temporary probe workflow was removed; production code and service settings were not changed.
- Release gate: a **real** `sendWidget` response containing a message ID from the intended environment, successful widget rendering in a compatible Telegram client, a delivered `widget_query`, and a valid `answerWidgetQuery`. Until all are verified, don't describe HTML bubbles as working in production.

## Status and safety

Bot API 10.4 was released on 2026-11-09. Official docs:
- https://core.telegram.org/bots/api#sendwidget
- https://core.telegram.org/bots/api#answerwidgetquery
- https://core.telegram.org/bots/widgets

Production scope: **private chats only**; Telegram protects widget messages from forwarding. The server's flight-bot systemd drop-in enables widgets, while the code keeps the feature flag off by default in other environments. Deploy verifies the server recognizes `sendWidget` (HTTP 400 for a deliberate call without `chat_id`); this alone does **not** verify actual delivery or UI interaction. Use `/widgettest` and `/browse` in Telegram for end-to-end checks.

```env
FLIGHT_BOT_HTML_BUBBLES=1
FLIGHT_BOT_WIDGET_API=production
```

Set `FLIGHT_BOT_WIDGET_API=beta` only on a separate staging bot. In beta mode aiogram polling and raw widget requests both use `https://api.telegram.org/beta/bot{token}/test/{method}`; never mix test and production endpoints. Do not enable two polling instances with the same token.

The new response to "⚡ Дешёвые за границу" includes:
- 4-item pages and stable per-offer index
- direct-only filtering with alternative direct fares persisted to SQLite
- private widget request, owned by the original search user
- Russian-entry rule lookup through the existing data source
- Tutu verification via the existing bot handler as a separate message
- ordinary Telegram button to open the legacy listing if widget media is unsupported or disabled

No prices are stated as bookable until checked at the ticket provider. Bot never purchases tickets on behalf of the user.

## Testing

```bash
python -m compileall -q bot tests
pytest -q tests/test_widget_browse.py tests/test_browse_direct_filter.py tests/test_entry_rules.py
```

A private Telegram chat with @a_ticketbot can test live HTML bubbles. First send `/widgettest`, then:
1. Start the bot, choose "⚡ Дешёвые за границу" and complete origin/country/date.
2. Compare all destinations to the legacy listing, including pagination.
3. Toggle "Без пересадок" and confirm no unknown-transfer fare appears as nonstop.
4. Tap a destination and verify the visa source and checked Tutu offer.
5. On the production API, widgets are protected from forwarding. Do not attempt forwarding as a required test; group and inline widget flows remain test-server-only.
6. Test a Telegram client without widget support using the "Обычная выдача" button.
7. Restart the bot and ensure archived results still work (especially direct fare metadata).

## API limitations

- HTML <= 32 KB source, <= 4 KB zlib(level 9).
- Widget state/answers <= 1024 UTF-8 bytes; request <= 64 bytes.
- We do not use shared widgets or automatic background queries in Stage 1.
- aiogram 3 currently has no native 10.4 widget model. The opt-in dispatcher intercepts unknown update fields without changing existing handlers.
- Production deployment checks the actual method's availability and rolls back on HTTP 404. The feature flag defaults to off.
