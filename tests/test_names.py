"""Name rules: which names from the report go to the Jira field. All names here are made up."""
import logging
import zipfile
from dataclasses import replace

import pytest
from openpyxl import Workbook

from names import NO_GROUP, ExcelFormatError, NameRules, match_key, read_names

# "ACME" plays your company's tag on merchant names, "PROMO" a word that marks a variant of a merchant
RULES = NameRules(drop_words=("PROMO",), ignore_words=("ACME",))


def test_reads_merchant_then_group_for_each_row_in_file_order(bot_excel):
    path = bot_excel([("Nova Bet ACME", "Nova Group"), ("Orbit Games Ltd ACME", "Orbit")])

    assert read_names(path, RULES) == ["Nova Bet ACME", "Nova Group", "Orbit Games Ltd ACME", "Orbit"]


def test_without_rules_names_are_used_as_written(bot_excel):
    path = bot_excel([("Nova Bet ACME PROMO", "Nova Group"), ("Nova Bet", NO_GROUP)])

    assert read_names(path) == ["Nova Bet ACME PROMO", "Nova Group", "Nova Bet"]


def test_total_row_is_not_a_name_even_without_a_blank_row_before_it(bot_excel):
    path = bot_excel([("Lumen Pay ACME", "Lumen Group")], blank_before_total=False)

    assert read_names(path, RULES) == ["Lumen Pay ACME", "Lumen Group"]


@pytest.mark.parametrize("merchant", ["Total-Bet ACME", "TOTAL - Gaming Ltd", "Total Casino ACME", "Total - 5 Stars Casino"])
def test_a_merchant_whose_name_starts_with_total_is_a_normal_merchant(bot_excel, merchant):
    path = bot_excel([(merchant, NO_GROUP), ("Lumen Pay ACME", "Lumen Group")])

    assert read_names(path, RULES) == [merchant, "Lumen Pay ACME", "Lumen Group"]


def test_a_blank_row_inside_the_table_does_not_drop_the_rows_after_it(bot_excel):
    path = bot_excel([("Nova Bet ACME", "Orbit Group"), None, ("Lumen Pay ACME", "Lumen Group")])

    assert read_names(path, RULES) == ["Nova Bet ACME", "Orbit Group", "Lumen Pay ACME", "Lumen Group"]


def test_rows_read_must_match_the_count_in_the_total_row(bot_excel):
    path = bot_excel([("Nova Bet ACME", "Orbit Group"), ("Lumen Pay ACME", "Lumen Group")], footer_count=3)

    with pytest.raises(ExcelFormatError, match="3"):
        read_names(path, RULES)


def test_excel_without_the_total_row_is_rejected(bot_excel):
    path = bot_excel([("Nova Bet ACME", "Orbit Group")], footer=False)

    with pytest.raises(ExcelFormatError, match="Total"):
        read_names(path, RULES)


def test_the_total_row_check_can_be_switched_off(bot_excel):
    path = bot_excel([("Nova Bet ACME", "Orbit Group"), None, ("Lumen Pay ACME", NO_GROUP)], footer=False)

    assert read_names(path, NameRules(check_total_row=False)) == ["Nova Bet ACME", "Orbit Group", "Lumen Pay ACME"]


def test_drop_words_are_removed_from_merchants_so_their_rows_merge(bot_excel):
    path = bot_excel([
        ("Lumen Pay ACME", "Orbit Group"),
        ("Lumen Pay ACME PROMO", "Orbit Group"),
        ("Atlas PROMO ACME", NO_GROUP),
    ])

    assert read_names(path, RULES) == ["Lumen Pay ACME", "Orbit Group", "Atlas ACME"]


def test_ignore_words_are_never_removed_from_any_name(bot_excel):
    path = bot_excel([
        ("Lumen Pay ACME", "Zenith ACME Group"),
        ("ACME Gaming", NO_GROUP),
        ("Blue Harbor ACME PROMO", "Vega Play"),
    ])

    assert read_names(path, RULES) == ["Lumen Pay ACME", "Zenith ACME Group", "ACME Gaming", "Blue Harbor ACME",
                                       "Vega Play"]


@pytest.mark.parametrize("no_group", [NO_GROUP, "- no group -", None, "   "])
def test_rows_without_a_group_only_give_the_merchant(bot_excel, no_group):
    path = bot_excel([("Lumen Pay ACME", no_group)])

    assert read_names(path, RULES) == ["Lumen Pay ACME"]


@pytest.mark.parametrize("group", ["Casino Group", "No Group Ltd", "Nogroup Media"])
def test_only_the_no_group_placeholder_is_skipped_not_real_groups(bot_excel, group):
    path = bot_excel([("Lasino ACME", group)])

    assert read_names(path, RULES) == ["Lasino ACME", group]


