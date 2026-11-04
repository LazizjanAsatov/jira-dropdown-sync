"""Our Telegram bot: ask the report bot for its Excel in the group, download it, post notes and questions.

Uses the plain Bot API over HTTPS (long polling), so no server or webhook is needed.
Our bot sends exactly one command per run and never answers other bots, so it can't loop.
Reading (getUpdates, getFile, the download) is retried on network blips and "slow down" answers;
writing a message is never retried, so a note can't be posted twice.
The bot token is part of every API URL, so it is scrubbed from every error message.
"""
import logging
import time
from collections.abc import Callable
from pathlib import Path

import requests

API_URL = "https://api.telegram.org"
READ_RETRIES = 3
MAX_BACKOFF = 30  # seconds between tries while waiting out an outage
# Telegram remembers this filter between calls and drops updates it excludes, so every call asks for both
ALLOWED_UPDATES = ["message", "callback_query"]
OLD_QUESTION = "This list is old. Use the newest one."

log = logging.getLogger("merchant_sync.telegram")


class TelegramError(RuntimeError):
    """Telegram refused a request, couldn't be reached, or the Excel never came."""


class _Transient(Exception):
    """A failure worth trying again: network blip, Telegram 5xx, or "too many requests"."""

    def __init__(self, problem: str, wait: float | None = None):
        super().__init__(problem)
        self.problem = problem
        self.wait = wait


