"""Keep a Jira select-list field in sync with the merchant report a Telegram bot sends.

    py merchant_sync.py                         the weekly run: ask the bot, add new names, tell the group
    py merchant_sync.py --dry-run               show what would be added; Jira not changed, no note posted
                                                (still writes REPORT_COMMAND in the group to get a fresh Excel)
    py merchant_sync.py --dry-run --file X.xlsx completely silent check on a saved Excel
    py merchant_sync.py --file X.xlsx           use a saved Excel instead of asking the bot (note still posted)
    py merchant_sync.py --no-limit              allow more than 25 new names in one run (first catch-up)

Everything else is configured in .env (see .env.example). With BY_CONFIRMATION=True (the default) the new
names are posted in the group with "✅ Add them" / "❌ Don't add" buttons, and Jira is only changed after
someone presses ✅. With BY_CONFIRMATION=False they're added straight away and the group is told what was added.
"""
import argparse
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path

from dotenv import dotenv_values

try:  # the one-run-at-a-time lock: Windows
    import msvcrt
except ImportError:  # Linux / macOS
    msvcrt = None
    import fcntl

from jira_field import FieldOption, JiraError, JiraField
from names import ExcelFormatError, NameRules, match_key, read_names
from telegram_bot import TelegramBot, TelegramError

HERE = Path(__file__).resolve().parent
DOWNLOADS_DIR = HERE / "downloads"
LOG_FILE = HERE / "logs" / "merchant_sync.log"
LOCK_FILE = HERE / "logs" / "merchant_sync.lock"  # held while a run talks to Telegram

WAIT_SECONDS = 180                   # how long the report bot gets to send its Excel
MAX_NEW_PER_RUN = 25                 # only when adding straight away (BY_CONFIRMATION=False)
NOTE_BUDGET = 3500                   # characters of names per message; Telegram allows 4096
ANSWER_UNTIL = time(18, 0)           # in confirmation mode, people can press a button until then...
MIN_ANSWER_SECONDS = 3600            # ...but always for at least an hour...
MAX_ANSWER_SECONDS = 11 * 3600       # ...and never longer than the scheduled task may run (12 h)
BUTTONS = [("✅ Add them", "add"), ("❌ Don't add", "skip")]

log = logging.getLogger("merchant_sync")


class SettingsError(ValueError):
    """.env is missing something or has a value that can't work."""


class AddError(JiraError):
    """Jira refused the new options."""


@dataclass(frozen=True)
class Settings:
    telegram_token: str                  # our bot
    report_bot: str                      # the bot that answers report_command with the Excel
    group_id: int                        # the Telegram group both bots are in
    report_command: str                  # e.g. "/report"; our bot writes it as "/report@<report_bot>"
    jira_url: str
    jira_email: str
    jira_token: str
    report_file_prefix: str = ""         # only take Excel files whose name starts with this
    jira_field: str = "Merchant"         # the select-list custom field to keep in sync
    name_rules: NameRules = NameRules()
    by_confirmation: bool = True         # ask in the group first unless told otherwise

    @property
    def label(self) -> str:
        return f'Jira "{self.jira_field}" field'


def seconds_to_answer(now: datetime) -> int:
    """People have until 18:00 that day to press a button: at least an hour, at most 11 hours."""
    deadline = datetime.combine(now.date(), ANSWER_UNTIL)
    return min(max(int((deadline - now).total_seconds()), MIN_ANSWER_SECONDS), MAX_ANSWER_SECONDS)


_ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "REPORT_BOT", "TELEGRAM_GROUP_ID", "REPORT_COMMAND",
             "JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")


