"""Telegram side: ask the report bot for the Excel in the group and download it. All names are made up."""
import pytest

from telegram_bot import TelegramBot, TelegramError
from tests.fakes import FakeClock, FakeResponse, FakeTelegramSession, network_down, telegram_error

TOKEN = "123456789:TEST-TOKEN-not-real"
GROUP = -1001234567890
OTHER_CHAT = -1009999999999
COMMAND = "/report@ReportBot"
REPORT_BOT = {"id": 7000000001, "is_bot": True, "first_name": "Report Bot", "username": "ReportBot"}
PERSON = {"id": 51000001, "is_bot": False, "first_name": "Alex", "username": "alex"}
NO_USERNAME = {"id": 51000002, "is_bot": False, "first_name": "Sam"}
BUTTONS = [("✅ Add them", "add"), ("❌ Don't add", "skip")]


@pytest.fixture
def session():
    return FakeTelegramSession(TOKEN)


def make_bot(session, sleeps: list | None = None) -> TelegramBot:
    session.clock = session.clock or FakeClock(step=30)  # every look at the clock is 30 s later
    sleeps = [] if sleeps is None else sleeps
    return TelegramBot(TOKEN, session=session, clock=session.clock, sleep=sleeps.append)


def request_file(bot: TelegramBot, tmp_path):
    return bot.request_file(GROUP, COMMAND, from_username="ReportBot",
                            name_prefix="Merchant report", wait_seconds=180, save_dir=tmp_path / "downloads")


def answer_with(session, *replies):
    """When our bot writes the command, the given messages arrive in the group."""
    def on_send(sent):
        return [build(session) for build in replies] if sent["text"] == COMMAND else []
    session.on_send = on_send


def report(session, content: bytes, *, chat=GROUP, sender=REPORT_BOT, name="Merchant report_2026-11-01.xlsx"):
    return lambda s: s.message(chat, sender, document=s.document(name, content))


def test_writes_the_command_in_the_group_and_saves_the_excel_that_comes_back(session, tmp_path):
    answer_with(session, report(session, b"fresh report"))

    path = request_file(make_bot(session), tmp_path)

    assert [(m["chat"]["id"], m["text"]) for m in session.sent] == [(GROUP, COMMAND)]
    assert path == tmp_path / "downloads" / "Merchant report_2026-11-01.xlsx"
    assert path.read_bytes() == b"fresh report"


def test_an_old_excel_already_in_the_group_is_ignored(session, tmp_path):
    session.push(session.message(GROUP, REPORT_BOT, document=session.document("Merchant report_2026-10-25.xlsx", b"old")))
    answer_with(session, report(session, b"fresh report"))

    path = request_file(make_bot(session), tmp_path)

    assert path.read_bytes() == b"fresh report"


def test_an_excel_written_just_before_our_command_is_ignored(session, tmp_path):
    def on_send(sent):
        if sent["text"] != COMMAND:
            return []
        earlier = session.message(GROUP, REPORT_BOT, document=session.document("Merchant report_2026-10-25.xlsx", b"old"))
        earlier["message_id"] = sent["message_id"] - 1  # written before our command, delivered after it
        fresh = session.message(GROUP, REPORT_BOT, document=session.document("Merchant report_2026-11-01.xlsx", b"fresh"))
        return [earlier, fresh]
    session.on_send = on_send

    path = request_file(make_bot(session), tmp_path)

    assert path.read_bytes() == b"fresh"


def test_a_short_network_blip_while_waiting_is_retried(session, tmp_path):
    answer_with(session, report(session, b"fresh report"))
    session.scripted["getUpdates"] = [network_down()]
    sleeps = []

    path = request_file(make_bot(session, sleeps), tmp_path)

    assert path.read_bytes() == b"fresh report"
    assert len(sleeps) == 1


def test_telegram_asking_to_slow_down_is_respected(session, tmp_path):
    answer_with(session, report(session, b"fresh report"))
    session.scripted["getUpdates"] = [FakeResponse(429, {
        "ok": False, "error_code": 429, "description": "Too Many Requests: retry after 7",
        "parameters": {"retry_after": 7}})]
    sleeps = []

    path = request_file(make_bot(session, sleeps), tmp_path)

    assert path.read_bytes() == b"fresh report"
    assert sleeps == [7]


def test_a_question_is_posted_with_the_buttons_under_it(session):
    question = make_bot(session).send_message(GROUP, "3 new names to add", buttons=BUTTONS)

    assert question["reply_markup"] == {"inline_keyboard": [[
        {"text": "✅ Add them", "callback_data": "add"},
        {"text": "❌ Don't add", "callback_data": "skip"},
    ]]}


@pytest.mark.parametrize("user, who", [(PERSON, "@alex"), (NO_USERNAME, "Sam")])
def test_wait_for_press_says_which_button_and_who_pressed_it(session, user, who):
    bot = make_bot(session)
    question = bot.send_message(GROUP, "3 new names to add", buttons=BUTTONS)
    press_id = session.press(question, user, "add")

    assert bot.wait_for_press(GROUP, question["message_id"], wait_seconds=3600) == ("add", who)
    assert [a["callback_query_id"] for a in session.answered] == [press_id]  # its spinner stops


def test_a_press_hours_later_is_still_caught(session):
    bot = make_bot(session)
    question = bot.send_message(GROUP, "3 new names to add", buttons=BUTTONS)
    session.press(question, PERSON, "add", after_seconds=5 * 3600)

    assert bot.wait_for_press(GROUP, question["message_id"], wait_seconds=8 * 3600) == ("add", "@alex")


