# Mail Forwarder to Telegram

Это приложение регулярно проверяет один почтовый ящик по IMAP и пересылает новые (UNSEEN) письма в Telegram через вашего бота. Запускать нужно через `main.py`, все секреты передаются через переменные окружения.

## Переменные окружения (секреты)

- `IMAP_HOST` — IMAP-хост.
- `IMAP_USERNAME` — логин (обычно полный email).
- `IMAP_PASSWORD` — пароль (часто это app password/пароль приложения).
- `IMAP_PORT` — порт (по умолчанию 993).
- `IMAP_MAILBOX` — папка (по умолчанию `INBOX`).
- `IMAP_USE_SSL` — использовать SSL (по умолчанию `true`).
- `TELEGRAM_BOT_TOKEN` — токен бота.
- `TELEGRAM_CHAT_ID` — ID чата, куда слать сообщения.
- `DUPLICATE_FROM_EMAIL` — если поле `Forwarded from` совпадет с этим email, сообщение продублируется в личку.
- `TELEGRAM_DUPLICATE_CHAT_ID` — ID личного Telegram-чата для дублирования (используется только вместе с `DUPLICATE_FROM_EMAIL`).
- `POLL_INTERVAL` — интервал проверки в секундах (по умолчанию 60).
- `LOG_LEVEL` — уровень логов (например, `INFO`).

## Пример запуска

```bash
export IMAP_HOST="imap.example.com"
export IMAP_USERNAME="inbox@example.com"
export IMAP_PASSWORD="super-secret"
export IMAP_PORT="993"
export IMAP_MAILBOX="INBOX"
export IMAP_USE_SSL="true"

export TELEGRAM_BOT_TOKEN="123456:ABCDEF"
export TELEGRAM_CHAT_ID="123456789"

# опционально: дублировать письма конкретного alias/email в личку
export DUPLICATE_FROM_EMAIL="forwarded-alias@example.com"
export TELEGRAM_DUPLICATE_CHAT_ID="987654321"

python main.py
```

## Как привязать почту

1. Узнайте IMAP-адрес и порт у вашего почтового провайдера (например, `imap.gmail.com`, `imap.yandex.ru`).
2. Создайте пароль приложения (app password), если почтовый сервис требует отдельный пароль для IMAP.
3. Заполните переменные `IMAP_HOST`, `IMAP_USERNAME`, `IMAP_PASSWORD` и при необходимости `IMAP_PORT`.
4. Запустите `python main.py`. В Telegram будут приходить сообщения с полем `From`, где указана почта отправителя.
