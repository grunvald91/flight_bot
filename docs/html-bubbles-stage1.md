# HTML bubbles — Stage 1: international indicative browse

Code: bot/widgets/browse.py and browse.html. Docs:
- https://github.com/TelegramMessenger/widget-sandbox/blob/main/docs/tgw-format.md
- https://github.com/TelegramMessenger/widget-sandbox/blob/main/docs/sdk-v1.md

## Status and safety

Off by default. Do not enable on the main production bot before verifying Bot API 10.4 support and running a live staging test.

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

A separate staging bot token is needed to test a real HTML bubble. Use a private chat:
1. Start the bot, choose "⚡ Дешёвые за границу" and complete origin/country/date.
2. Compare all destinations to the legacy listing, including pagination.
3. Toggle "Без пересадок" and confirm no unknown-transfer fare appears as nonstop.
4. Tap a destination and verify the visa source and checked Tutu offer.
5. Forward the bubble: its first-page public snapshot must be harmless; private search requests must be unavailable.
6. Test a Telegram client without widget support using the "Обычная выдача" button.
7. Restart the bot and ensure archived results still work (especially direct fare metadata).

## API limitations

- HTML <= 32 KB source, <= 4 KB zlib(level 9).
- Widget state/answers <= 1024 UTF-8 bytes; request <= 64 bytes.
- We do not use shared widgets or automatic background queries in Stage 1.
- aiogram 3 currently has no native 10.4 widget model. The opt-in dispatcher intercepts unknown update fields without changing existing handlers.
- A production rollout requires checking official Bot API availability and deployment configuration. The feature flag defaults to off.
