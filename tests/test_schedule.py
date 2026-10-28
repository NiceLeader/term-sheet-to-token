from datetime import date
from decimal import Decimal

import pytest

from tstt.reference import reference_flows
from tstt.schedule import build_schedule
from tstt.terms import build_terms


def terms(**overrides):
    values = {
        "currency": "EUR",
        "aggregate_nominal_amount": "100000000",
        "specified_denomination": "1000",
        "issue_date": "2023-10-23",
        "maturity_date": "2026-10-23",
        "interest_rate": "3.399",
        "coupon_frequency": 1,
        "first_interest_payment_date": "2024-10-23",
        "day_count_fraction": "ACT_ACT_ICMA",
        "business_day_convention": "NONE",
        "accrual_adjusted": False,
        "business_day": ["London", "TARGET", "New York"],
        "final_redemption_amount": "100",
        "rounding_rule": "HALF_UP",
    }
    values.update(overrides)
    return build_terms(values)


def assert_matches_quantlib(t):
    schedule = build_schedule(t)
    reference = reference_flows(t)
    coupons = [f for f in reference.flows if f.kind == "coupon"]
    assert len(coupons) == len(schedule.periods)
    for period, flow in zip(schedule.periods, coupons, strict=True):
        assert period.payment_date == flow.payment_date
        assert period.accrual_start == flow.accrual_start
        assert period.accrual_end == flow.accrual_end
        assert period.amount_per_basis == flow.amount, period
    redemption = [f for f in reference.flows if f.kind == "redemption"][0]
    assert schedule.redemption_date == redemption.payment_date
    assert schedule.redemption_per_basis == redemption.amount
    return schedule


def test_regular_annual_act_act_icma():
    schedule = assert_matches_quantlib(terms())
    assert [p.amount_per_basis for p in schedule.periods] == [Decimal("33.99")] * 3
    assert schedule.redemption_per_basis == Decimal("1000.00")


def test_short_first_coupon_act_act_icma_matches_printed_amount():
    # IBRD GBP 0.750% 2026: the final terms print GBP 6.45 per 1,000 for the first coupon.
    t = terms(currency="GBP", issue_date="2020-02-04", maturity_date="2026-12-15",
              interest_rate="0.750", first_interest_payment_date="2020-12-15")
    schedule = assert_matches_quantlib(t)
    assert schedule.periods[0].stub == "short_first"
    assert schedule.periods[0].amount_per_basis == Decimal("6.45")
    assert schedule.periods[1].amount_per_basis == Decimal("7.50")


def test_short_first_coupon_30_360_on_aggregate_amount():
    # KfW USD 4.50% 2031: first interest payment of USD 9,000,000 on USD 3bn.
    t = terms(currency="USD", aggregate_nominal_amount="3000000000",
              specified_denomination="3000000000", issue_date="2026-09-16",
              maturity_date="2031-10-10", interest_rate="4.50", coupon_frequency=2,
              first_interest_payment_date="2026-10-10", day_count_fraction="THIRTY_360",
              business_day_convention="FOLLOWING", business_day=["New York"])
    schedule = assert_matches_quantlib(t)
    assert schedule.periods[0].amount_per_basis == Decimal("9000000.00")


def test_long_first_coupon_30_360():
    # KfW USD 4.25% 2029: long first coupon of USD 69,062,500 on USD 3bn.
    t = terms(currency="USD", specified_denomination="3000000000", issue_date="2026-07-15",
              maturity_date="2029-07-30", interest_rate="4.25", coupon_frequency=2,
              first_interest_payment_date="2027-01-30", day_count_fraction="THIRTY_360",
              business_day_convention="FOLLOWING", business_day=["New York"])
    schedule = assert_matches_quantlib(t)
    assert schedule.periods[0].stub == "long_first"
    assert schedule.periods[0].amount_per_basis == Decimal("69062500.00")


def test_long_first_coupon_act_act_icma_splits_notional_periods():
    t = terms(issue_date="2023-06-01", first_interest_payment_date="2024-10-23",
              maturity_date="2026-10-23")
    schedule = assert_matches_quantlib(t)
    assert len(schedule.periods[0].segments) == 2


