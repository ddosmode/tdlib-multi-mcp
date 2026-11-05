# TODO — tdlib-multi-mcp

Статус задач разработки. Обновляется агентом при каждом прогрессе.

## ✅ Завершено

- [x] Создание репозитория GitHub `ddosmode/tdlib-multi-mcp`
- [x] Структура каталогов проекта
- [x] `README.md` с архитектурой, инструкциями и таблицей MCP-инструментов
- [x] `AGENTS.md` — регламент для всех AI-агентов
- [x] `.gitignore` — исключение секретов, venv, БД, логов
- [x] `requirements.txt` — зависимости

## 🚧 В работе

- [ ] `config.py` — загрузка конфигурации
- [ ] `db/models.py` — SQLite схема
- [ ] `db/database.py` — CRUD операции

## 📋 Запланировано

### Этап 1: Основа (config, db, fingerprints)
- [ ] `core/fingerprints.py` — генератор device profiles (20+ профилей Android/iOS/Desktop)
- [ ] `core/session_manager.py` — сериализатор TDLib dir ↔ Base64 ↔ RAM-диск
- [ ] `core/tdlib_client.py` — обёртка над aiotdlib.Client

### Этап 2: Авторизация
- [ ] `auth/phone_auth.py` — авторизация по телефону + код + 2FA
- [ ] `auth/qr_auth.py` — авторизация через QR-код
- [ ] `auth/session_import.py` — импорт из Telethon/Pyrogram StringSession
- [ ] `auth/session_export.py` — экспорт TDLib → Telethon/Pyrogram StringSession

### Этап 3: Прокси
- [ ] `proxy/collector.py` — сбор SOCKS5-прокси
- [ ] `proxy/validator.py` — изолированный чекер
- [ ] `proxy/rotator.py` — управление пулом

### Этап 4: MCP-сервер
- [ ] `server.py` — 15 MCP-инструментов, stdio transport

### Этап 5: Тесты и интеграция
- [ ] `tests/test_fingerprints.py`
- [ ] `tests/test_session_base64.py`
- [ ] `tests/test_proxy_isolated.py`
- [ ] `tests/test_auth_modes.py`
- [ ] `tests/test_mcp_tools.py`
- [ ] Регистрация в `~/.agents/mcp/servers.json` через `mcp-hub add`
- [ ] `mcp-hub sync` и проверка `agy mcp list` / `kilo mcp list`
