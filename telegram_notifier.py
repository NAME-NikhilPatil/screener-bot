from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib import error, parse, request

from storage import get_storage

LOGGER = logging.getLogger(__name__)
DEFAULT_SUBSCRIBERS_FILE = Path("telegram_subscribers.json")
DEFAULT_OFFSET_FILE = Path("telegram_offset.json")


def load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


@dataclass(frozen=True, repr=False)
class TelegramNotifier:
    bot_token: str
    seed_chat_ids: tuple[str, ...] = ()
    subscribers_path: Path = DEFAULT_SUBSCRIBERS_FILE
    timeout_seconds: float = 15.0

    @classmethod
    def from_env(cls) -> "TelegramNotifier | None":
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not bot_token:
            return None

        notifier = cls(
            bot_token=bot_token,
            seed_chat_ids=_env_chat_ids(),
            subscribers_path=Path(_subscribers_name()),
            timeout_seconds=_telegram_timeout_seconds(),
        )
        notifier.seed_subscribers()
        return notifier

    def send_message(self, text: str) -> bool:
        recipients = get_all_telegram_recipients()
        if not recipients:
            LOGGER.warning("Telegram notification skipped because no recipients are registered")
            return False

        sent_any = False
        for chat_id in recipients:
            if self.send_to_chat(chat_id, text):
                sent_any = True

        return sent_any

    def send_to_chat(self, chat_id: str, text: str) -> bool:
        return _send_to_chat(self.bot_token, chat_id, text, self.timeout_seconds)

    def seed_subscribers(self) -> None:
        # Static recipients are intentionally read from env at send time. This
        # method remains for compatibility with the previous class API.
        return None

    def process_updates(self) -> int:
        return poll_telegram_subscribers_once()

    def get_subscribers(self) -> list[dict[str, str]]:
        return _load_dynamic_subscribers()

    def get_chat_candidates(self) -> list[dict[str, str]]:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or self.bot_token
        updates = _fetch_updates(bot_token, offset=None, timeout_seconds=self.timeout_seconds)
        return _chat_candidates_from_updates(updates)


def poll_telegram_subscribers_once() -> int:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        return 0

    try:
        offset_data = _load_offset_data()
        offset = _offset_value(offset_data)
        print(f"Telegram polling started with offset: {offset}", flush=True)
        updates = _fetch_updates(bot_token, offset=offset, timeout_seconds=_telegram_timeout_seconds())
        print(f"Telegram updates received: {len(updates)}", flush=True)
        if not updates:
            return 0

        subscribers = {item["chat_id"]: item for item in _load_dynamic_subscribers() if item.get("chat_id")}
        max_next_offset = offset
        changes = 0

        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                max_next_offset = max(max_next_offset or 0, update_id + 1)

            message = update.get("message") or update.get("channel_post") or update.get("edited_message")
            if not isinstance(message, dict):
                continue

            text = str(message.get("text", "")).strip()
            command = _message_command(text)
            chat = message.get("chat")
            if not isinstance(chat, dict) or "id" not in chat:
                continue

            chat_id = str(chat["id"])
            print(f"Telegram update message: update_id={update_id} chat_id={chat_id} text={text}", flush=True)
            if command == "/start":
                print(f"Processing /start for chat_id: {chat_id}", flush=True)
                is_new = chat_id not in subscribers
                existing = subscribers.get(chat_id, {})
                subscribers[chat_id] = {
                    "chat_id": chat_id,
                    "type": str(chat.get("type", "")),
                    "title": _chat_display_name(chat),
                    "source": "telegram_start",
                    "subscribed_at": existing.get("subscribed_at", _now_text()),
                    "updated_at": _now_text(),
                }
                changes += 1 if is_new else 0
                _send_to_chat(bot_token, chat_id, "Screener Bot alerts enabled for this chat.", _telegram_timeout_seconds())
            elif command == "/stop":
                print(f"Processing /stop for chat_id: {chat_id}", flush=True)
                if chat_id in subscribers:
                    subscribers.pop(chat_id)
                    changes += 1
                _send_to_chat(bot_token, chat_id, "Screener Bot alerts disabled for this chat.", _telegram_timeout_seconds())

        _save_dynamic_subscribers(sorted(subscribers.values(), key=lambda item: item["chat_id"]))
        print(f"Dynamic Telegram subscribers saved: {len(subscribers)}", flush=True)
        if max_next_offset is not None:
            _save_offset_data({"offset": max_next_offset})
            print(f"Telegram offset saved: {max_next_offset}", flush=True)
        return changes
    except Exception as exc:
        print(f"Telegram subscriber polling failed: {exc.__class__.__name__}: {exc}", flush=True)
        LOGGER.exception("Telegram subscriber polling failed")
        return 0


