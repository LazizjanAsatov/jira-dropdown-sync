"""The whole Monday run, through the real Telegram, Excel and Jira code against fake services.

All names here are made up.
"""
import copy
import itertools
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from jira_field import FieldOption, JiraField
from merchant_sync import Settings, SettingsError, load_settings, new_names, run_and_report, seconds_to_answer
from names import NO_GROUP, NameRules
from telegram_bot import TelegramBot
from tests.fakes import FakeClock, FakeJiraSession, FakeTelegramSession, network_down, telegram_error

TOKEN = "123456789:TEST-TOKEN-not-real"
GROUP = -1001234567890
COMMAND = "/report@ReportBot"
REPORT_BOT = {"id": 7000000001, "is_bot": True, "first_name": "Report Bot", "username": "ReportBot"}
ALEX = {"id": 51000001, "is_bot": False, "first_name": "Alex", "username": "alex"}
MONDAY_10 = datetime(2026, 11, 9, 10, 0)
RULES = NameRules(drop_words=("PROMO",), ignore_words=("ACME",))  # ACME plays your company tag
SETTINGS = Settings(telegram_token=TOKEN, report_bot="ReportBot", group_id=GROUP, report_command="/report",
                    jira_url="https://example.atlassian.net", jira_email="ops@example.com", jira_token="jira-token",
                    report_file_prefix="Merchant report", name_rules=RULES)

ROWS = [
    ("Blue Harbor ACME", "Vega Play"),
    ("Nova Bet ACME", "Orbit Group"),
    ("Nova Bet ACME PROMO", "Orbit Group"),
    ("Lumen Pay ACME", NO_GROUP),
]  # names: Blue Harbor ACME, Vega Play, Nova Bet ACME, Orbit Group, Lumen Pay ACME


class World:
    """Fake Telegram group + fake Jira, driven through the real clients."""

    def __init__(self, tmp_path, jira_options=(), bot_answers=True):
        self.tmp_path = tmp_path
        self.tg = FakeTelegramSession(TOKEN)
        self.jira = FakeJiraSession(options=jira_options)
        self.excel_bytes: bytes | None = None
        self.bot_answers = bot_answers
        self.tg.on_send = self._group_reacts
        self.tg.clock = FakeClock(step=30)
        self.jira.on_post = self._jira_adding
        self.questions: list[dict] = []          # messages with buttons, as they were first posted
        self.question_id: int | None = None
        self.jira_writes_when_asked: int | None = None
        self.buttons_while_adding: bool | None = None
        self.when_asked = None                   # callback() run when the question is posted
        self._press = None

    def bot_sends(self, excel_path):
        self.excel_bytes = excel_path.read_bytes()

    def press_when_asked(self, answer, user, before=None, after_seconds=None):
        """When our bot asks, `user` presses `answer` (now or later); `before` runs first (e.g. someone edits Jira)."""
        self._press = (answer, user, before, after_seconds)

    def _jira_adding(self):
        question = next((m for m in self.tg.sent if m["message_id"] == self.question_id), None)
        self.buttons_while_adding = question is not None and "reply_markup" in question

    def _group_reacts(self, sent):
        if "reply_markup" in sent:
            self.questions.append(copy.deepcopy(sent))
            self.question_id = sent["message_id"]
            self.jira_writes_when_asked = len(self.jira_writes)
            if self.when_asked:
                self.when_asked()
            if self._press:
                answer, user, before, after_seconds = self._press
                if before:
                    before()
                self.tg.press(sent, user, answer, after_seconds=after_seconds)
            return []
        if sent["text"] != COMMAND or self.excel_bytes is None or not self.bot_answers:
            return []
        doc = self.tg.document("Merchant report_2026-11-01.xlsx", self.excel_bytes)
        return [self.tg.message(GROUP, REPORT_BOT, document=doc)]

    def run(self, confirm=False, now=MONDAY_10, rules=RULES, **options):
        telegram = TelegramBot(TOKEN, session=self.tg, clock=self.tg.clock, sleep=lambda s: None)
        jira = JiraField(SETTINGS.jira_url, SETTINGS.jira_email, SETTINGS.jira_token, session=self.jira,
                         sleep=lambda s: None)
        settings = replace(SETTINGS, by_confirmation=confirm, name_rules=rules)
        return run_and_report(settings, telegram, jira, downloads=self.tmp_path / "downloads",
                              now=lambda: now, **options)

    @property
    def posted(self) -> list[str]:
        return [m["text"] for m in self.tg.sent if m["text"] != COMMAND]

    @property
    def jira_writes(self) -> list:
        return [call for call in self.jira.calls if call[0] != "GET"]