def test_following_moves_payment_past_weekend_and_us_holiday():
    # 10 October 2026 is a Saturday and 12 October 2026 is Columbus Day in New York.
    t = terms(currency="USD", issue_date="2026-04-10", maturity_date="2028-10-10",
              interest_rate="4.50", coupon_frequency=2,
              first_interest_payment_date="2026-10-10", day_count_fraction="THIRTY_360",
              business_day_convention="FOLLOWING", business_day=["New York"])
    schedule = assert_matches_quantlib(t)
    assert schedule.periods[0].payment_date == date(2026, 10, 13)
    assert schedule.periods[0].amount_per_basis == Decimal("22.50")


@pytest.mark.parametrize("rule, expected", [("HALF_UP", "18.13"), ("DOWN", "18.12"),
                                            ("HALF_EVEN", "18.12")])
def test_rounding_rule_decides_half_cent(rule, expected):
    # HKSAR USD 3.625% digital notes print USD 18.12 per 1,000 for 18.125.
    t = terms(currency="USD", issue_date="2025-11-12", maturity_date="2028-11-12",
              interest_rate="3.625", coupon_frequency=2,
              first_interest_payment_date="2026-05-12", day_count_fraction="THIRTY_360",
              rounding_rule=rule)
    schedule = assert_matches_quantlib(t)
    assert schedule.periods[0].amount_per_basis == Decimal(expected)


@pytest.mark.parametrize("day_count", ["THIRTY_E_360", "ACT_365_FIXED", "ACT_360"])
def test_other_day_counts_match_quantlib(day_count):
    t = terms(currency="SEK", issue_date="2026-04-22", maturity_date="2029-10-22",
              interest_rate="2.680", first_interest_payment_date="2026-10-22",
              day_count_fraction=day_count, business_day_convention="FOLLOWING",
              business_day=["Stockholm", "TARGET"])
    assert_matches_quantlib(t)


def test_modified_following_stays_in_month():
    t = terms(currency="HKD", issue_date="2025-11-11", maturity_date="2027-11-11",
              interest_rate="2.50", coupon_frequency=2,
              first_interest_payment_date="2026-05-11", day_count_fraction="ACT_365_FIXED",
              business_day_convention="MODIFIED_FOLLOWING", accrual_adjusted=True,
              business_day=["Hong Kong"])
    assert_matches_quantlib(t)


def hksar_usd(**overrides):
    # HKSAR USD 3.625% digital notes: USD 200,000 notes, calculation amount USD 1,000.
    return terms(currency="USD", aggregate_nominal_amount="300000000",
                 specified_denomination="200000", calculation_amount="1000",
                 issue_date="2025-11-12", maturity_date="2028-11-12", interest_rate="3.625",
                 coupon_frequency=2, first_interest_payment_date="2026-05-12",
                 day_count_fraction="THIRTY_360", rounding_rule="DOWN", **overrides)


def test_interest_on_a_holding_is_rounded_once():
    per_note = build_schedule(hksar_usd())
    on_holding = build_schedule(hksar_usd(interest_on_holding=True))
    period = on_holding.periods[0]
    assert per_note.coupon_for(per_note.periods[0], 1) == Decimal("3624.00")
    assert on_holding.coupon_for(period, 1) == Decimal("3625.00")
    for units in (1, 7, 750):
        ql = reference_flows(on_holding.terms, units * Decimal(200000)).flows[0]
        assert on_holding.coupon_for(period, units) == ql.amount


def test_quantlib_amounts_on_a_large_holding_keep_their_cents():
    # CNY 1.90% Actual/365 (Fixed), rounded down: float noise must not cost a fen.
    t = terms(currency="CNY", aggregate_nominal_amount="2500000000",
              specified_denomination="1000000", calculation_amount="10000",
              issue_date="2025-11-11", maturity_date="2030-11-11", interest_rate="1.90",
              coupon_frequency=2, first_interest_payment_date="2026-05-11",
              day_count_fraction="ACT_365_FIXED", rounding_rule="DOWN",
              interest_on_holding=True)
    schedule = build_schedule(t)
    coupons = [f for f in reference_flows(t, Decimal("2500000000")).flows if f.kind == "coupon"]
    assert [schedule.coupon_on(p, Decimal("2500000000")) for p in schedule.periods] == [
        f.amount for f in coupons]