@pytest.mark.parametrize("junk", ["—", "-", "…", "?"])
def test_a_value_without_any_letter_or_digit_is_never_a_name(bot_excel, junk):
    path = bot_excel([("Lumen Pay ACME", junk)])

    assert read_names(path, RULES) == ["Lumen Pay ACME"]


def test_groups_are_kept_exactly_as_written(bot_excel):
    path = bot_excel([("ZN Kite ACME", "Zenith ACME Group"), ("Maple Tech ACME PROMO", "Maple group")])

    assert read_names(path, RULES) == ["ZN Kite ACME", "Zenith ACME Group", "Maple Tech ACME", "Maple group"]


def test_same_name_in_different_case_or_spacing_is_added_once_first_spelling_wins(bot_excel):
    path = bot_excel([("NOVA LTD ACME", NO_GROUP), ("Nova  Ltd ACME", NO_GROUP)])

    assert read_names(path, RULES) == ["NOVA LTD ACME"]


def test_names_that_differ_only_by_ignore_words_count_once_and_that_spelling_is_used(bot_excel):
    path = bot_excel([
        ("Blue Harbor ACME", "Vega Play"),
        ("Vega Play ACME", "Vega Play"),
        ("Kite Ltd (x.com) PROMO", "Kite Group"),
        ("Kite Ltd (x.com) ACME", "Kite Group"),
    ])

    assert read_names(path, RULES) == ["Blue Harbor ACME", "Vega Play ACME", "Kite Ltd (x.com) ACME", "Kite Group"]


# --- groups whose merchants are left out (GROUPS_WITHOUT_MERCHANTS) ---------------------------------------


def test_a_group_listed_without_merchants_gives_only_the_group_name(bot_excel):
    path = bot_excel([
        ("Nova Bet ACME", "Orbit Group"),
        ("Blue Harbor ACME", "Vega Play"),
        ("Lumen Pay ACME PROMO", "Orbit Group"),
    ])

    names = read_names(path, replace(RULES, groups_without_merchants=("Orbit Group",)))

    assert names == ["Orbit Group", "Blue Harbor ACME", "Vega Play"]


@pytest.mark.parametrize("listed", ["orbit group", "  ORBIT   Group ", "Orbit ACME Group"])
def test_a_listed_group_matches_whatever_its_case_spacing_or_ignore_words(bot_excel, listed):
    path = bot_excel([("Nova Bet ACME", "Orbit Group")])

    assert read_names(path, replace(RULES, groups_without_merchants=(listed,))) == ["Orbit Group"]


def test_a_listed_group_must_be_the_whole_group_name(bot_excel):
    path = bot_excel([("Nova Bet ACME", "Orbit Group")])

    assert read_names(path, replace(RULES, groups_without_merchants=("Orbit",))) == ["Nova Bet ACME", "Orbit Group"]


def test_the_group_column_decides_who_is_left_out_not_the_merchant_name(bot_excel):
    path = bot_excel([
        ("Orbit Pay ACME", NO_GROUP),
        ("Orbit Games ACME", "Vega Play"),
        ("Nova Bet ACME", "Orbit Group"),
    ])

    names = read_names(path, replace(RULES, groups_without_merchants=("Orbit Group",)))

    assert names == ["Orbit Pay ACME", "Orbit Games ACME", "Vega Play", "Orbit Group"]


def test_a_listed_name_made_only_of_ignore_words_never_leaves_out_merchants_without_a_group(bot_excel):
    path = bot_excel([("Lumen Pay ACME", NO_GROUP), ("Nova Bet ACME", "Orbit Group")])

    names = read_names(path, replace(RULES, groups_without_merchants=("ACME",)))

    assert names == ["Lumen Pay ACME", "Nova Bet ACME", "Orbit Group"]


def test_a_listed_group_that_is_not_in_the_report_is_warned_about(bot_excel, caplog):
    path = bot_excel([("Nova Bet ACME", "Orbit Group"), ("Lumen Pay ACME", NO_GROUP)])

    with caplog.at_level(logging.WARNING):
        names = read_names(path, replace(RULES, groups_without_merchants=("Orbit Group", "Zenith Group")))

    assert names == ["Orbit Group", "Lumen Pay ACME"]
    [warning] = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert "Zenith Group" in warning


def test_header_is_found_even_when_the_title_block_is_gone(bot_excel):
    path = bot_excel([("Lumen Pay ACME", "Lumen Group")], title_block=False)

    assert read_names(path, RULES) == ["Lumen Pay ACME", "Lumen Group"]


def test_columns_are_found_by_their_header_not_their_position(bot_excel):
    header = ("Owner", "Group", "Status", "Merchant", "Volume (8 weeks)")
    path = bot_excel([("Lumen Pay ACME", "Lumen Group")], header=header)

    assert read_names(path, RULES) == ["Lumen Pay ACME", "Lumen Group"]