def test_the_wait_survives_the_network_being_down_for_a_while(session):
    bot = make_bot(session)
    question = bot.send_message(GROUP, "3 new names to add", buttons=BUTTONS)
    session.press(question, PERSON, "add")
    session.scripted["getUpdates"] = [network_down() for _ in range(8)]  # minutes, not seconds

    assert bot.wait_for_press(GROUP, question["message_id"], wait_seconds=8 * 3600) == ("add", "@alex")


def test_another_copy_polling_at_the_same_time_is_waited_out(session):
    bot = make_bot(session)
    question = bot.send_message(GROUP, "3 new names to add", buttons=BUTTONS)
    session.press(question, PERSON, "skip")
    session.scripted["getUpdates"] = [telegram_error(
        409, "Conflict: terminated by other getUpdates request; make sure that only one bot instance is running")]

    assert bot.wait_for_press(GROUP, question["message_id"], wait_seconds=3600) == ("skip", "@alex")


def test_telegram_unreachable_until_the_deadline_is_an_error_not_silence(session):
    bot = make_bot(session)
    question = bot.send_message(GROUP, "3 new names to add", buttons=BUTTONS)
    session.raise_error = network_down()

    with pytest.raises(TelegramError):
        bot.wait_for_press(GROUP, question["message_id"], wait_seconds=3600)


def test_editing_is_retried_after_a_network_blip(session):
    bot = make_bot(session)
    question = bot.send_message(GROUP, "3 new names to add", buttons=BUTTONS)
    session.scripted["editMessageText"] = [network_down()]

    bot.edit_message(GROUP, question["message_id"], "Added 3, approved by @alex")

    assert session.sent[-1]["text"] == "Added 3, approved by @alex"


def test_editing_to_the_text_it_already_has_counts_as_done(session):
    bot = make_bot(session)
    question = bot.send_message(GROUP, "3 new names to add", buttons=BUTTONS)
    bot.edit_message(GROUP, question["message_id"], "Added 3, approved by @alex")

    bot.edit_message(GROUP, question["message_id"], "Added 3, approved by @alex")  # e.g. a retry after a timeout


def test_a_press_on_an_older_question_is_answered_but_ignored(session):
    bot = make_bot(session)
    old = bot.send_message(GROUP, "last week's names", buttons=BUTTONS)
    question = bot.send_message(GROUP, "this week's names", buttons=BUTTONS)
    session.press(old, PERSON, "add")
    session.press(question, PERSON, "skip")

    assert bot.wait_for_press(GROUP, question["message_id"], wait_seconds=3600) == ("skip", "@alex")
    assert len(session.answered) == 2


def test_nobody_pressing_in_time_gives_no_answer(session):
    bot = make_bot(session)
    question = bot.send_message(GROUP, "3 new names to add", buttons=BUTTONS)

    assert bot.wait_for_press(GROUP, question["message_id"], wait_seconds=600) is None


def test_editing_a_message_sends_only_the_new_text_so_the_buttons_go_away(session):
    bot = make_bot(session)
    question = bot.send_message(GROUP, "3 new names to add", buttons=BUTTONS)

    bot.edit_message(GROUP, question["message_id"], "Added 3, approved by @alex")

    assert session.calls[-1] == ("editMessageText", {"chat_id": GROUP, "message_id": question["message_id"],
                                                     "text": "Added 3, approved by @alex"})


def test_every_update_request_asks_for_both_messages_and_button_presses(session, tmp_path):
    answer_with(session, report(session, b"fresh report"))
    bot = make_bot(session)
    request_file(bot, tmp_path)
    question = bot.send_message(GROUP, "3 new names to add", buttons=BUTTONS)
    session.press(question, PERSON, "add")
    bot.wait_for_press(GROUP, question["message_id"], wait_seconds=3600)

    filters = [params.get("allowed_updates") for method, params in session.calls if method == "getUpdates"]
    assert filters and all(f == ["message", "callback_query"] for f in filters)


def test_writing_a_message_is_never_retried(session):
    session.scripted["sendMessage"] = [network_down()]

    with pytest.raises(TelegramError):
        make_bot(session).send_message(GROUP, "Jira \"Merchant\" field: nothing new")
    assert len([call for call in session.calls if call[0] == "sendMessage"]) == 1


def test_only_the_report_bots_excel_in_this_group_is_taken(session, tmp_path):
    answer_with(
        session,
        report(session, b"from a person", sender=PERSON),
        report(session, b"other chat", chat=OTHER_CHAT),
        report(session, b"wrong file", name="weekly volumes.pdf"),
        lambda s: s.message(GROUP, REPORT_BOT, text="Building the report..."),
        report(session, b"the right one"),
    )

    path = request_file(make_bot(session), tmp_path)

    assert path.read_bytes() == b"the right one"


def test_no_excel_within_the_wait_time_is_an_error(session, tmp_path):
    answer_with(session, lambda s: s.message(GROUP, REPORT_BOT, text="Building the report..."))

    with pytest.raises(TelegramError, match="ReportBot"):
        request_file(make_bot(session), tmp_path)


def test_telegram_refusing_the_token_is_an_error(tmp_path):
    session = FakeTelegramSession("some-other-token")

    with pytest.raises(TelegramError, match="Unauthorized"):
        request_file(make_bot(session), tmp_path)


def test_errors_never_show_the_bot_token(session, tmp_path):
    session.raise_error = network_down().__class__(f"Max retries exceeded with url: /bot{TOKEN}/getUpdates")

    with pytest.raises(TelegramError) as caught:
        request_file(make_bot(session), tmp_path)

    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
