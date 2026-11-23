"""Turn a reporting bot's Excel (one row per merchant, with its group) into option names for a Jira field.

The rules are configurable (see NameRules):
- drop_words are removed from merchant names, so "Nova PROMO" merges into "Nova".
- ignore_words don't make two names different ("Nova ACME" is the same as "Nova"), but they're kept
  in the names that get added; when two spellings differ only by them, the one that has them wins.
- Groups are used exactly as written; placeholders like "— no group —" and empty cells are skipped.
- groups_without_merchants give only the group's name: the merchants in them (by the group column) are left out.
- add_group_names=False leaves out the other groups' names, so only merchants (and those listed groups) are added.
- Upper/lowercase and extra spaces never make two names different.
"""
import logging
import re
import zipfile
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from xml.parsers.expat import ExpatError

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

NO_GROUP = "— no group —"

log = logging.getLogger("merchant_sync.names")  # under the run's logger, so warnings reach its log file

# What openpyxl raises for files that aren't a workbook or are damaged inside
_UNREADABLE = (zipfile.BadZipFile, InvalidFileException, KeyError, SyntaxError, ExpatError, zlib.error, EOFError)
_NO_GROUP = re.compile(r"^[\W_]*no group[\W_]*$", re.IGNORECASE)
_FOOTER = re.compile(r"^total\s*[—–-]\s*(\d+)(?:\s+\w+)?$", re.IGNORECASE)  # "Total — 222 merchants"


class ExcelFormatError(ValueError):
    """The Excel doesn't look like the report we expect."""


@dataclass(frozen=True)
class NameRules:
    drop_words: tuple[str, ...] = ()     # removed from merchant names, e.g. a marker for a variant of a merchant
    ignore_words: tuple[str, ...] = ()   # never make two names different, e.g. your own company tag or "Ltd"
    groups_without_merchants: tuple[str, ...] = ()  # these groups give only their own name, not their merchants'
    add_group_names: bool = True         # False: only groups_without_merchants give a group name
    merchant_column: str = "Merchant"    # header of the merchant column
    group_column: str = "Group"          # header of the group column
    check_total_row: bool = True         # the rows read must match a "Total — N ..." row under the table


def match_key(name: str, ignore_words: Iterable[str] = ()) -> str:
    """What two names must share to count as the same name."""
    pattern = _words_pattern(tuple(ignore_words))
    if pattern:
        name = pattern.sub(" ", name)
    return _tidy(name).casefold()


def read_names(path: str | Path, rules: NameRules = NameRules()) -> list[str]:
    """Merchant and group names from the report, cleaned and without repeats, in file order.

    A group in rules.groups_without_merchants gives only its own name: its merchants are left out.
    """
    drop = _words_pattern(tuple(rules.drop_words))
    # a listed name made only of ignore words would otherwise match every merchant without a group
    without_merchants = {match_key(group, rules.ignore_words) for group in rules.groups_without_merchants} - {""}
    found: set[str] = set()
    picked: dict[str, str] = {}
    for merchant, group in _table_rows(Path(path), rules):
        group_name = _clean_group(group)
        group_key = match_key(group_name, rules.ignore_words)
        if group_key in without_merchants:
            found.add(group_key)
            _keep(picked, group_name, rules.ignore_words)  # the group stands in for its merchants
        else:
            merchant_name = _tidy(drop.sub(" ", merchant)) if drop else merchant
            _keep(picked, merchant_name, rules.ignore_words)
            if rules.add_group_names:
                _keep(picked, group_name, rules.ignore_words)
    if not picked:
        raise ExcelFormatError(f"{Path(path).name}: no merchants under the header row")
    for group in rules.groups_without_merchants:
        if match_key(group, rules.ignore_words) not in found:  # a typo, or the group was renamed
            log.warning("GROUPS_WITHOUT_MERCHANTS lists %r, but %s has no group by that name, "
                        "so none of its merchants are left out. Check the spelling in .env.", group, Path(path).name)
    return list(picked.values())


def _keep(picked: dict[str, str], name: str, ignore_words: tuple[str, ...]) -> None:
    if not any(char.isalnum() for char in name):
        return  # "—", "-", "?": a placeholder, never a merchant or group
    key = match_key(name, ignore_words)
    if not key:
        return
    current = picked.get(key)
    if current is None or _count(name, ignore_words) > _count(current, ignore_words):
        picked[key] = name  # replacing a value keeps the name's original position


def _count(name: str, words: tuple[str, ...]) -> int:
    pattern = _words_pattern(tuple(words))
    return len(pattern.findall(name)) if pattern else 0


@lru_cache(maxsize=64)
def _words_pattern(words: tuple[str, ...]) -> re.Pattern | None:
    """Whole words only ("ACME" never matches inside "ACMEPay"); words may contain dots, like "N.V."."""
    cleaned = sorted({word.strip() for word in words if word and word.strip()}, key=len, reverse=True)
    if not cleaned:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(word) for word in cleaned) + r")(?!\w)", re.IGNORECASE)


def _clean_group(value) -> str:
    group = _tidy(value)
    return "" if _NO_GROUP.match(group) else group


def _table_rows(path: Path, rules: NameRules) -> list[tuple[str, object]]:
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except _UNREADABLE as exc:
        raise ExcelFormatError(f"{path.name}: not a readable Excel file ({exc})") from None
    try:
        for sheet in workbook.worksheets:
            rows = sheet.iter_rows(values_only=True)
            for row in rows:
                columns = _header_columns(row, rules)
                if columns:
                    return _read_table(rows, *columns, check_total_row=rules.check_total_row, file_name=path.name)
    except _UNREADABLE as exc:
        raise ExcelFormatError(f"{path.name}: the Excel is damaged ({exc})") from None
    finally:
        workbook.close()
    raise ExcelFormatError(f"{path.name}: no header row with {rules.merchant_column!r} and "
                           f"{rules.group_column!r} columns")


def _header_columns(row: tuple, rules: NameRules) -> tuple[int, int] | None:
    cells = [_tidy(cell).casefold() for cell in row]
    merchant, group = rules.merchant_column.strip().casefold(), rules.group_column.strip().casefold()
    if merchant in cells and group in cells:
        return cells.index(merchant), cells.index(group)
    return None


def _read_table(rows: Iterator[tuple], merchant_col: int, group_col: int, *, check_total_row: bool,
                file_name: str) -> list[tuple[str, object]]:
    """Merchant rows under the header, up to a "Total — N ..." row or the end of the sheet.

    Blank rows are skipped. With check_total_row, the Total row is the check that nothing was missed:
    if its number doesn't match the rows read, or it's missing, the file is rejected.
    """
    table: list[tuple[str, object]] = []
    for row in rows:
        merchant = _tidy(_cell(row, merchant_col))
        total = _FOOTER.match(merchant)
        if total:
            expected = int(total.group(1))
            if check_total_row and expected != len(table):
                raise ExcelFormatError(f"{file_name}: the Total row says {expected} but {len(table)} rows were read")
            return table
        if merchant:
            table.append((merchant, _cell(row, group_col)))
    if check_total_row:
        raise ExcelFormatError(f"{file_name}: no 'Total — N' row under the table, so the rows can't be checked")
    return table


def _cell(row: tuple, index: int):
    return row[index] if index < len(row) else None


def _tidy(value) -> str:
    return " ".join(str(value).split()) if value is not None else ""