@pytest.fixture
def world(tmp_path):
    return lambda **kw: World(tmp_path, **kw)


def test_weekly_run_adds_only_new_names_and_tells_the_group_what_it_added(world, bot_excel):
    w = world(jira_options=["Orbit Group", "Blue Harbor"])
    w.bot_sends(bot_excel(ROWS))

    code, note = w.run()

    assert code == 0
    assert w.jira.option_values() == ["Orbit Group", "Blue Harbor", "Vega Play", "Nova Bet ACME", "Lumen Pay ACME"]
    assert w.posted == [note]
    for name in ("Vega Play", "Nova Bet ACME", "Lumen Pay ACME"):
        assert name in note
    assert "Blue Harbor" not in note


def test_nothing_new_adds_nothing_and_says_so(world, bot_excel):
    w = world(jira_options=["Blue Harbor", "Vega Play", "Nova Bet", "Orbit Group", "LUMEN PAY ACME"])
    w.bot_sends(bot_excel(ROWS))

    code, note = w.run()

    assert code == 0
    assert w.jira_writes == []
    assert w.posted == [note]
    assert "nothing new" in note.lower()


def test_more_new_names_than_the_limit_adds_nothing_and_warns(world, bot_excel):
    w = world(jira_options=["Orbit Group"])
    w.bot_sends(bot_excel(ROWS))

    code, note = w.run(limit=3)

    assert code == 1
    assert w.jira_writes == []
    assert w.posted == [note]
    assert "4" in note and "3" in note


def test_no_limit_adds_everything_new(world, bot_excel):
    w = world(jira_options=["Orbit Group"])
    w.bot_sends(bot_excel(ROWS))

    code, _ = w.run(limit=None)

    assert code == 0
    assert w.jira.option_values() == ["Orbit Group", "Blue Harbor ACME", "Vega Play", "Nova Bet ACME", "Lumen Pay ACME"]


def test_dry_run_changes_nothing_posts_nothing_and_lists_what_it_would_add(world, bot_excel):
    w = world(jira_options=["Orbit Group", "Blue Harbor"])

    code, report = w.run(excel=bot_excel(ROWS), dry_run=True)

    assert code == 0
    assert w.jira_writes == []
    assert w.tg.sent == []
    for name in ("Vega Play", "Nova Bet ACME", "Lumen Pay ACME"):
        assert name in report


def test_a_saved_excel_is_used_without_asking_the_bot(world, bot_excel):
    w = world(jira_options=["Orbit Group", "Blue Harbor"])

    code, note = w.run(excel=bot_excel(ROWS))

    assert code == 0
    assert [m["text"] for m in w.tg.sent] == [note]
    assert "Nova Bet ACME" in w.jira.option_values()


def test_bot_not_answering_changes_nothing_and_tells_the_group(world):
    w = world(jira_options=["Orbit Group"], bot_answers=False)

    code, note = w.run()

    assert code == 1
    assert w.jira_writes == []
    assert w.posted == [note]
    assert "ReportBot" in note


def test_broken_excel_changes_nothing_and_tells_the_group(world, tmp_path):
    from openpyxl import Workbook
    broken = tmp_path / "broken.xlsx"
    wb = Workbook()
    wb.active.append(["Name", "Team"])
    wb.save(broken)
    w = world(jira_options=["Orbit Group"])
    w.bot_sends(broken)

    code, note = w.run()

    assert code == 1
    assert w.jira_writes == []
    assert w.posted == [note]


def test_jira_refusing_the_new_options_is_reported(world, bot_excel):
    w = world(jira_options=["Orbit Group"])
    w.jira.fail_post_with = (400, "Option value already exists")
    w.bot_sends(bot_excel(ROWS))

    code, note = w.run()

    assert code == 1
    assert w.posted == [note]
    assert "Option value already exists" in note
    assert "adding" in note.lower() and "nothing was changed" not in note