def load_settings(env_path: Path) -> Settings:
    # utf-8-sig: PowerShell 5.1 saves files with a byte-order mark that would hide the first key.
    # Key names are matched in capitals, so "BY_Confirmation" works the same as "BY_CONFIRMATION".
    values = {key.strip().upper(): (value or "").strip()
              for key, value in dotenv_values(env_path, encoding="utf-8-sig").items()}
    missing = [key for key in _ENV_KEYS if not values.get(key) or "<" in values[key]]
    if missing:
        raise SettingsError(f"fill in {', '.join(missing)} in {env_path}")
    if not values["JIRA_URL"].lower().startswith("https://"):
        raise SettingsError(f"JIRA_URL must start with https:// (got {values['JIRA_URL']!r})")
    try:
        group_id = int(values["TELEGRAM_GROUP_ID"])
    except ValueError:
        raise SettingsError(f"TELEGRAM_GROUP_ID must be a number like -1001234567890, "
                            f"not {values['TELEGRAM_GROUP_ID']!r}") from None
    command = values["REPORT_COMMAND"].split("@")[0]  # "/report@SomeBot" -> "/report"; the bot name is added
    if not command.startswith("/") or len(command) < 2 or " " in command:
        raise SettingsError(f"REPORT_COMMAND must be a bot command like /report, not {values['REPORT_COMMAND']!r}")
    defaults = NameRules()
    return Settings(
        telegram_token=values["TELEGRAM_BOT_TOKEN"],
        report_bot=values["REPORT_BOT"].lstrip("@"),
        group_id=group_id,
        report_command=command,
        jira_url=values["JIRA_URL"].rstrip("/"),
        jira_email=values["JIRA_EMAIL"],
        jira_token=values["JIRA_API_TOKEN"],
        report_file_prefix=values.get("REPORT_FILE_PREFIX", ""),
        jira_field=values.get("JIRA_FIELD") or "Merchant",
        name_rules=NameRules(
            drop_words=_words(values, "DROP_WORDS"),
            ignore_words=_words(values, "IGNORE_WORDS"),
            merchant_column=values.get("MERCHANT_COLUMN") or defaults.merchant_column,
            group_column=values.get("GROUP_COLUMN") or defaults.group_column,
            check_total_row=_flag(values, "CHECK_TOTAL_ROW", default=defaults.check_total_row),
        ),
        by_confirmation=_flag(values, "BY_CONFIRMATION", default=True),  # missing: ask first, the safe way
    )


def _words(values: dict[str, str], key: str) -> tuple[str, ...]:
    """A comma-separated list in .env, e.g. DROP_WORDS=PROMO, VIP."""
    return tuple(word.strip() for word in values.get(key, "").split(",") if word.strip())


def _flag(values: dict[str, str], key: str, *, default: bool) -> bool:
    raw = values.get(key, "")
    if not raw:
        return default
    if raw.lower() in ("true", "yes", "1"):
        return True
    if raw.lower() in ("false", "no", "0"):
        return False
    raise SettingsError(f"{key} must be True or False, not {raw!r}")


def new_names(names: list[str], existing: list[FieldOption], ignore_words: tuple[str, ...] = ()) -> list[str]:
    """Names Jira doesn't have yet. Disabled options count as already there."""
    taken = {match_key(option.value, ignore_words) for option in existing}
    return [name for name in names if match_key(name, ignore_words) not in taken]


@dataclass
class _Question:
    """The message with buttons, once it's posted. The final note replaces it, so the buttons go away."""
    message_id: int | None = None


def run_and_report(settings: Settings, telegram: TelegramBot, jira: JiraField, *, excel: Path | None = None,
                   dry_run: bool = False, limit: int | None = MAX_NEW_PER_RUN, downloads: Path = DOWNLOADS_DIR,
                   now: Callable[[], datetime] = datetime.now) -> tuple[int, str]:
    """One sync. Returns (exit code, note). The note goes to the group unless it's a dry run."""
    question = _Question()
    try:
        code, note = _sync(settings, telegram, jira, excel=excel, dry_run=dry_run, limit=limit,
                           downloads=downloads, now=now, question=question)
    except AddError as exc:
        code, note = 1, f"{settings.label}: adding the new names failed, check the field in Jira.\n{exc}"
    except (TelegramError, ExcelFormatError, JiraError, OSError) as exc:
        code, note = 1, f"{settings.label}: not updated, nothing was changed.\n{exc}"
    except Exception:  # a bug: still tell the group, and never leave the buttons behind
        log.exception("unexpected error")
        code, note = 1, (f"{settings.label}: the run crashed. Details are in logs/merchant_sync.log on the PC. "
                         f"Check the field in Jira.")
    except BaseException:  # stopped by hand (Ctrl+C): take the buttons away, then stop
        if question.message_id is not None and not dry_run:
            _replace(telegram, settings.group_id, question.message_id,
                     f"{settings.label}: the run was stopped before it finished. Check the field in Jira.")
        raise
    log.log(logging.INFO if code == 0 else logging.ERROR, note)
    if not dry_run:
        if question.message_id is None:
            _post(telegram, settings.group_id, note)
        else:
            _replace(telegram, settings.group_id, question.message_id, note)
    return code, note


