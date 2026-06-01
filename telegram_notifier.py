from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib import error, parse, request

LOGGER = logging.getLogger(__name__)
DEFAULT_SUBSCRIBERS_FILE = Path("telegram_subscribers.json")


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
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> "TelegramNotifier | None":
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not bot_token:
            return None

        try:
            timeout_seconds = float(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "10"))
        except ValueError:
            timeout_seconds = 10.0

        subscribers_path = Path(os.getenv("TELEGRAM_SUBSCRIBERS_FILE", str(DEFAULT_SUBSCRIBERS_FILE)))
        notifier = cls(
            bot_token=bot_token,
            seed_chat_ids=_env_chat_ids(),
            subscribers_path=subscribers_path,
            timeout_seconds=timeout_seconds,
        )
        notifier.seed_subscribers()
        return notifier

    def send_message(self, text: str) -> bool:
        subscribers = self.get_subscribers()
        if not subscribers:
            LOGGER.warning("Telegram notification skipped because no subscribers are registered")
            return False

        sent_any = False
        for subscriber in subscribers:
            if self.send_to_chat(subscriber["chat_id"], text):
                sent_any = True

        return sent_any

    def send_to_chat(self, chat_id: str, text: str) -> bool:
        payload = parse.urlencode(
            {
                "chat_id": chat_id,
                "text": text[:4096],
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        telegram_request = request.Request(api_url, data=payload, method="POST")

        try:
            with request.urlopen(telegram_request, timeout=self.timeout_seconds) as response:
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

    def seed_subscribers(self) -> None:
        if not self.seed_chat_ids:
            return

        data = self._load_subscriber_data()
        subscribers = data.setdefault("subscribers", {})
        changed = False
        for chat_id in self.seed_chat_ids:
            if chat_id in subscribers:
                continue
            subscribers[chat_id] = {
                "chat_id": chat_id,
                "type": "",
                "title": "Configured chat",
                "source": "env",
                "subscribed_at": _now_text(),
            }
            changed = True

        if changed:
            self._save_subscriber_data(data)

    def process_updates(self) -> int:
        data = self._load_subscriber_data()
        offset = _next_update_offset(data.get("last_update_id"))
        updates = self._fetch_updates(offset=offset)
        if not updates:
            return 0

        subscribers = data.setdefault("subscribers", {})
        last_update_id = data.get("last_update_id")
        max_update_id = last_update_id if isinstance(last_update_id, int) else None
        changes = 0

        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                max_update_id = update_id if max_update_id is None else max(max_update_id, update_id)

            message = update.get("message") or update.get("channel_post") or update.get("edited_message")
            if not isinstance(message, dict):
                continue

            text = str(message.get("text", "")).strip()
            command = _message_command(text)
            chat = message.get("chat")
            if not isinstance(chat, dict) or "id" not in chat:
                continue

            chat_id = str(chat["id"])
            if command == "/start":
                is_new = chat_id not in subscribers
                subscribers[chat_id] = {
                    "chat_id": chat_id,
                    "type": str(chat.get("type", "")),
                    "title": _chat_display_name(chat),
                    "source": "telegram_start",
                    "subscribed_at": subscribers.get(chat_id, {}).get("subscribed_at", _now_text()),
                    "updated_at": _now_text(),
                }
                changes += 1 if is_new else 0
                self.send_to_chat(chat_id, "Screener Bot alerts enabled for this chat.")
            elif command == "/stop":
                if chat_id in subscribers:
                    subscribers.pop(chat_id)
                    changes += 1
                self.send_to_chat(chat_id, "Screener Bot alerts disabled for this chat.")

        if max_update_id is not None:
            data["last_update_id"] = max_update_id

        self._save_subscriber_data(data)
        return changes

    def get_subscribers(self) -> list[dict[str, str]]:
        data = self._load_subscriber_data()
        subscribers = data.get("subscribers", {})
        if not isinstance(subscribers, dict):
            return []
        return sorted(subscribers.values(), key=lambda item: str(item.get("chat_id", "")))

    def get_chat_candidates(self) -> list[dict[str, str]]:
        updates = self._fetch_updates(offset=None)
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

    def _fetch_updates(self, offset: int | None) -> list[dict]:
        query = {"timeout": "0"}
        if offset is not None:
            query["offset"] = str(offset)

        api_url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates?{parse.urlencode(query)}"
        telegram_request = request.Request(api_url, method="GET")

        try:
            with request.urlopen(telegram_request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8", errors="replace"))
        except error.HTTPError as exc:
            details = _telegram_error_details(exc)
            if details:
                LOGGER.warning("Telegram getUpdates failed with HTTP %s: %s", exc.code, details)
            else:
                LOGGER.warning("Telegram getUpdates failed with HTTP %s", exc.code)
            return []
        except error.URLError as exc:
            LOGGER.warning("Telegram getUpdates failed: %s", exc.reason)
            return []
        except TimeoutError:
            LOGGER.warning("Telegram getUpdates timed out")
            return []
        except json.JSONDecodeError:
            LOGGER.warning("Telegram getUpdates returned invalid JSON")
            return []

        updates = data.get("result", [])
        return updates if isinstance(updates, list) else []

    def _load_subscriber_data(self) -> dict:
        if not self.subscribers_path.exists():
            return {"last_update_id": None, "subscribers": {}}

        try:
            data = json.loads(self.subscribers_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Telegram subscribers file is invalid; starting with an empty subscriber list")
            return {"last_update_id": None, "subscribers": {}}

        if not isinstance(data, dict):
            return {"last_update_id": None, "subscribers": {}}
        if not isinstance(data.get("subscribers"), dict):
            data["subscribers"] = {}
        return data

    def _save_subscriber_data(self, data: dict) -> None:
        self.subscribers_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.subscribers_path.with_suffix(self.subscribers_path.suffix + ".tmp")
        temp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(self.subscribers_path)


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


def _next_update_offset(last_update_id: object) -> int | None:
    if isinstance(last_update_id, int):
        return last_update_id + 1
    return None


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