def test_exactly_as_many_new_names_as_the_limit_are_still_added(world, bot_excel):
    w = world(jira_options=["Orbit Group"])
    w.bot_sends(bot_excel(ROWS))

    code, _ = w.run(limit=4)

    assert code == 0
    assert len(w.jira.option_values()) == 5


def test_jira_is_checked_before_the_bot_is_asked(world, bot_excel):
    w = world(jira_options=["Orbit Group"])
    w.jira.fail_with = (401, "Client must be authenticated to access this resource.")
    w.bot_sends(bot_excel(ROWS))

    code, note = w.run()

    assert code == 1
    assert COMMAND not in [m["text"] for m in w.tg.sent]
    assert w.posted == [note]


def test_dry_run_without_a_file_only_writes_the_command(world, bot_excel):
    w = world(jira_options=["Orbit Group"])
    w.bot_sends(bot_excel(ROWS))

    code, _ = w.run(dry_run=True)

    assert code == 0
    assert [m["text"] for m in w.tg.sent] == [COMMAND]
    assert w.jira_writes == []


def test_dry_run_says_when_a_real_run_would_stop_at_the_limit(world, bot_excel):
    w = world(jira_options=["Orbit Group"])

    code, report = w.run(excel=bot_excel(ROWS), dry_run=True, limit=3)

    assert code == 0
    assert "--no-limit" in report


def test_a_very_long_list_still_fits_in_one_telegram_message(world, bot_excel):
    rows = [(f"Merchant number {i:03d} " + "with a really long registered company name " * 2 + "ACME", NO_GROUP)
            for i in range(300)]
    w = world(jira_options=[])
    w.bot_sends(bot_excel(rows))

    code, note = w.run(limit=None)

    assert code == 0
    assert len(note) <= 4096
    assert w.posted == [note]


# --- confirmation mode (BY_CONFIRMATION=True) -------------------------------------------


def test_confirmation_mode_asks_first_and_adds_only_after_add_is_pressed(world, bot_excel):
    w = world(jira_options=["Orbit Group", "Blue Harbor"])
    w.bot_sends(bot_excel(ROWS))
    w.press_when_asked("add", ALEX)

    code, note = w.run(confirm=True)

    assert code == 0
    [question] = w.questions
    for name in ("Vega Play", "Nova Bet ACME", "Lumen Pay ACME"):
        assert name in question["text"]
    assert w.jira_writes_when_asked == 0
    assert w.jira.option_values() == ["Orbit Group", "Blue Harbor", "Vega Play", "Nova Bet ACME", "Lumen Pay ACME"]
    assert [m["text"] for m in w.tg.sent] == [COMMAND, note]  # the question was turned into the result
    assert "reply_markup" not in w.tg.sent[-1]
    assert "@alex" in note


def test_merchants_of_a_group_listed_without_merchants_are_never_offered_or_added(world, bot_excel):
    w = world(jira_options=["Blue Harbor"])
    w.bot_sends(bot_excel(ROWS))
    w.press_when_asked("add", ALEX)

    code, note = w.run(confirm=True, rules=replace(RULES, groups_without_merchants=("Orbit Group",)))

    assert code == 0
    [question] = w.questions
    assert "Orbit Group" in question["text"]
    assert "Nova Bet" not in question["text"] and "Nova Bet" not in note
    assert w.jira.option_values() == ["Blue Harbor", "Vega Play", "Orbit Group", "Lumen Pay ACME"]


def test_pressing_dont_add_adds_nothing(world, bot_excel):
    w = world(jira_options=["Orbit Group", "Blue Harbor"])
    w.bot_sends(bot_excel(ROWS))
    w.press_when_asked("skip", ALEX)

    code, note = w.run(confirm=True)

    assert code == 0
    assert w.jira_writes == []
    assert [m["text"] for m in w.tg.sent] == [COMMAND, note]
    assert "reply_markup" not in w.tg.sent[-1]
    assert "@alex" in note


