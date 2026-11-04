"""Read and add options of a Jira Cloud select-list custom field (the one named "Merchant" by default).

Only two kinds of request are ever sent: GET (read) and POST of new options.
Nothing is renamed, reordered, enabled, disabled or deleted.
Reads are retried a few times on network blips; adding is never retried, so it can't run twice.
"""
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import requests

PAGE_SIZE = 100
READ_RETRIES = 3
TRANSIENT_STATUS = {429, 500, 502, 503, 504}


class JiraError(RuntimeError):
    """Jira refused a request, couldn't be reached, or didn't do what was asked."""


@dataclass(frozen=True)
class FieldOption:
    id: str
    value: str
    disabled: bool = False


class JiraField:
    """One custom select-list field, through its global context (the one for all spaces)."""

    def __init__(self, base_url: str, email: str, api_token: str, field_name: str = "Merchant",
                 session: requests.Session | None = None, sleep: Callable[[float], None] = time.sleep):
        self._base_url = base_url.rstrip("/")
        self._field_name = field_name
        self._session = session or requests.Session()
        self._session.auth = (email, api_token)
        self._session.headers.update({"Accept": "application/json"})
        self._sleep = sleep
        self._ids: tuple[str, str] | None = None  # (field id, context id), looked up once

    def options(self) -> list[FieldOption]:
        """Every option of the field, disabled ones included."""
        return [_option(raw) for raw in self._paged(self._options_path())]

    def add_options(self, values: list[str]) -> list[FieldOption]:
        """Add values as new options at the end of the list. Fails unless Jira confirms every one."""
        if not values:
            return []
        body = {"options": [{"value": value, "disabled": False} for value in values]}
        reply = self._request("POST", self._options_path(), json=body)
        created = [_option(raw) for raw in reply.get("options", [])]
        missing = _not_confirmed(values, [option.value for option in created])
        if missing:
            raise JiraError(f"Jira didn't confirm {len(missing)} of {len(values)} new options "
                            f"(e.g. {', '.join(missing[:5])}); check the field in Jira")
        return created

    def _options_path(self) -> str:
        field_id, context_id = self._locate()
        return f"/rest/api/3/field/{field_id}/context/{context_id}/option"

    def _locate(self) -> tuple[str, str]:
        if self._ids is None:
            wanted = self._field_name.casefold()
            fields = [f for f in self._paged("/rest/api/3/field/search", type="custom", query=self._field_name)
                      if f.get("name", "").strip().casefold() == wanted]
            if len(fields) != 1:
                raise JiraError(f"expected 1 custom field named {self._field_name!r}, found {len(fields)}")
            field_id = fields[0]["id"]
            contexts = [c for c in self._paged(f"/rest/api/3/field/{field_id}/context") if c.get("isGlobalContext")]
            if not contexts:
                raise JiraError(f"field {self._field_name!r} ({field_id}) has no global context")
            self._ids = (field_id, str(contexts[0]["id"]))
        return self._ids

    def _paged(self, path: str, **params) -> Iterator[dict]:
        start = 0
        while True:
            page = self._request("GET", path, params={**params, "startAt": start, "maxResults": PAGE_SIZE})
            values = page.get("values", [])
            yield from values
            if not values or page.get("isLast", len(values) < PAGE_SIZE):
                return
            start += len(values)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        attempts = 1 + (READ_RETRIES if method == "GET" else 0)
        for attempt in range(1, attempts + 1):
            wait = None
            try:
                response = self._session.request(method, self._base_url + path, timeout=30, **kwargs)
            except requests.RequestException as exc:
                problem = f"{method} {path} failed: {exc}"
            else:
                if response.status_code < 400:
                    return _json(response, method, path)
                problem = f"{method} {path} -> {response.status_code}: {response.text[:300]}"
                if response.status_code not in TRANSIENT_STATUS:
                    raise JiraError(problem)
                wait = _retry_after(response)
            if attempt == attempts:
                raise JiraError(problem)
            self._sleep(wait if wait is not None else 2 ** attempt)
        raise AssertionError("unreachable")


def _json(response, method: str, path: str) -> dict:
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        raise JiraError(f"{method} {path}: Jira answered HTTP {response.status_code} "
                        f"with something that isn't JSON") from None


def _retry_after(response) -> int | None:
    try:
        return int(response.headers.get("Retry-After", ""))
    except ValueError:
        return None


def _not_confirmed(sent: list[str], created: list[str]) -> list[str]:
    def norm(value: str) -> str:
        return " ".join(value.split()).casefold()
    available = Counter(norm(value) for value in created)
    missing = []
    for value in sent:
        if available[norm(value)] > 0:
            available[norm(value)] -= 1
        else:
            missing.append(value)
    return missing


def _option(raw: dict) -> FieldOption:
    return FieldOption(id=str(raw["id"]), value=raw["value"], disabled=bool(raw.get("disabled", False)))
