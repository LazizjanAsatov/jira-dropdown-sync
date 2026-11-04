"""Jira side: read the Merchant field's options and add new ones, nothing else. All names are made up."""
import pytest

from jira_field import FieldOption, JiraError, JiraField
from tests.fakes import NOT_JSON, FakeJiraSession, FakeResponse, _context, _field, network_down

BASE = "https://example.atlassian.net"


def make_field(session: FakeJiraSession, sleeps: list | None = None) -> JiraField:
    sleeps = [] if sleeps is None else sleeps
    return JiraField(BASE, "ops@example.com", "jira-token", session=session, sleep=sleeps.append)


def test_options_reads_every_page():
    session = FakeJiraSession(options=[f"Merchant {i} ACME" for i in range(250)])

    values = [o.value for o in make_field(session).options()]

    assert len(values) == 250
    assert values[0] == "Merchant 0 ACME"
    assert values[-1] == "Merchant 249 ACME"


def test_options_keep_disabled_ones_marked_as_disabled():
    session = FakeJiraSession(options=["Orbit Group", ("Old Merchant ACME", True)])

    assert make_field(session).options() == [
        FieldOption(id="10001", value="Orbit Group", disabled=False),
        FieldOption(id="10002", value="Old Merchant ACME", disabled=True),
    ]


def test_field_is_found_by_its_exact_name_not_a_partial_match():
    session = FakeJiraSession(fields=[_field("customfield_10049", "Merchant ID"), _field("customfield_10050", "Merchant")])
    session.options[("customfield_10050", "10150")] = [{"id": "1", "value": "Blue Harbor", "disabled": False}]

    assert [o.value for o in make_field(session).options()] == ["Blue Harbor"]


def test_two_fields_with_the_same_name_is_an_error():
    session = FakeJiraSession(fields=[_field("customfield_10050", "Merchant"), _field("customfield_10077", "merchant")])

    with pytest.raises(JiraError, match="2"):
        make_field(session).options()


def test_missing_field_is_an_error():
    session = FakeJiraSession(fields=[_field("customfield_10049", "Merchant ID")])

    with pytest.raises(JiraError, match="Merchant"):
        make_field(session).options()


def test_options_come_from_the_global_context():
    session = FakeJiraSession(contexts={"customfield_10050": [
        _context("10200", "Only for one space", is_global=False),
        _context("10150", "Default Configuration Scheme for Merchant", is_global=True),
    ]})
    session.options[("customfield_10050", "10200")] = [{"id": "9", "value": "Scoped only", "disabled": False}]
    session.options[("customfield_10050", "10150")] = [{"id": "1", "value": "Vega Exchange", "disabled": False}]

    assert [o.value for o in make_field(session).options()] == ["Vega Exchange"]


def test_add_options_appends_the_values_to_the_end_of_the_list():
    session = FakeJiraSession(options=["Orbit Group", "Blue Harbor"])

    added = make_field(session).add_options(["Nova Bet ACME", "Lumen Group"])

    assert [o.value for o in added] == ["Nova Bet ACME", "Lumen Group"]
    assert session.option_values() == ["Orbit Group", "Blue Harbor", "Nova Bet ACME", "Lumen Group"]


def test_add_options_with_nothing_to_add_sends_no_request():
    session = FakeJiraSession(options=["Orbit Group"])

    assert make_field(session).add_options([]) == []
    assert [c for c in session.calls if c[0] == "POST"] == []


def test_only_ever_reads_or_adds_options():
    session = FakeJiraSession(options=["Orbit Group"])
    field = make_field(session)

    field.options()
    field.add_options(["Nova Bet ACME"])

    assert {method for method, *_ in session.calls} == {"GET", "POST"}
    assert [path for method, path, *_ in session.calls if method == "POST"] == [
        "/rest/api/3/field/customfield_10050/context/10150/option"
    ]


def test_http_error_becomes_jira_error_with_the_status():
    session = FakeJiraSession()
    session.fail_with = (401, "Client must be authenticated to access this resource.")

    with pytest.raises(JiraError, match="401"):
        make_field(session).options()


def test_network_failure_becomes_jira_error():
    session = FakeJiraSession()
    session.raise_error = network_down()

    with pytest.raises(JiraError):
        make_field(session).options()


def test_adding_fails_loudly_when_jira_did_not_create_what_was_sent():
    session = FakeJiraSession(options=["Orbit Group"])
    session.post_reply = {"maxResults": 100, "startAt": 0, "total": 1, "isLast": True,  # an options *page*,
                          "values": [{"id": "10001", "value": "Orbit Group", "disabled": False}]}  # e.g. POST->GET

    with pytest.raises(JiraError, match="Nova Bet ACME"):
        make_field(session).add_options(["Nova Bet ACME"])


def test_a_reply_that_is_not_json_is_a_jira_error():
    session = FakeJiraSession()
    session.scripted = [FakeResponse(200, NOT_JSON)]

    with pytest.raises(JiraError):
        make_field(session).options()


def test_a_read_that_fails_for_a_moment_is_retried():
    session = FakeJiraSession(options=["Orbit Group"])
    session.scripted = [network_down(), FakeResponse(503, {"errorMessages": ["Service Unavailable"]})]
    sleeps = []

    assert [o.value for o in make_field(session, sleeps).options()] == ["Orbit Group"]
    assert len(sleeps) == 2


def test_reads_give_up_after_three_retries():
    session = FakeJiraSession()
    session.scripted = [FakeResponse(503, {"errorMessages": ["Service Unavailable"]}) for _ in range(4)]

    with pytest.raises(JiraError, match="503"):
        make_field(session).options()
    assert len(session.calls) == 4


def test_jira_asking_to_slow_down_is_respected():
    session = FakeJiraSession(options=["Orbit Group"])
    session.scripted = [FakeResponse(429, {"errorMessages": ["Rate limit"]}, headers={"Retry-After": "7"})]
    sleeps = []

    make_field(session, sleeps).options()

    assert sleeps == [7]


def test_adding_options_is_never_retried():
    session = FakeJiraSession(options=["Orbit Group"])
    session.fail_post_with = (503, "Service Unavailable")

    with pytest.raises(JiraError, match="503"):
        make_field(session).add_options(["Nova Bet ACME"])
    assert len([call for call in session.calls if call[0] == "POST"]) == 1