def get_all_telegram_recipients() -> list[str]:
    recipients: set[str] = set(_env_chat_ids())
    for subscriber in _load_dynamic_subscribers():
        chat_id = str(subscriber.get("chat_id", "")).strip()
        if chat_id:
            recipients.add(chat_id)
    return sorted(recipients)


def send_telegram_alert_to_all(message: str) -> bool:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        LOGGER.warning("Telegram notification skipped because TELEGRAM_BOT_TOKEN is not configured")
        return False

    recipients = get_all_telegram_recipients()
    if not recipients:
        LOGGER.warning("Telegram notification skipped because no recipients are registered")
        return False

    sent_any = False
    for chat_id in recipients:
        if _send_to_chat(bot_token, chat_id, message, _telegram_timeout_seconds()):
            sent_any = True
    return sent_any


def _send_to_chat(bot_token: str, chat_id: str, text: str, timeout_seconds: float) -> bool:
    payload = parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text[:4096],
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    telegram_request = request.Request(api_url, data=payload, method="POST")

    try:
        with request.urlopen(telegram_request, timeout=timeout_seconds) as response:
            if response.status == 200:
                return True
            LOGGER.warning("Telegram notification failed with HTTP %s", response.status)
    except error.HTTPError as exc:
        details = _telegram_error_details(exc)
        if details:
            LOGGER.warning("Telegram notification failed with HTTP %s: %s", exc.code, details)
        else:
            LOGGER.warning("Telegram notification failed with HTTP %s", exc.code)
    except error.URLError as exc:
        LOGGER.warning("Telegram notification failed: %s", exc.reason)
    except TimeoutError:
        LOGGER.warning("Telegram notification timed out")

    return False