def test_nobody_answering_in_time_adds_nothing_and_removes_the_buttons(world, bot_excel):
    w = world(jira_options=["Orbit Group", "Blue Harbor"])
    w.bot_sends(bot_excel(ROWS))

    code, note = w.run(confirm=True, now=datetime(2026, 11, 9, 17, 30))

    assert code == 1
    assert w.jira_writes == []
    assert [m["text"] for m in w.tg.sent] == [COMMAND, note]
    assert "reply_markup" not in w.tg.sent[-1]


def test_names_added_by_hand_while_waiting_are_not_added_again(world, bot_excel):
    w = world(jira_options=["Orbit Group", "Blue Harbor"])
    w.bot_sends(bot_excel(ROWS))
    added_by_hand = {"id": "999", "value": "Vega Play", "disabled": False}
    w.press_when_asked("add", ALEX, before=lambda: w.jira.options[("customfield_10050", "10150")].append(added_by_hand))

    code, note = w.run(confirm=True)

    assert code == 0
    assert len(w.questions) == 1
    assert w.jira.option_values() == ["Orbit Group", "Blue Harbor", "Vega Play", "Nova Bet ACME", "Lumen Pay ACME"]
    assert "Vega Play" not in note  # the note lists what this run added, not what was asked about


def test_confirmation_mode_is_not_stopped_by_the_limit(world, bot_excel):
    w = world(jira_options=["Orbit Group"])
    w.bot_sends(bot_excel(ROWS))
    w.press_when_asked("add", ALEX)

    code, _ = w.run(confirm=True, limit=3)

    assert code == 0
    assert len(w.jira.option_values()) == 5


def test_nothing_new_in_confirmation_mode_asks_nothing(world, bot_excel):
    w = world(jira_options=["Blue Harbor", "Vega Play", "Nova Bet", "Orbit Group", "LUMEN PAY ACME"])
    w.bot_sends(bot_excel(ROWS))

    code, note = w.run(confirm=True)

    assert code == 0
    assert w.questions == []
    assert w.posted == [note]
    assert "nothing new" in note.lower()


def test_a_failure_after_asking_replaces_the_question_with_the_problem(world, bot_excel):
    w = world(jira_options=["Orbit Group"])
    w.bot_sends(bot_excel(ROWS))
    w.press_when_asked("add", ALEX)
    w.jira.fail_post_with = (400, "Option value already exists")

    code, note = w.run(confirm=True)

    assert code == 1
    assert len(w.questions) == 1
    assert [m["text"] for m in w.tg.sent] == [COMMAND, note]
    assert "reply_markup" not in w.tg.sent[-1]
    assert "Option value already exists" in note


def test_dry_run_in_confirmation_mode_asks_nothing(world, bot_excel):
    w = world(jira_options=["Orbit Group", "Blue Harbor"])

    code, report = w.run(confirm=True, excel=bot_excel(ROWS), dry_run=True)

    assert code == 0
    assert w.tg.sent == []
    assert w.jira_writes == []
    for name in ("Vega Play", "Nova Bet ACME", "Lumen Pay ACME"):
        assert name in report


@pytest.mark.parametrize("now, seconds", [
    (datetime(2026, 11, 9, 10, 0), 8 * 3600),   # normal Monday run: until 18:00
    (datetime(2026, 11, 9, 17, 30), 3600),      # PC was off, run started late: still a full hour
    (datetime(2026, 11, 9, 19, 0), 3600),
    (datetime(2026, 11, 10, 2, 0), 11 * 3600),  # started at night: capped, the scheduled task allows 12 h
])
def test_people_have_until_18_00_to_answer_but_always_at_least_an_hour(now, seconds):
    assert seconds_to_answer(now) == seconds


def test_a_press_hours_after_asking_still_counts(world, bot_excel):
    w = world(jira_options=["Orbit Group", "Blue Harbor"])
    w.bot_sends(bot_excel(ROWS))
    w.press_when_asked("add", ALEX, after_seconds=5 * 3600)  # 15:00

    code, _ = w.run(confirm=True)

    assert code == 0
    assert "Nova Bet ACME" in w.jira.option_values()


@pytest.mark.parametrize("now, until", [(MONDAY_10, "18:00"), (datetime(2026, 11, 9, 17, 30), "18:30")])
def test_the_question_says_until_when_it_waits(world, bot_excel, now, until):
    w = world(jira_options=["Orbit Group"])
    w.bot_sends(bot_excel(ROWS))
    w.press_when_asked("skip", ALEX)

    w.run(confirm=True, now=now)

    assert f"Waiting until {until}" in w.questions[0]["text"]


