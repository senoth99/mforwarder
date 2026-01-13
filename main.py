import json
import logging
import os
import time
import imaplib
import ssl
from dataclasses import dataclass
from email import message_from_bytes
from email.header import decode_header
from typing import Iterable
import urllib.request
import urllib.parse


@dataclass
class MailboxConfig:
    name: str
    host: str
    username: str
    password: str
    port: int = 993
    mailbox: str = "INBOX"
    use_ssl: bool = True


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str


@dataclass
class AppConfig:
    mailboxes: list[MailboxConfig]
    telegram: TelegramConfig
    poll_interval: int = 60


LOGGER = logging.getLogger("mforwarder")


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_config() -> AppConfig:
    mailboxes_raw = _require_env("MAILBOXES_JSON")
    telegram_token = _require_env("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = _require_env("TELEGRAM_CHAT_ID")
    poll_interval = int(os.getenv("POLL_INTERVAL", "60"))

    try:
        mailboxes_data = json.loads(mailboxes_raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("MAILBOXES_JSON must be valid JSON") from exc

    if not isinstance(mailboxes_data, list) or not mailboxes_data:
        raise RuntimeError("MAILBOXES_JSON must be a non-empty JSON array")

    mailboxes: list[MailboxConfig] = []
    for idx, entry in enumerate(mailboxes_data, start=1):
        if not isinstance(entry, dict):
            raise RuntimeError(f"Mailbox entry #{idx} must be a JSON object")
        try:
            mailboxes.append(
                MailboxConfig(
                    name=entry["name"],
                    host=entry["host"],
                    username=entry["username"],
                    password=entry["password"],
                    port=int(entry.get("port", 993)),
                    mailbox=entry.get("mailbox", "INBOX"),
                    use_ssl=bool(entry.get("use_ssl", True)),
                )
            )
        except KeyError as exc:
            raise RuntimeError(
                f"Mailbox entry #{idx} is missing required field: {exc.args[0]}"
            ) from exc

    return AppConfig(
        mailboxes=mailboxes,
        telegram=TelegramConfig(bot_token=telegram_token, chat_id=telegram_chat_id),
        poll_interval=poll_interval,
    )


def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    decoded_parts = decode_header(value)
    decoded_strings = []
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            decoded_strings.append(part.decode(encoding or "utf-8", errors="replace"))
        else:
            decoded_strings.append(part)
    return "".join(decoded_strings)


def _extract_text_payload(message) -> str:
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = part.get("Content-Disposition", "")
            if content_type == "text/plain" and "attachment" not in disposition.lower():
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return ""
    payload = message.get_payload(decode=True)
    if payload is None:
        return ""
    return payload.decode(message.get_content_charset() or "utf-8", errors="replace")


def _build_summary(mailbox: MailboxConfig, message_bytes: bytes) -> str:
    message = message_from_bytes(message_bytes)
    subject = _decode_header_value(message.get("Subject"))
    from_header = _decode_header_value(message.get("From"))
    date_header = _decode_header_value(message.get("Date"))
    body = _extract_text_payload(message)
    body_preview = "\n".join(body.strip().splitlines()[:12])

    summary_lines = [
        f"📬 {mailbox.name}",
        f"From: {from_header}",
        f"Date: {date_header}",
        f"Subject: {subject}",
    ]
    if body_preview:
        summary_lines.append("")
        summary_lines.append(body_preview)
    return "\n".join(summary_lines)


def _send_telegram_message(telegram: TelegramConfig, text: str) -> None:
    url = f"https://api.telegram.org/bot{telegram.bot_token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": telegram.chat_id, "text": text}).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"Telegram API returned status {response.status}")


def _connect_mailbox(config: MailboxConfig):
    if config.use_ssl:
        context = ssl.create_default_context()
        return imaplib.IMAP4_SSL(config.host, config.port, ssl_context=context)
    return imaplib.IMAP4(config.host, config.port)


def fetch_unseen_messages(config: MailboxConfig) -> Iterable[tuple[str, bytes]]:
    with _connect_mailbox(config) as client:
        client.login(config.username, config.password)
        client.select(config.mailbox)
        status, data = client.search(None, "UNSEEN")
        if status != "OK":
            LOGGER.warning("Failed to search mailbox %s", config.name)
            return []
        for uid in data[0].split():
            status, msg_data = client.fetch(uid, "(RFC822)")
            if status != "OK":
                LOGGER.warning("Failed to fetch message %s from %s", uid.decode(), config.name)
                continue
            yield uid.decode(), msg_data[0][1]


def mark_seen(config: MailboxConfig, message_uid: str) -> None:
    with _connect_mailbox(config) as client:
        client.login(config.username, config.password)
        client.select(config.mailbox)
        client.store(message_uid, "+FLAGS", "\\Seen")


def process_mailbox(config: MailboxConfig, telegram: TelegramConfig) -> int:
    count = 0
    for uid, message_bytes in fetch_unseen_messages(config):
        summary = _build_summary(config, message_bytes)
        _send_telegram_message(telegram, summary)
        mark_seen(config, uid)
        count += 1
    return count


def run_loop(config: AppConfig) -> None:
    while True:
        total = 0
        for mailbox in config.mailboxes:
            try:
                total += process_mailbox(mailbox, config.telegram)
            except Exception:
                LOGGER.exception("Failed to process mailbox %s", mailbox.name)
        LOGGER.info("Processed %s new messages", total)
        time.sleep(config.poll_interval)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    LOGGER.info("Loaded %s mailboxes", len(config.mailboxes))
    run_loop(config)


if __name__ == "__main__":
    main()
