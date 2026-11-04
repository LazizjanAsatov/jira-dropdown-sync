"""Shared test helpers. All names here are made up."""
from pathlib import Path

import pytest
from openpyxl import Workbook

PROJECT_DIR = Path(__file__).resolve().parent.parent

HEADER = ("Merchant", "Group", "Owner", "Volume (8 weeks)", "Status")


def write_bot_excel(path: Path, rows, *, header=HEADER, title_block=True, blank_before_total=True,
                    footer=True, footer_count=None) -> Path:
    """Write a workbook laid out like a typical weekly merchant report from a reporting bot:
    a few title rows, the table, a blank row, then "Total — N merchants".

    rows: (merchant, group) pairs; None in the list writes a blank row there.
    The Total row counts the merchant rows unless footer_count says otherwise.
    """
    merchant_rows = [row for row in rows if row is not None]
    wb = Workbook()
    ws = wb.active
    ws.title = "Merchants"
    if title_block:
        ws.append([None])
        ws.append(["Merchant report"])
        ws.append(["Week ending 1 Nov 2026"])
        ws.append([f"{len(merchant_rows)} merchants"])
        ws.append([None])
    ws.append(list(header))
    for row in rows:
        if row is None:
            ws.append([None])
            continue
        merchant, group = row
        record = {"Merchant": merchant, "Group": group, "Owner": "Alex", "Volume (8 weeks)": 1250.5, "Status": None}
        ws.append([record.get(column) for column in header])
    if blank_before_total:
        ws.append([None])
    if footer:
        count = len(merchant_rows) if footer_count is None else footer_count
        total = {"Merchant": f"Total — {count} merchants"}
        ws.append([total.get(column) for column in header])
    wb.save(path)
    return path


@pytest.fixture
def bot_excel(tmp_path):
    """Factory: bot_excel(rows, **layout) -> path of a freshly written report."""
    def make(rows, **layout):
        return write_bot_excel(tmp_path / "Merchant report_2026-11-01.xlsx", rows, **layout)
    return make
