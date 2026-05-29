import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _parse(path: str) -> ast.Module:
    return ast.parse((ROOT / path).read_text(encoding="utf-8"))


def test_main_menu_contains_settings_button():
    text = (ROOT / "bot/keyboards/common_kb.py").read_text(encoding="utf-8")
    assert "menu:settings" in text


def test_settings_has_notifications_toggle():
    text = (ROOT / "bot/handlers/settings.py").read_text(encoding="utf-8")
    assert "settings:notifications" in text


def test_settings_parser_function_exists():
    module = _parse("bot/handlers/settings.py")
    fn_names = [node.name for node in module.body if isinstance(node, ast.FunctionDef)]
    assert "parse_min_price_drop_input" in fn_names


def test_remove_subscription_requires_owner_id():
    module = _parse("database/crud.py")
    target = None
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "remove_subscription":
            target = node
            break

    assert target is not None
    args = [arg.arg for arg in target.args.args]
    assert args == ["sub_id", "telegram_id"]


def test_user_settings_has_notifications_enabled_field():
    text = (ROOT / "database/models.py").read_text(encoding="utf-8")
    assert "notifications_enabled" in text


def test_scheduler_checks_notifications_enabled():
    text = (ROOT / "bot/services/scheduler.py").read_text(encoding="utf-8")
    assert "notifications_enabled" in text
    assert "if should_notify and notifications_enabled" in text


def test_subscription_keyboard_has_settings_shortcut():
    text = (ROOT / "bot/keyboards/common_kb.py").read_text(encoding="utf-8")
    assert 'callback_data="menu:settings"' in text


def test_subscriptions_screen_has_settings_controls():
    text = (ROOT / "bot/handlers/subscriptions.py").read_text(encoding="utf-8")
    assert "_subscriptions_header_keyboard" in text
    assert "submgr:open" in text


def test_subscription_management_screen_exists():
    text = (ROOT / "bot/handlers/subscriptions.py").read_text(encoding="utf-8")
    assert "submgr:open" in text
    assert "submgr:pause_all" in text
    assert "submgr:resume_all" in text
    assert "submgr:notifications" in text
    assert "submgr:direct" in text
    assert "submgr:min_drop" in text


def test_subscription_card_has_pause_resume_controls():
    text = (ROOT / "bot/handlers/subscriptions.py").read_text(encoding="utf-8")
    assert "sub:pause:" in text
    assert "sub:resume:" in text


def test_price_subscription_model_has_pause_flag():
    text = (ROOT / "database/models.py").read_text(encoding="utf-8")
    assert "is_paused" in text


def test_database_migration_adds_pause_flag():
    text = (ROOT / "database/database.py").read_text(encoding="utf-8")
    assert "is_paused" in text


def test_crud_has_pause_controls():
    module = _parse("database/crud.py")
    fn_names = [node.name for node in module.body if isinstance(node, ast.AsyncFunctionDef)]
    assert "pause_subscription" in fn_names
    assert "pause_all_subscriptions" in fn_names


def test_common_keyboard_labels_are_readable():
    text = (ROOT / "bot/keyboards/common_kb.py").read_text(encoding="utf-8")
    assert "Купить на Aviasales" in text
    assert "В избранное" in text
    assert "Следить за ценой" in text
    assert "Повторить поиск" in text
    assert "Искать билеты" in text
    assert "Удалить" in text
    assert "Показать с пересадками" in text
