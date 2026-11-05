# tdlib-multi-mcp — AGENTS.md

Этот файл — обязательное чтение для любого AI-агента (Antigravity, Kilo, Codex, Claude),
работающего с данным проектом.

## Назначение проекта

MCP-сервер (`stdio` JSON-RPC, спецификация `2026-07-28`) для управления несколькими
аккаунтами Telegram через официальную библиотеку **TDLib**.

## Структура каталогов

```
~/.agents/mcp/telegram-mcp/
├── server.py          # Точка входа MCP, регистрация всех 15 инструментов
├── config.py          # Пути, таймауты, загрузка api_id/api_hash из config.env
├── requirements.txt   # Python-зависимости
├── AGENTS.md          # Этот файл
├── TODO.md            # Трекер задач и статус
│
├── core/
│   ├── AGENTS.md      # Инструкции по ядру TDLib
│   ├── tdlib_client.py      # Обёртка над aiotdlib.Client
│   ├── session_manager.py   # Упаковка TDLib dir ↔ Base64-строка ↔ RAM-диск
│   └── fingerprints.py      # Генератор и хранилище device profiles
│
├── auth/
│   ├── AGENTS.md      # Инструкции по режимам авторизации
│   ├── phone_auth.py        # Авторизация по номеру телефона + код + 2FA
│   ├── qr_auth.py           # Авторизация через QR-код
│   ├── session_import.py    # Импорт из Telethon / Pyrogram StringSession
│   └── session_export.py    # Экспорт TDLib → Telethon / Pyrogram StringSession
│
├── proxy/
│   ├── AGENTS.md      # Инструкции по прокси-ротатору
│   ├── collector.py         # Сбор SOCKS5-прокси из открытых источников
│   ├── validator.py         # Изолированный чекер (БЕЗ Telegram!)
│   └── rotator.py           # Управление пулом и ротация
│
├── db/
│   ├── models.py            # SQLite схема (accounts, proxies, logs)
│   └── database.py          # CRUD-операции с thread-safe блокировками
│
└── tests/
    ├── AGENTS.md      # Инструкции по запуску тестов
    ├── test_fingerprints.py
    ├── test_session_base64.py
    ├── test_proxy_isolated.py
    ├── test_auth_modes.py
    └── test_mcp_tools.py
```

## Ключевые принципы

### Безопасность
- `api_id` и `api_hash` **никогда** не хардкодятся, не логируются, не выводятся.
  Загружаются исключительно из `~/.config/telegram-mcp/config.env` (права 600).
- Сессии распаковываются **только** в RAM-диск `/dev/shm/tg_mcp/` и очищаются при завершении.
- Прокси проверяются **изолированно** — до добавления к аккаунту.

### Хранение сессий
- TDLib хранит состояние в директории на диске.
- Мы упаковываем эту директорию: `tar+gzip` → `base64` → поле `session_blob TEXT` в SQLite.
- При запуске: Base64 → распаковка → `/dev/shm/tg_mcp/<account_id>/`.
- После завершения сессии: директория упаковывается обратно и обновляется в БД.

### Device Fingerprints
- Каждый аккаунт получает уникальный, **неизменяемый** профиль устройства.
- Профиль фиксируется при первом добавлении и хранится в `accounts.db`.

### Прокси
- Сбор из открытых источников (`proxmint`, `monosans`, `proxifly`).
- Изолированный чекер проверяет прокси через `api.ipify.org` **без** активации сессии Telegram.
- При падении прокси — автоматическая замена из пула.

## Команды запуска

```bash
cd ~/.agents/mcp/telegram-mcp
source venv/bin/activate

# Запуск сервера (обычно запускается через mcp-hub)
python server.py

# Тесты
pytest tests/ -v

# Синтаксическая проверка всего кода
python -m py_compile server.py config.py
python -m py_compile core/*.py auth/*.py proxy/*.py db/*.py
```

## TODO.md

Всегда обновляй `TODO.md` при добавлении или завершении задачи.
Не оставляй задачи со статусом "в работе" без пометки.