def _fetch_updates(bot_token: str, offset: int | None, timeout_seconds: float) -> list[dict]:
    query = {"timeout": "0"}
    if offset is not None and offset > 0:
        query["offset"] = str(offset)

    api_url = f"https://api.telegram.org/bot{bot_token}/getUpdates?{parse.urlencode(query)}"
    telegram_request = request.Request(api_url, method="GET")

    try:
        with request.urlopen(telegram_request, timeout=timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except error.HTTPError as exc:
        details = _telegram_error_details(exc)
        if details:
            LOGGER.warning("Telegram getUpdates failed with HTTP %s: %s", exc.code, details)
            print(f"Telegram getUpdates request failed: HTTP {exc.code}: {details}", flush=True)
        else:
            LOGGER.warning("Telegram getUpdates failed with HTTP %s", exc.code)
            print(f"Telegram getUpdates request failed: HTTP {exc.code}", flush=True)
        return []
    except error.URLError as exc:
        LOGGER.warning("Telegram getUpdates failed: %s", exc.reason)
        print(f"Telegram getUpdates request failed: URLError: {exc.reason}", flush=True)
        return []
    except TimeoutError:
        LOGGER.warning("Telegram getUpdates timed out")
        print("Telegram getUpdates request failed: TimeoutError", flush=True)
        return []
    except json.JSONDecodeError:
        LOGGER.warning("Telegram getUpdates returned invalid JSON")
        print("Telegram getUpdates request failed: invalid JSON response", flush=True)
        return []

    updates = data.get("result", [])
    print(f"Telegram getUpdates response ok: {str(bool(data.get('ok'))).lower()}", flush=True)
    return updates if isinstance(updates, list) else []


def _load_dynamic_subscribers() -> list[dict[str, str]]:
    data = get_storage().load_json(_subscribers_name(), [])
    if isinstance(data, list):
        return [_normalize_subscriber(item) for item in data if isinstance(item, dict) and item.get("chat_id")]

    if isinstance(data, dict):
        subscribers = data.get("subscribers", data)
        if isinstance(subscribers, dict):
            return [
                _normalize_subscriber(item)
                for item in subscribers.values()
                if isinstance(item, dict) and item.get("chat_id")
            ]

    return []


def _save_dynamic_subscribers(subscribers: list[dict[str, str]]) -> None:
    get_storage().save_json(_subscribers_name(), subscribers)


def _load_offset_data() -> dict:
    data = get_storage().load_json(_offset_name(), {"offset": 0})
    return data if isinstance(data, dict) else {"offset": 0}


def _save_offset_data(data: dict) -> None:
    get_storage().save_json(_offset_name(), data)


def _normalize_subscriber(item: dict) -> dict[str, str]:
    return {
        "chat_id": str(item.get("chat_id", "")).strip(),
        "type": str(item.get("type", "")),
        "title": str(item.get("title", "")),
        "source": str(item.get("source", "telegram_start")),
        "subscribed_at": str(item.get("subscribed_at", "")),
        "updated_at": str(item.get("updated_at", "")),
    }


def _telegram_error_details(exc: error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        return ""

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body[:200]

    description = data.get("description")
    return str(description)[:200] if description else ""


def _chat_candidates_from_updates(updates: list[dict]) -> list[dict[str, str]]:
    candidates: dict[str, dict[str, str]] = {}
    for update in updates:
        message = update.get("message") or update.get("channel_post") or update.get("edited_message")
        if not isinstance(message, dict):
            continue

        chat = message.get("chat")
        if not isinstance(chat, dict) or "id" not in chat:
            continue

        chat_id = str(chat["id"])
        candidates[chat_id] = {
            "chat_id": chat_id,
            "type": str(chat.get("type", "")),
            "title": _chat_display_name(chat),
        }

    return list(candidates.values())


def _chat_display_name(chat: dict) -> str:
    if chat.get("title"):
        return str(chat["title"])

    name_parts = [chat.get("first_name"), chat.get("last_name")]
    display_name = " ".join(str(part) for part in name_parts if part)
    return display_name or str(chat.get("username", ""))


def _env_chat_ids() -> tuple[str, ...]:
    raw_values = [os.getenv("TELEGRAM_CHAT_ID", ""), os.getenv("TELEGRAM_CHAT_IDS", "")]
    chat_ids: list[str] = []
    for raw_value in raw_values:
        for chat_id in raw_value.replace(";", ",").split(","):
            chat_id = chat_id.strip()
            if chat_id and chat_id not in chat_ids:
                chat_ids.append(chat_id)
    return tuple(chat_ids)


def _message_command(text: str) -> str:
    if not text.startswith("/"):
        return ""
    command = text.split(maxsplit=1)[0]
    return command.split("@", 1)[0].lower()


def _offset_value(data: dict) -> int:
    offset = data.get("offset", 0)
    return offset if isinstance(offset, int) and offset >= 0 else 0


def _subscribers_name() -> str:
    return os.getenv("SUBSCRIBERS_BLOB_NAME", os.getenv("TELEGRAM_SUBSCRIBERS_FILE", str(DEFAULT_SUBSCRIBERS_FILE)))


def _offset_name() -> str:
    return os.getenv("TELEGRAM_OFFSET_BLOB_NAME", str(DEFAULT_OFFSET_FILE))


def _telegram_timeout_seconds() -> float:
    try:
        return float(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "15"))
    except ValueError:
        return 15.0


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