def test_buttons_disappear_as_soon_as_someone_decides(world, bot_excel):
    w = world(jira_options=["Orbit Group"])
    w.bot_sends(bot_excel(ROWS))
    w.press_when_asked("add", ALEX)

    w.run(confirm=True)

    assert w.buttons_while_adding is False  # nobody can press again while Jira is being changed


def test_telegram_failing_while_waiting_turns_the_question_into_the_problem(world, bot_excel):
    w = world(jira_options=["Orbit Group"])
    w.bot_sends(bot_excel(ROWS))
    w.when_asked = lambda: w.tg.scripted.__setitem__("getUpdates", [network_down() for _ in range(1000)])

    code, note = w.run(confirm=True, now=datetime(2026, 11, 9, 17, 30))

    assert code == 1
    assert w.jira_writes == []
    assert [m["text"] for m in w.tg.sent] == [COMMAND, note]
    assert "reply_markup" not in w.tg.sent[-1]


def test_when_the_question_cannot_be_edited_the_result_is_posted_instead(world, bot_excel):
    w = world(jira_options=["Orbit Group"])
    w.bot_sends(bot_excel(ROWS))
    w.press_when_asked("add", ALEX)
    w.tg.scripted["editMessageText"] = [telegram_error(400, "Bad Request: message to edit not found")] * 5

    code, note = w.run(confirm=True)

    assert code == 0
    assert w.tg.sent[-1]["text"] == note
    assert len(w.tg.sent) == 3  # command, question, and the result as a new message


def test_an_unexpected_crash_after_asking_still_removes_the_buttons(world, bot_excel):
    w = world(jira_options=["Orbit Group"])
    w.bot_sends(bot_excel(ROWS))
    w.press_when_asked("add", ALEX, before=lambda: w.jira.scripted.append(RuntimeError("bug")))

    code, note = w.run(confirm=True)

    assert code == 1
    assert w.jira_writes == []
    assert [m["text"] for m in w.tg.sent] == [COMMAND, note]
    assert "reply_markup" not in w.tg.sent[-1]


def test_stopping_the_run_by_hand_still_removes_the_buttons(world, bot_excel):
    w = world(jira_options=["Orbit Group"])
    w.bot_sends(bot_excel(ROWS))
    w.when_asked = lambda: w.tg.scripted.__setitem__("getUpdates", [KeyboardInterrupt()])

    with pytest.raises(KeyboardInterrupt):
        w.run(confirm=True)

    assert w.jira_writes == []
    assert "reply_markup" not in w.tg.sent[-1]


def test_a_list_too_long_to_show_is_not_put_to_a_vote(world, bot_excel):
    rows = [(f"Merchant number {i:03d} " + "with a really long registered company name " * 2 + "ACME", NO_GROUP)
            for i in range(300)]
    w = world(jira_options=[])
    w.bot_sends(bot_excel(rows))

    code, note = w.run(confirm=True)

    assert code == 1
    assert w.questions == []  # nobody can approve names they can't see
    assert w.jira_writes == []
    assert w.posted == [note] and len(note) <= 4096


def test_a_dry_run_failure_is_not_posted(world, tmp_path):
    w = world(jira_options=["Orbit Group"])
    missing = tmp_path / "missing.xlsx"

    code, _ = w.run(excel=missing, dry_run=True)

    assert code == 1
    assert w.tg.sent == []


def test_new_names_skips_names_already_in_jira_ignoring_the_ignore_words_and_case():
    existing = [FieldOption("1", "Blue Harbor"), FieldOption("2", "NOVA BET")]

    assert new_names(["Blue Harbor ACME", "Vega Play", "nova bet acme"], existing, ("ACME",)) == ["Vega Play"]


def test_without_ignore_words_a_tagged_name_counts_as_a_different_name():
    existing = [FieldOption("1", "Blue Harbor")]

    assert new_names(["Blue Harbor ACME"], existing) == ["Blue Harbor ACME"]


def test_disabled_options_count_as_already_there():
    existing = [FieldOption("1", "Old Merchant ACME", disabled=True)]

    assert new_names(["Old Merchant ACME"], existing) == []


