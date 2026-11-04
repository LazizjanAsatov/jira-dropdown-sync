"""In-memory stand-ins for the Jira Cloud and Telegram Bot HTTP APIs.

They answer the same paths with the same JSON shapes as the real services,
keep state, and record every request so tests can check what was sent.
"""
from urllib.parse import urlparse

import requests


NOT_JSON = object()  # payload marker: the body is HTML, not JSON


class FakeResponse:
    def __init__(self, status_code: int, payload=None, content: bytes | None = None, headers: dict | None = None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        if payload is NOT_JSON:
            content = b"<html><body>502 Bad Gateway</body></html>"
        self.content = content if content is not None else (b"{}" if payload is not None else b"")
        self.text = self.content.decode(errors="replace") if payload is NOT_JSON else str(payload)

    def json(self):
        if self._payload is NOT_JSON:
            raise requests.JSONDecodeError("Expecting value", self.text, 0)
        return self._payload


def _scripted(queue: list):
    """Pop the next scripted outcome: an exception (even KeyboardInterrupt) is raised, a response is returned."""
    outcome = queue.pop(0)
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome


class FakeClock:
    """A monotonic clock that moves `step` seconds forward every time it's read."""

    def __init__(self, step: float = 30):
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class FakeJiraSession:
    """Jira Cloud REST v3: field search, field contexts and select-list options."""

    def __init__(self, options=(), fields=None, contexts=None):
        self.auth = None
        self.headers = {}
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []
        self.fail_with: tuple[int, str] | None = None        # every request fails
        self.fail_post_with: tuple[int, str] | None = None   # only adding options fails
        self.raise_error: Exception | None = None
        self.scripted: list = []                             # next outcomes, before normal answers
        self.post_reply: dict | None = None                  # POST answers this and creates nothing
        self.on_post = None                                  # callback() run when options are being added
        self.fields = fields if fields is not None else [_field("customfield_10050", "Merchant")]
        self.contexts = contexts if contexts is not None else {
            "customfield_10050": [_context("10150", "Default Configuration Scheme for Merchant", is_global=True)],
        }
        self.options = {("customfield_10050", "10150"): [
            {"id": str(10001 + i), "value": value, "disabled": disabled} for i, (value, disabled) in enumerate(_pairs(options))
        ]}
        self._next_id = 20001

    def request(self, method, url, params=None, json=None, timeout=None):
        path = urlparse(url).path
        self.calls.append((method, path, params, json))
        if self.raise_error:
            raise self.raise_error
        if self.scripted:
            return _scripted(self.scripted)
        if method == "POST" and self.on_post:
            self.on_post()
        if method == "POST" and self.post_reply is not None:
            return FakeResponse(200, self.post_reply)
        failure = self.fail_with or (self.fail_post_with if method == "POST" else None)
        if failure:
            status, text = failure
            response = FakeResponse(status, {"errorMessages": [text]})
            response.text = text
            return response
        parts = path.strip("/").split("/")  # rest api 3 field ...
        if method == "GET" and parts[3:] == ["field", "search"]:
            hits = [f for f in self.fields if params["query"].casefold() in f["name"].casefold()]
            return FakeResponse(200, _page(hits, params))
        if method == "GET" and len(parts) == 6 and parts[5] == "context":
            return FakeResponse(200, _page(self.contexts.get(parts[4], []), params))
        if len(parts) == 8 and parts[5] == "context" and parts[7] == "option":
            key = (parts[4], parts[6])
            if method == "GET":
                return FakeResponse(200, _page(self.options.setdefault(key, []), params))
            if method == "POST":
                created = []
                for option in json["options"]:
                    created.append({"id": str(self._next_id), "value": option["value"], "disabled": option.get("disabled", False)})
                    self._next_id += 1
                self.options.setdefault(key, []).extend(created)
                return FakeResponse(200, {"options": created})
        return FakeResponse(404, {"errorMessages": [f"no fake route for {method} {path}"]})

    def option_values(self, field_id="customfield_10050", context_id="10150") -> list[str]:
        return [o["value"] for o in self.options[(field_id, context_id)]]


class FakeTelegramSession:
    """Telegram Bot API: getMe, getUpdates (offset/long poll), sendMessage (with inline buttons),
    editMessageText, answerCallbackQuery, getFile and file download."""

    def __init__(self, token: str, bot_username: str = "syncbot"):
        self.token = token
        self.bot = {"id": 123456789, "is_bot": True, "first_name": "Sync bot", "username": bot_username}
        self.queue: list[dict] = []          # updates not yet confirmed
        self.sent: list[dict] = []           # messages our bot sent
        self.files: dict[str, bytes] = {}    # file_id -> bytes
        self.on_send = None                  # callback(message) -> list of messages that arrive in reply
        self.raise_error: Exception | None = None
        self.scripted: dict[str, list] = {}  # Bot API method -> next outcomes, before normal answers
        self.calls: list[tuple[str, dict]] = []
        self.answered: list[dict] = []       # answerCallbackQuery requests (stops the button's spinner)
        self.clock: FakeClock | None = None  # set to release presses scheduled for later
        self._later: list[tuple[float, dict]] = []
        self._press_ids: set[str] = set()
        self._update_id = 500
        self._message_id = 3900

    # --- helpers for tests ---------------------------------------------------
    def push(self, message: dict) -> dict:
        self._update_id += 1
        self.queue.append({"update_id": self._update_id, "message": message})
        return message

    def press(self, message: dict, user: dict, data: str, *, after_seconds: float | None = None) -> str:
        """Someone in the group presses one of the buttons under `message` (now, or later on the clock)."""
        press_id = f"4382{len(self._press_ids) + 1:06d}"
        self._press_ids.add(press_id)
        body = {"id": press_id, "from": user, "message": dict(message), "chat_instance": "-5559119116", "data": data}
        if after_seconds is None:
            self._deliver({"callback_query": body})
        else:
            self._later.append((self.clock.now + after_seconds, {"callback_query": body}))
        return press_id

    def _deliver(self, update: dict) -> None:
        self._update_id += 1  # Telegram numbers updates when they arrive
        self.queue.append({"update_id": self._update_id, **update})

    def message(self, chat_id: int, sender: dict, *, text=None, document=None) -> dict:
        self._message_id += 1
        msg = {"message_id": self._message_id, "date": 1793700000 + self._message_id,
               "chat": {"id": chat_id, "title": "Ops Team", "type": "supergroup"}, "from": sender}
        if text is not None:
            msg["text"] = text
        if document is not None:
            msg["document"] = document
        return msg

    def document(self, file_name: str, content: bytes, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet") -> dict:
        file_id = f"BQACAgIAAx{len(self.files)}"
        self.files[file_id] = content
        return {"file_name": file_name, "mime_type": mime, "file_id": file_id,
                "file_unique_id": f"AgAD{len(self.files)}", "file_size": len(content)}

    # --- HTTP ---------------------------------------------------------------
    def post(self, url, json=None, timeout=None):
        return self._handle(url, json or {})

    def get(self, url, params=None, timeout=None):
        return self._handle(url, params or {})

    def _handle(self, url, data):
        if self.raise_error:
            raise self.raise_error
        path = urlparse(url).path
        if path.startswith(f"/file/bot{self.token}/"):
            file_id = path.rsplit("/", 1)[-1]
            return FakeResponse(200, content=self.files[file_id])
        prefix = f"/bot{self.token}/"
        if not path.startswith(prefix):
            return FakeResponse(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
        method = path[len(prefix):]
        self.calls.append((method, dict(data)))
        if self.scripted.get(method):
            return _scripted(self.scripted[method])
        if method == "getMe":
            return _ok(self.bot)
        if method == "getUpdates":
            if self.clock is not None:
                due = [update for at, update in self._later if at <= self.clock.now]
                self._later = [(at, update) for at, update in self._later if at > self.clock.now]
                for update in due:
                    self._deliver(update)
            offset = data.get("offset")
            if offset is not None and offset < 0:
                self.queue = self.queue[offset:]
            elif offset is not None:
                self.queue = [u for u in self.queue if u["update_id"] >= offset]
            return _ok(list(self.queue))
        if method == "sendMessage":
            self._message_id += 1
            msg = {"message_id": self._message_id, "date": 1793700000 + self._message_id,
                   "chat": {"id": data["chat_id"], "title": "Ops Team", "type": "supergroup"},
                   "from": self.bot, "text": data["text"]}
            if "reply_markup" in data:
                msg["reply_markup"] = data["reply_markup"]
            self.sent.append(msg)
            for reply in (self.on_send(msg) if self.on_send else []):
                self.push(reply)
            return _ok(msg)
        if method == "editMessageText":
            msg = next((m for m in self.sent if m["message_id"] == data["message_id"]
                        and m["chat"]["id"] == data["chat_id"]), None)
            if msg is None:
                return FakeResponse(400, {"ok": False, "error_code": 400,
                                          "description": "Bad Request: message to edit not found"})
            if msg["text"] == data["text"] and "reply_markup" not in msg and "reply_markup" not in data:
                return FakeResponse(400, {"ok": False, "error_code": 400, "description":
                                          "Bad Request: message is not modified: specified new message content and "
                                          "reply markup are exactly the same as a current content and reply markup"})
            msg["text"] = data["text"]
            msg.pop("reply_markup", None)
            if "reply_markup" in data:
                msg["reply_markup"] = data["reply_markup"]
            return _ok(msg)
        if method == "answerCallbackQuery":
            if data.get("callback_query_id") not in self._press_ids:
                return FakeResponse(400, {"ok": False, "error_code": 400, "description":
                                          "Bad Request: query is too old and response timeout expired or query ID is invalid"})
            self.answered.append(dict(data))
            return _ok(True)
        if method == "getFile":
            file_id = data["file_id"]
            return _ok({"file_id": file_id, "file_unique_id": "x", "file_size": len(self.files[file_id]),
                        "file_path": f"documents/{file_id}"})
        return FakeResponse(404, {"ok": False, "error_code": 404, "description": "Not Found"})


def _ok(result):
    return FakeResponse(200, {"ok": True, "result": result})


def _pairs(options):
    for option in options:
        yield (option, False) if isinstance(option, str) else option


def _page(values: list, params: dict) -> dict:
    start = int(params.get("startAt", 0))
    size = int(params.get("maxResults", 50))
    chunk = values[start:start + size]
    return {"maxResults": size, "startAt": start, "total": len(values),
            "isLast": start + len(chunk) >= len(values), "values": chunk}


def _field(field_id: str, name: str) -> dict:
    return {"id": field_id, "key": field_id, "name": name, "isLocked": False,
            "schema": {"type": "option", "custom": "com.atlassian.jira.plugin.system.customfieldtypes:select",
                       "customId": int(field_id.split("_")[1])},
            "description": "", "searcherKey": "com.atlassian.jira.plugin.system.customfieldtypes:multiselectsearcher",
            "screensCount": 3, "contextsCount": 1}


def _context(context_id: str, name: str, is_global: bool) -> dict:
    return {"id": context_id, "name": name, "description": "", "isGlobalContext": is_global, "isAnyIssueType": True}


def network_down() -> Exception:
    return requests.ConnectionError("HTTPSConnectionPool(host='example', port=443): Max retries exceeded")


def telegram_error(code: int, description: str) -> FakeResponse:
    return FakeResponse(code, {"ok": False, "error_code": code, "description": description})