def test_column_names_can_be_configured(tmp_path):
    wb = Workbook()
    wb.active.append(["Client", "Holding", "Owner"])
    wb.active.append(["Lumen Pay", "Lumen Holding", "Alex"])
    wb.active.append(["Total — 1 clients", None, None])
    path = tmp_path / "clients.xlsx"
    wb.save(path)

    assert read_names(path, NameRules(merchant_column="client", group_column="Holding")) == ["Lumen Pay", "Lumen Holding"]


def test_excel_without_merchant_and_group_header_is_rejected(tmp_path):
    wb = Workbook()
    wb.active.append(["Name", "Team"])
    wb.active.append(["Lumen Pay ACME", "Lumen Group"])
    path = tmp_path / "other.xlsx"
    wb.save(path)

    with pytest.raises(ExcelFormatError):
        read_names(path, RULES)


def test_a_zip_that_is_not_an_excel_is_rejected(tmp_path):
    path = tmp_path / "Merchant report_2026-11-01.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("readme.txt", "not a workbook")

    with pytest.raises(ExcelFormatError):
        read_names(path, RULES)


def test_an_excel_with_a_damaged_sheet_is_rejected(bot_excel, tmp_path):
    good = bot_excel([("Lumen Pay ACME", "Lumen Group")])
    damaged = tmp_path / "damaged.xlsx"
    with zipfile.ZipFile(good) as source, zipfile.ZipFile(damaged, "w") as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename.startswith("xl/worksheets/sheet"):
                data = data[: len(data) // 2]  # sheet XML cut off half way
            target.writestr(item, data)

    with pytest.raises(ExcelFormatError):
        read_names(damaged, RULES)


def test_a_file_that_is_not_really_an_excel_is_rejected(tmp_path):
    path = tmp_path / "Merchant report_2026-11-01.xlsx"
    path.write_bytes(b"<html>Bad Gateway</html>")

    with pytest.raises(ExcelFormatError):
        read_names(path, RULES)


def test_excel_with_header_but_no_merchants_is_rejected(bot_excel):
    path = bot_excel([])

    with pytest.raises(ExcelFormatError):
        read_names(path, RULES)


@pytest.mark.parametrize("name, key", [
    ("Blue Harbor", "blue harbor"),
    ("  blue  HARBOR ACME ", "blue harbor"),
    ("Zenith ACME Group", "zenith group"),
    ("ACMEPay Ltd", "acmepay ltd"),
    ("Orbit Group", "orbit group"),
])
def test_match_key_ignores_the_ignore_words_case_and_spaces(name, key):
    assert match_key(name, ("ACME",)) == key


def test_ignore_words_can_contain_dots():
    assert match_key("Stable Tech N.V.", ("N.V.",)) == match_key("Stable Tech", ("N.V.",)) == "stable tech"


def test_without_ignore_words_only_case_and_spaces_are_ignored():
    assert match_key("  Blue  HARBOR ACME ") == "blue harbor acme"


def test_drop_words_are_removed_from_group_names_too(bot_excel):
    rules = NameRules(drop_words=("Group",))
    path = bot_excel([("Comeon Casino", "Comeon Group"), ("Heycasino", "Heycasino GROUP"), ("Nova Bet", NO_GROUP)])

    assert read_names(path, rules) == ["Comeon Casino", "Comeon", "Heycasino", "Nova Bet"]


@pytest.mark.parametrize("listed", ["Alpha Group", "Alpha"])
def test_groups_without_merchants_match_with_or_without_the_dropped_words(bot_excel, listed, caplog):
    rules = NameRules(drop_words=("Group",), groups_without_merchants=(listed,))
    path = bot_excel([("Alpha Bet", "Alpha Group"), ("Alpha Play", "Alpha Group"), ("Nova Bet", "Nova Group")])

    with caplog.at_level(logging.WARNING):
        assert read_names(path, rules) == ["Alpha", "Nova Bet", "Nova"]
    assert not caplog.records


def test_group_names_can_be_left_out_so_only_merchants_are_added(bot_excel):
    path = bot_excel([("Nova Bet ACME", "Orbit Group"), ("Lumen Pay ACME", NO_GROUP), ("Vega Play ACME", "Vega Play")])

    names = read_names(path, replace(RULES, add_group_names=False))

    assert names == ["Nova Bet ACME", "Lumen Pay ACME", "Vega Play ACME"]


def test_without_group_names_a_listed_group_gives_nothing_at_all(bot_excel, caplog):
    path = bot_excel([("Nova Bet ACME", "Orbit Group"), ("Blue Harbor ACME", "Vega Play")])
    rules = replace(RULES, add_group_names=False, groups_without_merchants=("Orbit Group",))

    with caplog.at_level(logging.WARNING):
        assert read_names(path, rules) == ["Blue Harbor ACME"]
    assert not caplog.records