ENV = ("TELEGRAM_BOT_TOKEN=123:abc\nREPORT_BOT=@ReportBot\nTELEGRAM_GROUP_ID=-1001234567890\nREPORT_COMMAND=/report\n"
       "JIRA_URL=https://example.atlassian.net/\nJIRA_EMAIL=ops@example.com\nJIRA_API_TOKEN=tok\n")


def test_load_settings_requires_an_https_jira_url(tmp_path):
    env = tmp_path / ".env"
    env.write_text(ENV.replace("https://example", "http://example"), encoding="utf-8")

    with pytest.raises(SettingsError, match="JIRA_URL"):
        load_settings(env)


def test_load_settings_reads_an_env_file_saved_with_a_byte_order_mark(tmp_path):
    env = tmp_path / ".env"
    env.write_text(ENV, encoding="utf-8-sig")  # what PowerShell 5.1 writes

    assert load_settings(env).telegram_token == "123:abc"


def test_load_settings_reads_the_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text(ENV + "REPORT_FILE_PREFIX=Merchant report\nJIRA_FIELD=Client\nDROP_WORDS=PROMO, VIP\n"
                   "IGNORE_WORDS=ACME,Ltd , N.V.\nGROUPS_WITHOUT_MERCHANTS=Orbit Group , Zenith ACME Group\n"
                   "MERCHANT_COLUMN=Client\nGROUP_COLUMN=Holding\nCHECK_TOTAL_ROW=no\nBY_CONFIRMATION=False\n",
                   encoding="utf-8")

    assert load_settings(env) == Settings(
        telegram_token="123:abc", report_bot="ReportBot", group_id=-1001234567890, report_command="/report",
        jira_url="https://example.atlassian.net", jira_email="ops@example.com", jira_token="tok",
        report_file_prefix="Merchant report", jira_field="Client",
        name_rules=NameRules(drop_words=("PROMO", "VIP"), ignore_words=("ACME", "Ltd", "N.V."),
                             groups_without_merchants=("Orbit Group", "Zenith ACME Group"),
                             merchant_column="Client", group_column="Holding", check_total_row=False),
        by_confirmation=False,
    )


def test_optional_settings_have_safe_defaults(tmp_path):
    env = tmp_path / ".env"
    env.write_text(ENV, encoding="utf-8")

    settings = load_settings(env)

    assert settings.report_file_prefix == ""            # any .xlsx the report bot sends
    assert settings.jira_field == "Merchant"
    assert settings.name_rules == NameRules()           # names used as written, Total row checked
    assert settings.by_confirmation is True             # ask in the group first


@pytest.mark.parametrize("value, command", [("/report", "/report"), ("/report@ReportBot", "/report")])
def test_report_command_is_read_without_a_bot_name(tmp_path, value, command):
    env = tmp_path / ".env"
    env.write_text(ENV.replace("REPORT_COMMAND=/report", f"REPORT_COMMAND={value}"), encoding="utf-8")

    assert load_settings(env).report_command == command


def test_report_command_must_start_with_a_slash(tmp_path):
    env = tmp_path / ".env"
    env.write_text(ENV.replace("REPORT_COMMAND=/report", "REPORT_COMMAND=report"), encoding="utf-8")

    with pytest.raises(SettingsError, match="REPORT_COMMAND"):
        load_settings(env)


@pytest.mark.parametrize("value, expected", [
    ("True", True), ("true", True), ("yes", True), ("1", True),
    ("False", False), ("false", False), ("no", False), ("0", False),
])
def test_by_confirmation_comes_from_the_env_file(tmp_path, value, expected):
    env = tmp_path / ".env"
    env.write_text(ENV + f"BY_CONFIRMATION={value}\n", encoding="utf-8")

    assert load_settings(env).by_confirmation is expected


def test_env_key_names_are_not_case_sensitive(tmp_path):
    env = tmp_path / ".env"
    env.write_text(ENV + "BY_Confirmation=False\n", encoding="utf-8")

    assert load_settings(env).by_confirmation is False


