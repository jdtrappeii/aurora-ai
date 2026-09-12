from datetime import timedelta
from decimal import Decimal

from app.analytics.comparisons import average_week, comparison_periods, weekly_comparison, weekly_trend
from app.analytics.financial import financial_summary, financial_summary_multi
from app.analytics.periods import Period, week_containing
from tests.conftest import MONDAY, days_ago, dt

SUNDAY = MONDAY + timedelta(days=6)


def build_week(seed):
    """Hand ledger for the week of 2026-09-07.

    Product A: cost 10, retail 25.  Product B: cost 4, retail 10.
    T1  2x A @25            gross 50   rev 50   cogs 20
    T2  1x A @20 (disc 5)   gross 25   rev 20   cogs 10
        3x B @10            gross 30   rev 30   cogs 12
    T3  1x B @10 REFUNDED   excluded from revenue; refund_amount 10
    T4  1x A @25 VOIDED     excluded entirely
    Completed: gross 105, discount 5, revenue 100, cogs 42, GP 58, GM 0.58
               transactions 2, units 6, ATV 50, UPT 3
    Opex: 30 rent + 12.5 utilities = 42.50 -> operating profit 15.50
    """
    seed.product("A", "Flower", "10", "25")
    seed.product("B", "Edibles", "4", "10")
    seed.sale(dt(MONDAY, 10), [("A", 2, "25")])
    seed.sale(dt(MONDAY, 11), [("A", 1, "20"), ("B", 3, "10")])
    seed.sale(dt(MONDAY, 12), [("B", 1, "10")], status="refunded")
    seed.sale(dt(MONDAY, 13), [("A", 1, "25")], status="voided")
    seed.expense(MONDAY, "30.00", "Rent")
    seed.expense(days_ago(-3), "12.50", "Utilities")  # Thursday, still in week
    seed.expense(days_ago(1), "999.00", "Rent")  # Sunday before: previous week, must not count
    seed.commit()


def test_financial_summary_matches_hand_ledger(session, seed):
    build_week(seed)
    s = financial_summary(session, week_containing(MONDAY))
    assert s.gross_sales == Decimal("105.00")
    assert s.discount_total == Decimal("5.00")
    assert s.discount_rate == Decimal("0.0476")  # 5 / 105
    assert s.revenue == Decimal("100.00")
    assert s.cogs == Decimal("42.00")
    assert s.gross_profit == Decimal("58.00")
    assert s.gross_margin == Decimal("0.5800")
    assert s.transactions == 2
    assert s.units == 6
    assert s.avg_transaction_value == Decimal("50.00")
    assert s.units_per_transaction == Decimal("3.0000")
    assert s.refund_count == 1
    assert s.refund_amount == Decimal("10.00")
    assert s.void_count == 1
    assert s.operating_expenses == Decimal("42.50")
    assert s.operating_profit == Decimal("15.50")


def test_empty_period_is_all_zero_not_error(session, seed):
    build_week(seed)
    s = financial_summary(session, week_containing(days_ago(60)))
    assert s.revenue == Decimal("0.00")
    assert s.gross_margin == Decimal("0.0000")
    assert s.avg_transaction_value == Decimal("0.00")
    assert s.transactions == 0


def test_store_filter_excludes_other_stores(session, seed):
    build_week(seed)
    assert financial_summary(session, week_containing(MONDAY), store_code="OTHER").revenue == Decimal("0.00")
    assert financial_summary(session, week_containing(MONDAY), store_code="MAIN").revenue == Decimal("100.00")


def test_comparison_periods_full_vs_partial():
    cur, prev, trailing, partial, days = comparison_periods(SUNDAY)
    assert not partial and days == 7
    assert (cur.start, cur.end) == (MONDAY, SUNDAY)
    assert (prev.start, prev.end) == (MONDAY - timedelta(days=7), MONDAY - timedelta(days=1))
    assert [t.days for t in trailing] == [7, 7, 7, 7]

    wed = MONDAY + timedelta(days=2)
    cur, prev, trailing, partial, days = comparison_periods(wed)
    assert partial and days == 3
    assert (cur.start, cur.end) == (MONDAY, wed)
    assert (prev.start, prev.end) == (MONDAY - timedelta(days=7), MONDAY - timedelta(days=5))  # Mon..Wed of last week
    assert [t.days for t in trailing] == [3, 3, 3, 3]
    assert trailing[0].start == MONDAY - timedelta(days=28)