class TelegramBot:
    def __init__(self, token: str, session: requests.Session | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 poll_seconds: int = 25):
        self._token = token
        self._session = session or requests.Session()
        self._clock = clock
        self._sleep = sleep
        self._poll_seconds = poll_seconds
        self._offset: int | None = None  # next update to read

    def request_file(self, chat_id: int, command: str, *, from_username: str, name_prefix: str,
                     wait_seconds: int, save_dir: Path) -> Path:
        """Write `command` in the chat, wait for the Excel `from_username` sends after it, save it."""
        self._skip_old_updates()
        sent = self.send_message(chat_id, command)
        document = self._wait_for_document(chat_id, from_username, sent["message_id"], name_prefix, wait_seconds)
        return self._download(document, Path(save_dir))

    def send_message(self, chat_id: int, text: str, buttons: list[tuple[str, str]] | None = None) -> dict:
        """Post a message; `buttons` are (label, answer) pairs shown under it in one row."""
        params = {"chat_id": chat_id, "text": text}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": [
                [{"text": label, "callback_data": answer} for label, answer in buttons]]}
        return self._call("sendMessage", **params)

    def edit_message(self, chat_id: int, message_id: int, text: str) -> dict | None:
        """Replace a message's text. Its buttons go away, so nobody can press them any more.

        Safe to repeat, so it's retried; "not modified" means an earlier try already went through.
        """
        try:
            return self._call("editMessageText", retry=True, chat_id=chat_id, message_id=message_id, text=text)
        except TelegramError as exc:
            if "message is not modified" in str(exc):
                return None
            raise

    def wait_for_press(self, chat_id: int, message_id: int, *, wait_seconds: int) -> tuple[str, str] | None:
        """Wait for the first button press under that message: (answer, who), or None if nobody pressed in time."""
        for update in self._updates(wait_seconds):
            press = update.get("callback_query")
            if not press:
                continue
            message = press.get("message") or {}
            if message.get("chat", {}).get("id") == chat_id and message.get("message_id") == message_id:
                self._answer_press(press, "Got it")
                return press.get("data", ""), _who(press.get("from") or {})
            self._answer_press(press, OLD_QUESTION)
        return None

    def _skip_old_updates(self) -> None:
        """Telegram forgets every update before the last one; we then start after that one too."""
        last = self._call("getUpdates", retry=True, offset=-1, timeout=0, allowed_updates=ALLOWED_UPDATES)
        self._offset = last[-1]["update_id"] + 1 if last else None

    def _wait_for_document(self, chat_id: int, from_username: str, after_message_id: int, name_prefix: str,
                           wait_seconds: int) -> dict:
        for update in self._updates(wait_seconds):
            if update.get("callback_query"):
                self._answer_press(update["callback_query"], OLD_QUESTION)
                continue
            document = _wanted_document(update.get("message"), chat_id, from_username, after_message_id,
                                        name_prefix)
            if document:
                return document
        raise TelegramError(f"no Excel from @{from_username} within {wait_seconds} s")

    def _updates(self, wait_seconds: int):
        """New updates, one by one, until the wait is over (long polling).

        Network outages, Telegram hiccups and another copy polling the same bot (409) are waited out
        until the deadline. If Telegram is still unreachable then, that's an error, not "nothing came".
        """
        deadline = self._clock() + wait_seconds
        failures, problem = 0, None
        while (remaining := deadline - self._clock()) > 0:
            params = {"timeout": max(1, min(self._poll_seconds, int(remaining))), "allowed_updates": ALLOWED_UPDATES}
            if self._offset is not None:
                params["offset"] = self._offset
            try:
                batch = self._call_once("getUpdates", params)
            except _Transient as exc:
                failures, problem = failures + 1, exc.problem
                if failures == 1:
                    log.warning("lost contact with Telegram (%s), waiting it out", exc.problem)
                pause = exc.wait if exc.wait is not None else 2 ** failures
                self._sleep(max(1, min(pause, MAX_BACKOFF, int(remaining))))
                continue
            if failures:
                log.warning("Telegram is back after %d failed tries", failures)
            failures, problem = 0, None
            for update in batch:
                self._offset = update["update_id"] + 1
                yield update
        if problem:
            raise TelegramError(f"Telegram was unreachable until the end of the wait: {problem}")

    def _answer_press(self, press: dict, text: str) -> None:
        """Stop the spinner on the pressed button; a failure here must not stop the run."""
        try:
            self._call("answerCallbackQuery", callback_query_id=press["id"], text=text)
        except TelegramError as exc:
            log.warning("couldn't answer a button press: %s", exc)

    def _download(self, document: dict, save_dir: Path) -> Path:
        file_path = self._call("getFile", retry=True, file_id=document["file_id"])["file_path"]
        content = self._retrying(lambda: self._fetch_file(file_path, document.get("file_name")))
        save_dir.mkdir(parents=True, exist_ok=True)
        target = save_dir / Path(document["file_name"]).name
        target.write_bytes(content)
        return target

    def _fetch_file(self, file_path: str, file_name: str | None) -> bytes:
        response = self._http("get", f"{API_URL}/file/bot{self._token}/{file_path}", timeout=60)
        if response.status_code == 200:
            return response.content
        problem = f"downloading {file_name!r} failed: HTTP {response.status_code}"
        if response.status_code == 429 or response.status_code >= 500:
            raise _Transient(problem)
        raise TelegramError(problem)

    def _call(self, method: str, *, retry: bool = False, **params):
        return self._retrying(lambda: self._call_once(method, params), attempts=1 + (READ_RETRIES if retry else 0))

    def _call_once(self, method: str, params: dict):
        response = self._http("post", f"{API_URL}/bot{self._token}/{method}", json=params,
                              timeout=self._poll_seconds + 15)
        try:
            data = response.json()
        except ValueError:
            data = None
        if data and data.get("ok"):
            return data["result"]
        data = data or {}
        status = data.get("error_code", response.status_code)
        problem = f"{method}: {data.get('description') or f'HTTP {response.status_code}, not a Bot API answer'}"
        if status in (409, 429) or status >= 500:  # 409: another copy is polling the same bot right now
            raise _Transient(problem, (data.get("parameters") or {}).get("retry_after"))
        raise TelegramError(problem)

    def _retrying(self, action: Callable, attempts: int = 1 + READ_RETRIES):
        for attempt in range(1, attempts + 1):
            try:
                return action()
            except _Transient as exc:
                if attempt == attempts:
                    raise TelegramError(exc.problem) from None
                self._sleep(exc.wait if exc.wait is not None else 2 ** attempt)
        raise AssertionError("unreachable")

    def _http(self, verb: str, url: str, **kwargs):
        try:
            return getattr(self._session, verb)(url, **kwargs)
        except requests.RequestException as exc:
            raise _Transient(f"Telegram request failed: {exc}".replace(self._token, "<token>")) from None


def _who(user: dict) -> str:
    if user.get("username"):
        return f"@{user['username']}"
    return " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part) or "someone"


def _wanted_document(message: dict | None, chat_id: int, from_username: str, after_message_id: int,
                     name_prefix: str) -> dict | None:
    if not message or message.get("chat", {}).get("id") != chat_id:
        return None
    if message.get("message_id", 0) <= after_message_id:
        return None
    sender = (message.get("from") or {}).get("username") or ""
    document = message.get("document")
    if sender.casefold() != from_username.casefold() or not document:
        return None
    name = (document.get("file_name") or "").casefold()
    if name.startswith(name_prefix.casefold()) and name.endswith(".xlsx"):
        return document
    log.warning("ignored a file from @%s named %r (expected %r…xlsx)", from_username,
                document.get("file_name"), name_prefix)
    return None
