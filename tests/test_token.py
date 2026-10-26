from datetime import date

import pytest
from test_schedule import terms

from tstt.calendars import Calendar
from tstt.schedule import build_schedule
from tstt.token import TokenError, forge_binary, record_date, record_rule, replay


@pytest.mark.parametrize("phrase, kind, days", [
    ("Close of business on the calendar day before the due date for payment", "calendar", 1),
    ("Fifteenth day (whether or not a Business Day) next preceding the Interest Payment Date",
     "calendar", 15),
    ("11:30 p.m. Hong Kong time on the Clearing System Business Day immediately preceding "
     "the due date", "business", 1),
    ("Close of business one Business Day before the Interest Payment Date while in global "
     "book-entry form (15th calendar day before if in definitive form)", "business", 1),
    ("Tenth New York business day prior to the relevant payment date", "business", 10),
    ("Close of business in Sydney on the date falling 8 calendar days before each Interest "
     "Payment Date", "calendar", 8),
    (None, "business", 1),
])
def test_record_rule_phrases(phrase, kind, days):
    rule = record_rule(phrase)
    assert (rule.kind, rule.days) == (kind, days)


def test_fixed_record_dates_pick_the_latest_before_payment():
    rule = record_rule("February 24 and August 27 preceding the applicable Interest Payment Date")
    calendar = Calendar(["New York"])
    assert record_date(rule, date(2027, 3, 10), calendar) == date(2027, 2, 24)
    assert record_date(rule, date(2027, 9, 10), calendar) == date(2027, 8, 27)


def test_business_day_record_date_skips_weekend():
    rule = record_rule("one Business Day before the Interest Payment Date")
    assert record_date(rule, date(2026, 10, 12), Calendar(["TARGET"])) == date(2026, 10, 9)


def _have_foundry() -> bool:
    try:
        forge_binary("forge")
        forge_binary("anvil")
    except TokenError:
        return False
    return True


@pytest.mark.skipif(not _have_foundry(), reason="Foundry is not installed")
def test_replay_pays_holders_of_record_and_redeems():
    # IBRD GBP 0.750% 2026: short first coupon of GBP 6.45 per GBP 1,000.
    t = terms(currency="GBP", issue_date="2020-02-04", maturity_date="2026-12-15",
              interest_rate="0.750", first_interest_payment_date="2020-12-15",
              record_date="Close of business on the calendar day before the due date")
    schedule = build_schedule(t)
    result = replay(schedule)
    coupons = [p for p in result.payments if p.kind == "coupon"]
    assert [p.per_basis_minor for p in coupons] == [645] + [750] * 6
    for plan, paid in zip(result.config.plan, result.payments, strict=True):
        assert paid.per_basis_minor == plan.per_basis_minor
        assert paid.executed >= paid.scheduled
    # A sells to C after the first record date and is still paid on the old holding;
    # A buys from B on the second record date and is already paid on the new one.
    sold, bought = (t["units"] for t in result.transfers)
    assert coupons[0].holders["A"][0] == result.allocation["A"]
    assert coupons[1].holders["A"][0] == result.allocation["A"] - sold + bought
    for paid in result.payments:
        assert {k: units for k, (units, _) in paid.holders.items()} == paid.expected_units
    assert all(probe.passed for probe in result.probes)
    assert result.final_units == {"A": 0, "B": 0, "C": 0}
