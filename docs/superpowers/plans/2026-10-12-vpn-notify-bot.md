# VPN Notify Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Реализовать и проверить локальный Telegram-бот с необязательным прокси.
**Architecture:** Один процесс: aiogram long polling и ежедневная фоновая рассылка. SQLite хранит серверы, версии и успешные доставки; черновики живут в памяти.
**Tech Stack:** Python 3.14 локально (минимум 3.11), uv, aiogram 3, python-dotenv, aiohttp-socks, tzdata, unittest.
**Spec:** ../specs/2026-10-12-vpn-notify-bot-design.md

## Global Constraints

- Настройки `.env`: `12:00`, `Europe/Moscow`, `REMIND_DAYS=7`, просрочка включена.
- Только разрешённые пользователи в личных чатах; остальные полностью игнорируются.
- Все кнопки inline. Изменение срока происходит только после подтверждения.
- Версии записей защищают совместную работу; доставку учитываем по получателю и дню.
- `PROXY_URL` пустой на сервере, локально `http://127.0.0.1:10808`.
- Не публиковать токен, `.env` и базу. Один процесс на токен и базу.
- Пользователь явно попросил немедленную реализацию; выполняем в текущем каталоге на ветке `codex/implement-bot`, без дополнительного согласования плана/создания worktree.

## Review Focus

- Старое подтверждение не сохраняет новый черновик того же администратора.
- Ошибка отправки одному администратору не блокирует доставку второму.
- HTML в названии не превращается в разметку; не-HTTP сайт отвергается.
- Переход через полночь и перезапуск не теряют ежедневное напоминание.
- Недоступный прокси и неверный токен дают ошибку без вывода секретов.

### Task 1: Конфигурация и хранилище

**Files:** `config.py`, `storage.py`, `tests/test_core.py`, `.env.example`, `.gitignore`, `pyproject.toml`, `uv.lock`.
**Interfaces:** `Settings.load(base: Path) -> Settings`; `validate_field(field: str, value: str) -> str`; `Storage(path)` с `add(fields)`, `get(id)`, `list()`, `update(id, version, fields)`, `delete(id, version)`, `delivered(id, end, admin, day)`, `mark_delivered(...)`.

- [x] Написать проверки неверной конфигурации, прокси, дат/URL, сохранения после открытия новой SQLite-сессии и конфликтов версий; увидеть отсутствие модулей.
- [x] Реализовать конфигурацию, валидацию и атомарные SQL-операции; повторить проверки, ожидается PASS.
- [x] Сохранить результат в Git без секретов.

### Task 2: Напоминания

**Files:** `reminders.py`, `tests/test_reminders.py`.
**Interfaces:** Использует Settings и Storage; `reminder_keyboard(server, today)`, `server_text(server)`, `Notifier(bot, storage, settings).tick(now)` и `.run()`.

- [x] Написать проверки окна 7 дней, просрочки, местного времени, повторного запуска, новых серверов после 12:00 и частичной ошибки отправки; увидеть отсутствие reminders.
- [x] Реализовать рассылку после местного времени с персистентным учётом доставки и ограничением запросов; вся suite должна пройти.
- [x] Сохранить результат в Git.

### Task 3: Диалоги и запуск

**Files:** `bot.py`, `main.py`, `tests/test_bot.py`, `README.md`.
**Interfaces:** `create_dispatcher(storage, settings) -> Dispatcher`; `main.py` загружает конфигурацию, создаёт AiohttpSession(proxy=...), запускает polling и Notifier.

- [x] Написать интеграционные тесты через реальный Dispatcher с тестовой Telegram-сессией: запрет доступа, добавление, изменение, отмена продления, сохранение и конфликт версий, подтверждение удаления, старые кнопки подтверждения; увидеть FAIL до реализации.
- [x] Реализовать inline-диалоги с отдельным случайным ID черновика, редактирование полей и сохранение; выполнить всю suite, ожидается PASS.
- [x] Добавить `--check` для проверки конфигурации и getMe без рассылки/изменения webhook. Проверить реальную сессию через локальный прокси.
- [ ] Написать README для Windows и Linux, прямого подключения и резервного копирования. Выполнить `uv pip check`, `compileall`, все тесты и проверку Git на секреты.
- [ ] Выполнить независимый финальный review и исправить существенные находки с RED→GREEN; сохранить код в Git.