def test_weekly_comparison_full_week_deltas(session, seed):
    """Current week revenue 100; previous week one sale of 4x B @10 = 40; the 4-week
    window holds only that sale so the 4-week average is 10/week."""
    build_week(seed)
    seed.sale(dt(days_ago(3), 15), [("B", 4, "10")])  # Friday of previous week
    seed.commit()
    cmp = weekly_comparison(session, SUNDAY)
    assert cmp["is_partial"] is False and cmp["days_elapsed"] == 7
    assert cmp["current_week"]["revenue"] == Decimal("100.00")
    assert cmp["previous_week"]["revenue"] == Decimal("40.00")
    assert cmp["four_week_average"]["revenue"] == Decimal("10.00")
    assert cmp["four_week_average"]["transactions"] == Decimal("0.2500")
    assert cmp["vs_previous_week"]["revenue"] == {"abs": Decimal("60.00"), "pct": Decimal("1.5000")}
    assert cmp["vs_four_week_average"]["revenue"]["pct"] == Decimal("9.0000")
    # ratio deltas are differences in points, not percentages of a percentage
    assert cmp["vs_previous_week"]["gross_margin"]["abs"] == Decimal("0.5800") - Decimal("0.6000")


def test_weekly_comparison_partial_week_uses_same_weekdays(session, seed):
    """as_of is Wednesday. Last week: Mon sale 40, Fri sale 60. Only the Monday
    counts in the week-to-date comparison; the Friday must not."""
    build_week(seed)
    seed.sale(dt(days_ago(7), 15), [("B", 4, "10")])  # last Monday: 40
    seed.sale(dt(days_ago(3), 15), [("B", 6, "10")])  # last Friday: 60, outside Mon..Wed
    seed.commit()
    wed = MONDAY + timedelta(days=2)
    cmp = weekly_comparison(session, wed)
    assert cmp["is_partial"] is True and cmp["days_elapsed"] == 3
    assert cmp["comparison_basis"].startswith("week-to-date")
    assert cmp["current_week"]["revenue"] == Decimal("100.00")
    assert cmp["current_week"]["operating_expenses"] == Decimal("30.00")  # Thursday utilities not yet in range
    assert cmp["previous_week"]["revenue"] == Decimal("40.00")
    assert cmp["four_week_average"]["revenue"] == Decimal("10.00")


def test_financial_summary_multi_adds_totals_and_reweights_ratios(session, seed):
    build_week(seed)
    seed.sale(dt(days_ago(7), 15), [("B", 4, "10")])  # 40 revenue, cogs 16 -> gm 0.6
    seed.commit()
    this_mon = Period("a", MONDAY, MONDAY)
    last_mon = Period("b", days_ago(7), days_ago(7))
    m = financial_summary_multi(session, [last_mon, this_mon])
    assert m.revenue == Decimal("140.00")
    assert m.gross_profit == Decimal("82.00")
    assert m.gross_margin == Decimal("0.5857")  # 82/140, not the mean of 0.58 and 0.60
    assert m.period["days"] == 2
    assert m.operating_expenses == Decimal("30.00")


def test_four_week_average_ratios_are_volume_weighted(session, seed):
    build_week(seed)
    window = financial_summary(session, week_containing(MONDAY))
    avg = average_week(window, 4)
    assert avg["revenue"] == Decimal("25.00")
    assert avg["gross_margin"] == window.gross_margin  # unchanged by dividing totals


def test_weekly_trend_orders_oldest_first_and_flags_partial(session, seed):
    build_week(seed)
    rows = weekly_trend(session, MONDAY, weeks=3)
    assert [r["week_start"] for r in rows] == ["2026-08-24", "2026-08-31", "2026-09-07"]
    assert rows[-1]["revenue"] == Decimal("100.00")
    assert [r["is_partial"] for r in rows] == [False, False, True]
