# Mail Forwarder to Telegram

Это приложение регулярно проверяет несколько почтовых ящиков по IMAP и пересылает новые (UNSEEN) письма в Telegram через вашего бота. Запускать нужно через `main.py`, все секреты передаются через переменные окружения.

## Переменные окружения (секреты)

- `MAILBOXES_JSON` — JSON-массив ящиков. Каждый элемент:
  - `name` — имя ящика для отображения.
  - `host` — IMAP-хост.
  - `username` — логин.
  - `password` — пароль.
  - `port` — порт (по умолчанию 993).
  - `mailbox` — папка (по умолчанию `INBOX`).
  - `use_ssl` — использовать SSL (по умолчанию `true`).
- `TELEGRAM_BOT_TOKEN` — токен бота.
- `TELEGRAM_CHAT_ID` — ID чата, куда слать сообщения.
- `POLL_INTERVAL` — интервал проверки в секундах (по умолчанию 60).
- `LOG_LEVEL` — уровень логов (например, `INFO`).

## Пример запуска

```bash
export MAILBOXES_JSON='[
  {
    "name": "Work",
    "host": "imap.example.com",
    "username": "work@example.com",
    "password": "super-secret",
    "port": 993,
    "mailbox": "INBOX",
    "use_ssl": true
  }
]'
export TELEGRAM_BOT_TOKEN="123456:ABCDEF"
export TELEGRAM_CHAT_ID="123456789"

python main.py
```