def _sync(settings: Settings, telegram: TelegramBot, jira: JiraField, *, excel: Path | None, dry_run: bool,
          limit: int | None, downloads: Path, now: Callable[[], datetime], question: _Question) -> tuple[int, str]:
    existing = jira.options()  # Jira first: a broken Jira token mustn't make the bot ask for nothing
    path = Path(excel) if excel else telegram.request_file(
        settings.group_id, f"{settings.report_command}@{settings.report_bot}", from_username=settings.report_bot,
        name_prefix=settings.report_file_prefix, wait_seconds=WAIT_SECONDS, save_dir=downloads)
    names = read_names(path, settings.name_rules)
    todo = new_names(names, existing, settings.name_rules.ignore_words)
    log.info("%s: %d names, %d not in Jira yet: %s", path.name, len(names), len(todo), ", ".join(todo) or "-")

    if dry_run:
        return 0, _dry_run_report(path, names, todo, limit, settings.by_confirmation)
    if not todo:
        return 0, f"{settings.label}: nothing new this week ({len(names)} names checked)."
    if settings.by_confirmation:
        return _ask_then_add(settings, telegram, jira, todo, now=now, question=question)
    if limit is not None and len(todo) > limit:
        return 1, (f"{settings.label}: not updated. {len(todo)} new names is more than the limit of {limit}, "
                   f"so nothing was added. Check the list, then run it by hand with --no-limit.\n{_bullets(todo)}")
    _add(jira, todo)
    return 0, f"{settings.label}: added {len(todo)} new\n{_bullets(todo)}"


def _ask_then_add(settings: Settings, telegram: TelegramBot, jira: JiraField, todo: list[str], *,
                  now: Callable[[], datetime], question: _Question) -> tuple[int, str]:
    if not _all_fit(todo):
        return 1, (f"{settings.label}: {len(todo)} new names, too many to show in one message, so I didn't ask and "
                   f"nothing was added. Check them on the PC with a dry run, then add them with "
                   f"BY_CONFIRMATION=False and --no-limit.\n{_bullets(todo)}")
    asked_at = now()
    wait = seconds_to_answer(asked_at)
    until = (asked_at + timedelta(seconds=wait)).strftime("%H:%M")
    asked = telegram.send_message(
        settings.group_id,
        f"{settings.label}: {len(todo)} new {'name' if len(todo) == 1 else 'names'} to add\n{_bullets(todo)}\n\n"
        f"Anyone here can decide. Waiting until {until}.",
        buttons=BUTTONS)
    question.message_id = asked["message_id"]
    log.info("asked the group, waiting for a button press until %s", until)

    answer = telegram.wait_for_press(settings.group_id, asked["message_id"], wait_seconds=wait)
    if answer is None:
        return 1, (f"{settings.label}: nobody answered by {until}, so nothing was added. "
                   f"I'll ask again next Monday.\n{_bullets(todo)}")
    choice, who = answer
    log.info("%s pressed %r", who, choice)
    if choice != "add":
        return 0, f"{settings.label}: not added, {who} pressed Don't add. I'll ask again next Monday.\n{_bullets(todo)}"
    # take the buttons away now, so nobody presses again while Jira is being changed
    _try_edit(telegram, settings.group_id, asked["message_id"],
              f"{settings.label}: {who} pressed ✅ Add them. Adding {len(todo)} new names…")
    still_missing = new_names(todo, jira.options(), settings.name_rules.ignore_words)  # added by hand meanwhile?
    if not still_missing:
        return 0, f"{settings.label}: approved by {who}, but they're all in Jira already, so nothing was added."
    _add(jira, still_missing)
    return 0, f"{settings.label}: added {len(still_missing)} new, approved by {who}\n{_bullets(still_missing)}"


def _add(jira: JiraField, names: list[str]) -> None:
    try:
        jira.add_options(names)
    except JiraError as exc:
        raise AddError(str(exc)) from None


