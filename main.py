import html
import logging
import os
import time
import imaplib
import io
import re
import ssl
import uuid
from dataclasses import dataclass
from email import message_from_bytes
from email.header import decode_header
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from typing import Iterable
import urllib.request
import urllib.parse


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


class _TelegramHtmlExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "head"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag in {"br", "p", "div", "li", "tr", "table", "ul", "ol"}:
            self._chunks.append("\n")
        if tag == "a":
            href = ""
            for key, value in attrs:
                if key.lower() == "href" and value:
                    href = value
                    break
            if href:
                self._chunks.append(f'<a href="{html.escape(href, quote=True)}">')

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head"}:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag in {"p", "div", "li", "tr", "table", "ul", "ol"}:
            self._chunks.append("\n")
        if tag == "a":
            self._chunks.append("</a>")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if data:
            self._chunks.append(html.escape(html.unescape(data)))

    def get_text(self) -> str:
        return "".join(self._chunks)


def _normalize_whitespace(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    normalized = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", normalized)


def _html_to_telegram_text(source: str) -> str:
    parser = _TelegramHtmlExtractor()
    parser.feed(source)
    return _normalize_whitespace(parser.get_text())


def _decode_payload(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _extract_body(message) -> tuple[str, bool]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disposition = part.get("Content-Disposition", "")
            if "attachment" in disposition.lower():
                continue
            content_type = part.get_content_type()
            if content_type == "text/plain":
                text = _decode_payload(part)
                if text.strip():
                    plain_parts.append(text)
            elif content_type == "text/html":
                text = _decode_payload(part)
                if text.strip():
                    html_parts.append(text)
    else:
        content_type = message.get_content_type()
        if content_type == "text/plain":
            plain_parts.append(_decode_payload(message))
        elif content_type == "text/html":
            html_parts.append(_decode_payload(message))

    if plain_parts:
        body = "\n".join(part.strip() for part in plain_parts if part.strip())
        return body, False
    if html_parts:
        html_body = "\n".join(html_parts)
        return _html_to_telegram_text(html_body), True
    return "", False


def _format_plain_text_for_telegram(text: str) -> str:
    pattern = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
    result: list[str] = []
    last_end = 0
    for match in pattern.finditer(text):
        result.append(html.escape(text[last_end:match.start()]))
        link_text = html.escape(match.group(1))
        link_url = html.escape(match.group(2), quote=True)
        result.append(f'<a href="{link_url}">{link_text}</a>')
        last_end = match.end()
    result.append(html.escape(text[last_end:]))
    return "".join(result)


def _extract_attachments(message) -> list[tuple[str, str, bytes]]:
    attachments: list[tuple[str, str, bytes]] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename()
        disposition = part.get("Content-Disposition", "")
        if not filename and "attachment" not in disposition.lower():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        decoded_filename = _decode_header_value(filename) if filename else "attachment"
        content_type = part.get_content_type() or "application/octet-stream"
        attachments.append((decoded_filename, content_type, payload))
    return attachments


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
    subject = html.escape(_decode_header_value(message.get("Subject")))
    from_header = html.escape(_decode_header_value(message.get("From")))
    to_header = _decode_header_value(message.get("To"))
    date_header = html.escape(_decode_header_value(message.get("Date")))
    body, is_html = _extract_body(message)
    body_lines = body.strip().splitlines()
    body_preview = "\n".join(body_lines[:12])
    if body_preview:
        body_preview = (
            body_preview if is_html else _format_plain_text_for_telegram(body_preview)
        )
    forwarded_from = _extract_recipient_email(to_header) or mailbox_username

    summary_lines = [
        "📬 <b>New message</b>",
        f"<b>Forwarded from:</b> {html.escape(forwarded_from)}",
        f"<b>From:</b> {from_header}",
        f"<b>Date:</b> {date_header}",
        f"<b>Subject:</b> {subject}",
    ]
    if body_preview:
        summary_lines.append("")
        summary_lines.append(body_preview)
    return "\n".join(summary_lines)


def _send_telegram_message(telegram: TelegramConfig, text: str) -> None:
    url = f"https://api.telegram.org/bot{telegram.bot_token}/sendMessage"
    payload = urllib.parse.urlencode(
        {"chat_id": telegram.chat_id, "text": text, "parse_mode": "HTML"}
    ).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"Telegram API returned status {response.status}")


def _send_telegram_document(
    telegram: TelegramConfig, filename: str, content_type: str, data: bytes
) -> None:
    url = f"https://api.telegram.org/bot{telegram.bot_token}/sendDocument"
    boundary = f"----mforwarder{uuid.uuid4().hex}"
    body = io.BytesIO()

    body.write(f"--{boundary}\r\n".encode("utf-8"))
    body.write(b'Content-Disposition: form-data; name="chat_id"\r\n\r\n')
    body.write(str(telegram.chat_id).encode("utf-8"))
    body.write(b"\r\n")

    body.write(f"--{boundary}\r\n".encode("utf-8"))
    disposition = f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
    body.write(disposition.encode("utf-8"))
    body.write(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
    body.write(data)
    body.write(b"\r\n")

    body.write(f"--{boundary}--\r\n".encode("utf-8"))
    request = urllib.request.Request(url, data=body.getvalue(), method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(request, timeout=30) as response:
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
        message = message_from_bytes(message_bytes)
        attachments = _extract_attachments(message)
        for filename, content_type, data in attachments:
            try:
                _send_telegram_document(telegram, filename, content_type, data)
            except Exception:
                LOGGER.exception("Failed to send attachment %s", filename)
        mark_seen(config, uid)
        count += 1
    return count


def run_loop(config: AppConfig) -> None:
    while True:
        try:
            total = process_mailbox(config.mailbox, config.telegram)
        except Exception:
            LOGGER.exception("Failed to process mailbox")
            total = 0
        LOGGER.info("Processed %s new messages", total)
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
