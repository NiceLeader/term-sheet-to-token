import json

from tstt.checks import check_terms
from tstt.review import GROUND_TRUTH
from tstt.schedule import build_schedule
from tstt.terms import build_terms


def _values(bond_id: str) -> dict:
    data = json.loads((GROUND_TRUTH / f"{bond_id}.json").read_text(encoding="utf-8"))
    return {name: f.get("value") for name, f in data["fields"].items()}


def _codes(values: dict, severity: str) -> set[str]:
    terms = build_terms(values)
    return {f.code for f in check_terms(terms, build_schedule(terms)) if f.severity == severity}


def test_first_period_of_two_regular_periods_is_an_error():
    values = _values("ibrd-eur-3399-2026")  # annual, first payment a year after issue
    assert "first_period" not in _codes(values, "error")
    late = values | {"first_interest_payment_date": "2025-10-23"}
    assert "first_period" in _codes(late, "error")


def test_long_first_coupon_is_not_an_error():
    values = _values("kfw-usd-4250-2029")  # half-yearly, 199-day first period
    assert "first_period" not in _codes(values, "error")


def test_interest_before_the_issue_date_is_a_warning():
    values = _values("paccar-usd-4750-2029")
    assert "accrual_start" not in _codes(values, "warning")
    early = values | {"interest_commencement_date": "2026-08-10"}
    assert "accrual_start" in _codes(early, "warning")
    assert "first_period" not in _codes(early, "error")