def _dry_run_report(path: Path, names: list[str], todo: list[str], limit: int | None, confirm: bool) -> str:
    lines = ["DRY RUN: Jira was not changed and no note was posted.",
             f"{path.name}: {len(names)} names, {len(names) - len(todo)} already in Jira.",
             f"Would add {len(todo)}:" if todo else "Would add nothing."]
    lines += [f"  {name}" for name in todo]
    if todo and confirm:
        lines.append("A real run would first ask in the group (BY_CONFIRMATION=True).")
    elif limit is not None and len(todo) > limit:
        lines.append(f"A real run would add nothing: {len(todo)} is more than the limit of {limit}. "
                     f"Run it with --no-limit to add them.")
    return "\n".join(lines)


def _all_fit(names: list[str]) -> bool:
    return sum(len(f"• {name}") + 1 for name in names) <= NOTE_BUDGET


def _bullets(names: list[str]) -> str:
    lines, used = [], 0
    for index, name in enumerate(names):
        line = f"• {name}"
        if used + len(line) + 1 > NOTE_BUDGET:
            lines.append(f"… and {len(names) - index} more (full list in logs/merchant_sync.log)")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _post(telegram: TelegramBot, chat_id: int, text: str) -> None:
    try:
        telegram.send_message(chat_id, text)
    except TelegramError as exc:
        log.error("could not post the note in the group: %s", exc)


def _replace(telegram: TelegramBot, chat_id: int, message_id: int, text: str) -> None:
    """Turn the question into the final note. If that fails, post the note as a new message."""
    if not _try_edit(telegram, chat_id, message_id, text):
        _post(telegram, chat_id, text)


def _try_edit(telegram: TelegramBot, chat_id: int, message_id: int, text: str) -> bool:
    try:
        telegram.edit_message(chat_id, message_id, text)
        return True
    except TelegramError as exc:
        log.error("could not update the question in the group: %s", exc)
        return False


def _lock_single_run(path: Path):
    """An open, locked file while this run talks to Telegram; None if another run holds it.

    Two runs polling the same bot would take each other's button presses and Excel files.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    handle.seek(0)
    try:
        if msvcrt:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _unlock(handle) -> None:
    handle.seek(0)
    if msvcrt:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be added; Jira is not changed and no note is posted "
                             "(without --file it still writes the command in the group to get the Excel)")
    parser.add_argument("--file", type=Path,
                        help="use this saved Excel instead of asking the bot (the note is still posted)")
    parser.add_argument("--no-limit", action="store_true",
                        help=f"allow more than {MAX_NEW_PER_RUN} new names in one run")
    return parser.parse_args(argv)


def _setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handlers: list[logging.Handler] = [file_handler]
    if sys.stdout:  # no console when started by Task Scheduler with pyw
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter("%(message)s"))
        handlers.append(console)
    log.handlers[:] = handlers
    log.setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _setup_logging(LOG_FILE)
    log.info("--- run started: %s, Excel from %s, %s", "DRY RUN" if args.dry_run else "real run",
             args.file or "the bot", "no limit" if args.no_limit else f"limit {MAX_NEW_PER_RUN}")
    try:
        settings = load_settings(HERE / ".env")
    except SettingsError as exc:
        log.error("%s", exc)
        return 2
    log.info("BY_CONFIRMATION=%s: %s", settings.by_confirmation,
             "asks in the group before adding" if settings.by_confirmation else "adds straight away")
    uses_telegram = not (args.dry_run and args.file)
    lock = _lock_single_run(LOCK_FILE) if uses_telegram else None
    if uses_telegram and lock is None:
        log.error("another run is still going (it may be waiting for a button press in the group), "
                  "so this one stops without doing anything")
        return 3
    telegram = TelegramBot(settings.telegram_token)
    jira = JiraField(settings.jira_url, settings.jira_email, settings.jira_token, field_name=settings.jira_field)
    try:
        code, _ = run_and_report(settings, telegram, jira, excel=args.file, dry_run=args.dry_run,
                                 limit=None if args.no_limit else MAX_NEW_PER_RUN)
    except Exception:  # an unattended job must not die silently
        log.exception("unexpected error")
        if not args.dry_run:
            _post(telegram, settings.group_id, f"{settings.label}: the sync crashed, see logs/merchant_sync.log on the PC.")
        return 1
    finally:
        if lock:
            _unlock(lock)
    return code


if __name__ == "__main__":
    sys.exit(main())
