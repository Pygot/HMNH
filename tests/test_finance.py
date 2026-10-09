# tests/test_finance.py
from agent.web.charts import (
    grouped_bars,
    line_plot,
    nice_top,
    row_bars,
)
from agent.config import (
    ChartTuning,
    FinanceTuning,
    StoreTuning,
)
from agent.finance import (
    format_amount,
    parse_amount,
    shift_month,
)
from agent.errors import InvalidRequest
from agent.finance import FinanceBook
from agent.store import Store
from datetime import datetime

import pytest
import time

NOON = datetime(2026, 6, 15, 12, 0).timestamp()


@pytest.fixture
def make(tmp_path):
    opened = []

    def build(**changes):
        store = Store(StoreTuning(path=tmp_path / "data" / "agent.db"))
        opened.append(store)
        return FinanceBook(store, FinanceTuning(**changes), lambda: NOON)

    yield build
    for store in opened:
        store.close()


@pytest.fixture
def book(make):
    return make()


def test_amounts_are_exact_and_strictly_validated():
    tuning = FinanceTuning()
    assert parse_amount("1 250,50", tuning) == 125_050
    assert parse_amount("0.01", tuning) == 1
    for bad in ("", "abc", "0", "-5", "1.234", "nan", "inf", "1e400", "99999999999999999"):
        with pytest.raises(InvalidRequest):
            parse_amount(bad, tuning)
    assert format_amount(125_050, tuning) == "1,250.50"
    assert format_amount(-5, tuning) == "-0.05"
    assert parse_amount("12", FinanceTuning(decimals=0)) == 12
    assert format_amount(12, FinanceTuning(decimals=0)) == "12"


def test_month_arithmetic_crosses_years():
    assert shift_month("2026-01", -1) == "2025-12"
    assert shift_month("2026-11", 3) == "2027-02"
    assert shift_month("2026-06", 0) == "2026-06"


def test_entries_are_validated_and_categorised(book):
    sales = book.add_category(" Sales ", "revenue")
    tools = book.add_category("Tools", "expense")
    with pytest.raises(InvalidRequest, match="already"):
        book.add_category("sales", "revenue")
    book.add_category("sales", "expense")
    with pytest.raises(InvalidRequest, match="name"):
        book.add_category(" ", "revenue")
    with pytest.raises(InvalidRequest, match="revenue or expense"):
        book.add_category("X", "gift")
    entry = book.add_entry(
        "2026-06-01", "revenue", "1000", category_id=sales.id, note="  Invoice 1 "
    )
    assert entry.amount == 100_000 and entry.month == "2026-06" and entry.category == "Sales"
    assert entry.note == "Invoice 1"
    with pytest.raises(InvalidRequest, match="same kind"):
        book.add_entry("2026-06-01", "expense", "5", category_id=sales.id)
    with pytest.raises(InvalidRequest, match="Dates"):
        book.add_entry("01/06/2026", "expense", "5")
    with pytest.raises(InvalidRequest, match="past"):
        book.add_entry("1999-01-01", "expense", "5")
    with pytest.raises(InvalidRequest, match="ahead"):
        book.add_entry("2030-01-01", "expense", "5")
    changed = book.update_entry(
        entry.id, day="2026-06-02", kind="expense", amount="40.5", category_id=tools.id, note="x"
    )
    assert changed.kind == "expense" and changed.amount == 4050 and changed.day == "2026-06-02"
    with pytest.raises(InvalidRequest, match="exist"):
        book.update_entry(
            "none", day="2026-06-02", kind="expense", amount="1", category_id=None, note=""
        )
    assert book.delete_entry(entry.id) is True and book.entry(entry.id) is None


def test_deleting_a_category_keeps_its_entries(book):
    tools = book.add_category("Tools", "expense")
    entry = book.add_entry("2026-06-01", "expense", "10", category_id=tools.id)
    assert book.categories()[0].entries == 1
    assert book.delete_category(tools.id) is True
    kept = book.entry(entry.id)
    assert kept is not None and kept.category == "" and kept.category_id is None
    book.rename_category(book.add_category("A", "expense").id, "B")
    with pytest.raises(InvalidRequest, match="exist"):
        book.rename_category("none", "C")


def test_limits_are_enforced(make):
    small = make(max_entries=2, max_categories=1)
    small.add_entry("2026-06-01", "revenue", "1")
    small.add_entry("2026-06-01", "revenue", "1")
    with pytest.raises(InvalidRequest, match="too many entries"):
        small.add_entry("2026-06-01", "revenue", "1")
    small.add_category("A", "revenue")
    with pytest.raises(InvalidRequest, match="too many categories"):
        small.add_category("B", "revenue")


