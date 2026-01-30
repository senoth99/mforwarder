import logging
import os
import time
import imaplib
import ssl
from dataclasses import dataclass
from email import message_from_bytes
from email.header import decode_header
from email.utils import getaddresses, parseaddr
from typing import Iterable
import urllib.request
import urllib.parse
import urllib.error


@dataclass
class MailboxConfig:
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
    mailbox: MailboxConfig
    telegram: TelegramConfig
    poll_interval: int = 60


LOGGER = logging.getLogger("mforwarder")
SITE_CHECK_URL = "https://cashercollection.com"
SITE_CHECK_INTERVAL = 600
SITE_DOWN_MESSAGE = "САЙТ НЕДОСТУПЕН @ivanvoropaeff @makstut1"
SITE_UP_MESSAGE = "САЙТ СНОВА РАБОТАЕТ @ivanvoropaeff @makstut1"


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_config() -> AppConfig:
    imap_host = _require_env("IMAP_HOST")
    imap_username = _require_env("IMAP_USERNAME")
    imap_password = _require_env("IMAP_PASSWORD")
    telegram_token = _require_env("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = _require_env("TELEGRAM_CHAT_ID")
    poll_interval = int(os.getenv("POLL_INTERVAL", "60"))

    return AppConfig(
        mailbox=MailboxConfig(
            host=imap_host,
            username=imap_username,
            password=imap_password,
            port=int(os.getenv("IMAP_PORT", "993")),
            mailbox=os.getenv("IMAP_MAILBOX", "INBOX"),
            use_ssl=os.getenv("IMAP_USE_SSL", "true").lower() != "false",
        ),
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


def _extract_sender_email(from_header: str) -> str:
    _, address = parseaddr(from_header)
    return address


def _extract_recipient_email(to_header: str) -> str:
    addresses = getaddresses([to_header])
    if not addresses:
        return ""
    _, address = addresses[0]
    return address


def _build_summary(message_bytes: bytes, mailbox_username: str) -> str:
    message = message_from_bytes(message_bytes)
    subject = _decode_header_value(message.get("Subject"))
    from_header = _decode_header_value(message.get("From"))
    to_header = _decode_header_value(message.get("To"))
    date_header = _decode_header_value(message.get("Date"))
    body = _extract_text_payload(message)
    body_preview = "\n".join(body.strip().splitlines()[:12])
    forwarded_from = _extract_recipient_email(to_header) or mailbox_username

    summary_lines = [
        "📬 New message",
        f"Forwarded from: {forwarded_from}",
        f"From: {from_header}",
        f"To: {to_header}",
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


def _is_site_available(url: str, timeout: int = 10) -> bool:
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status < 400
    except urllib.error.HTTPError as exc:
        if exc.code == 405:
            try:
                with urllib.request.urlopen(url, timeout=timeout) as response:
                    return response.status < 400
            except Exception:
                return False
        return False
    except Exception:
        return False


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
            LOGGER.warning("Failed to search mailbox")
            return []
        for uid in data[0].split():
            status, msg_data = client.fetch(uid, "(RFC822)")
            if status != "OK":
                LOGGER.warning("Failed to fetch message %s", uid.decode())
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
        summary = _build_summary(message_bytes, config.username)
        _send_telegram_message(telegram, summary)
        mark_seen(config, uid)
        count += 1
    return count


def run_loop(config: AppConfig) -> None:
    last_site_check = 0.0
    last_site_available: bool | None = None
    while True:
        try:
            total = process_mailbox(config.mailbox, config.telegram)
        except Exception:
            LOGGER.exception("Failed to process mailbox")
            total = 0
        LOGGER.info("Processed %s new messages", total)
        now = time.monotonic()
        if now - last_site_check >= SITE_CHECK_INTERVAL:
            last_site_check = now
            is_available = _is_site_available(SITE_CHECK_URL)
            if not is_available:
                LOGGER.warning("Site unavailable: %s", SITE_CHECK_URL)
                if last_site_available is not False:
                    try:
                        _send_telegram_message(config.telegram, SITE_DOWN_MESSAGE)
                    except Exception:
                        LOGGER.exception("Failed to send site availability alert")
            elif last_site_available is False:
                LOGGER.info("Site available again: %s", SITE_CHECK_URL)
                try:
                    _send_telegram_message(config.telegram, SITE_UP_MESSAGE)
                except Exception:
                    LOGGER.exception("Failed to send site recovery alert")
            last_site_available = is_available
        time.sleep(config.poll_interval)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    LOGGER.info("Loaded mailbox %s", config.mailbox.username)
    run_loop(config)


if __name__ == "__main__":
    main()