def test_settings_ask_first_unless_told_otherwise():
    settings = Settings(telegram_token="123:abc", report_bot="ReportBot", group_id=GROUP, report_command="/report",
                        jira_url="https://example.atlassian.net", jira_email="ops@example.com", jira_token="tok")

    assert settings.by_confirmation is True


def test_without_by_confirmation_it_asks_first(tmp_path):
    env = tmp_path / ".env"
    env.write_text(ENV, encoding="utf-8")

    assert load_settings(env).by_confirmation is True


def test_by_confirmation_with_a_typo_is_an_error(tmp_path):
    env = tmp_path / ".env"
    env.write_text(ENV + "BY_CONFIRMATION=Treu\n", encoding="utf-8")

    with pytest.raises(SettingsError, match="BY_CONFIRMATION"):
        load_settings(env)


def test_load_settings_names_every_missing_or_placeholder_value(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nREPORT_BOT=ReportBot\nTELEGRAM_GROUP_ID=-1001234567890\n"
        "JIRA_URL=https://<your-site>.atlassian.net\nJIRA_API_TOKEN=\n",
        encoding="utf-8",
    )

    with pytest.raises(SettingsError) as caught:
        load_settings(env)

    for key in ("REPORT_COMMAND", "JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        assert key in str(caught.value)
    assert "TELEGRAM_BOT_TOKEN" not in str(caught.value)


@pytest.fixture
def project_dir(monkeypatch, tmp_path):
    """Point main() at a temporary project folder; close its log file afterwards."""
    import merchant_sync
    monkeypatch.setattr(merchant_sync, "HERE", tmp_path)
    monkeypatch.setattr(merchant_sync, "LOG_FILE", tmp_path / "logs" / "merchant_sync.log")
    monkeypatch.setattr(merchant_sync, "LOCK_FILE", tmp_path / "logs" / "merchant_sync.lock", raising=False)
    monkeypatch.setattr(merchant_sync.log, "handlers", [])
    yield tmp_path
    for handler in merchant_sync.log.handlers:
        handler.close()


def test_main_turns_command_line_options_into_run_options(project_dir, monkeypatch):
    import merchant_sync
    (project_dir / ".env").write_text(ENV, encoding="utf-8")
    seen = {}

    def fake_run(settings, telegram, jira, **options):
        seen.clear()
        seen.update(options, group=settings.group_id)
        return 0, "ok"
    monkeypatch.setattr(merchant_sync, "run_and_report", fake_run)

    assert merchant_sync.main(["--dry-run", "--no-limit", "--file", "x.xlsx"]) == 0
    assert seen == {"excel": Path("x.xlsx"), "dry_run": True, "limit": None, "group": -1001234567890}
    assert merchant_sync.main([]) == 0
    assert seen == {"excel": None, "dry_run": False, "limit": 25, "group": -1001234567890}


def test_a_second_run_does_not_start_while_one_is_still_running(project_dir, monkeypatch):
    import merchant_sync
    (project_dir / ".env").write_text(ENV, encoding="utf-8")
    runs = []
    monkeypatch.setattr(merchant_sync, "run_and_report", lambda *args, **options: runs.append(options) or (0, "ok"))

    other_run = merchant_sync._lock_single_run(merchant_sync.LOCK_FILE)  # e.g. Monday's run, waiting for a press
    assert other_run is not None
    try:
        assert merchant_sync.main([]) == 3
        assert merchant_sync.main(["--dry-run", "--file", "x.xlsx"]) == 0  # silent check never touches Telegram
    finally:
        merchant_sync._unlock(other_run)

    assert merchant_sync.main([]) == 0
    assert [r["dry_run"] for r in runs] == [True, False]


def test_main_stops_with_code_2_when_env_is_incomplete(project_dir):
    import merchant_sync
    (project_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=123:abc\n", encoding="utf-8")

    assert merchant_sync.main([]) == 2


def test_group_id_must_be_a_number(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nREPORT_BOT=ReportBot\nTELEGRAM_GROUP_ID=Ops\nREPORT_COMMAND=/report\n"
        "JIRA_URL=https://example.atlassian.net\nJIRA_EMAIL=ops@example.com\nJIRA_API_TOKEN=tok\n",
        encoding="utf-8",
    )

    with pytest.raises(SettingsError, match="TELEGRAM_GROUP_ID"):
        load_settings(env)