def test_monthly_totals_cover_a_full_window_with_empty_months(book):
    book.add_entry("2026-06-10", "revenue", "100")
    book.add_entry("2026-06-11", "expense", "30")
    book.add_entry("2026-04-02", "revenue", "50")
    book.add_entry("2025-01-02", "revenue", "999")
    months = book.monthly(4)
    assert [item.month for item in months] == ["2026-03", "2026-04", "2026-05", "2026-06"]
    assert [item.revenue for item in months] == [0, 5000, 0, 10_000]
    assert months[-1].expense == 3000 and months[-1].net == 7000
    assert book.totals("2026-03", "2026-06") == {"revenue": 15_000, "expense": 3000}
    assert book.months_with_data() == ["2026-06", "2026-04", "2025-01"]


def test_category_totals_collapse_the_tail_into_one_row(book):
    for index in range(5):
        category = book.add_category(f"C{index}", "expense")
        book.add_entry("2026-06-01", "expense", str(10 * (index + 1)), category_id=category.id)
    book.add_entry("2026-06-01", "expense", "1")
    top = book.by_category("expense", limit=3)
    assert [item.name for item in top] == ["C4", "C3", "Everything else"]
    assert top[-1].total == (30 + 20 + 10 + 1) * 100
    assert book.by_category("expense", limit=20)[-1].name == "Uncategorised"
    with pytest.raises(InvalidRequest):
        book.by_category("gift")


def test_the_page_filters_by_kind_category_and_month(book):
    tools = book.add_category("Tools", "expense")
    book.add_entry("2026-06-01", "expense", "5", category_id=tools.id)
    book.add_entry("2026-05-01", "revenue", "5")
    assert book.page(kind="expense").total == 1
    assert book.page(category_id=tools.id).total == 1
    assert book.page(month="2026-05").items[0].kind == "revenue"
    assert book.page(month="not-a-month").total == 2
    assert book.page(size=1, offset=1).items[0].day == "2026-05-01"
    assert len(book.all_entries()) == 2 and book.count() == 2


def test_the_currency_label_is_validated_and_remembered(book, make):
    assert book.currency() == "USD" and book.money(125_050) == "1,250.50 USD"
    assert book.set_currency(" eur ") == "EUR"
    assert make().currency() == "EUR"
    with pytest.raises(InvalidRequest, match="three letter"):
        book.set_currency("euros")
    assert book.compact(1_500_000_00) == "1.5M" and book.compact(12_00_000) == "12k"
    assert book.compact(55_00) == "55" and book.plain(125_050) == "1250.50"


def test_charts_scale_to_nice_numbers_and_stay_inside_the_frame():
    assert nice_top(0, 4) == 1.0
    assert nice_top(930, 4) == 1000.0
    assert nice_top(4, 4) == 4.0
    tuning = ChartTuning()
    plot = grouped_bars(
        ["Jan", "A very long month label"],
        [("Revenue", [900, 100]), ("Expenses", [300, 0])],
        str,
        tuning,
        "Chart",
    )
    assert len(plot.bars) == 4 and plot.bars[0].height > plot.bars[1].height > 0
    assert plot.bars[3].height == 0 and len(plot.ticks) == tuning.ticks + 1
    assert all(bar.y + bar.height <= plot.base_y + 0.1 for bar in plot.bars)
    assert plot.marks[1].label.endswith(".") and len(plot.marks[1].label) <= tuning.label_chars
    assert [item.name for item in plot.legend] == ["Revenue", "Expenses"]
    lines = line_plot(["a", "b", "c"], [("Searches", [1, 4, 2])], str, tuning, "Line")
    assert len(lines.lines[0][1].split()) == 3 and lines.bars == []
    rows = row_bars([("One", 100), ("Two", 50)], str, tuning, "Rows")
    assert rows.rows[0].width > rows.rows[1].width > 0 and rows.height >= 2 * tuning.row_height
    empty = row_bars([], str, tuning, "None")
    assert empty.rows == [] and empty.height >= tuning.row_height
    assert grouped_bars([], [], str, tuning, "Empty").bars == []


def test_hostile_amounts_are_rejected_instantly():
    tuning = FinanceTuning()
    started = time.monotonic()
    for raw in (
        "1e999990",
        "1e1000000",
        "9" * 5000,
        "1" + "0" * 40,
        "-5",
        "NaN",
        "Infinity",
        "0x10",
        "٣",
    ):
        with pytest.raises(InvalidRequest):
            parse_amount(raw, tuning)
    assert time.monotonic() - started < 1
    assert parse_amount("1 234,50", tuning) == 123_450
    assert parse_amount("10.500", tuning) == 1050
