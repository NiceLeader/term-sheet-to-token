import pytest

from tstt.eval import normalise
from tstt.evidence import value_in_text


@pytest.mark.parametrize("name, value, text, found", [
    ("aggregate_nominal_amount", "1000000", "SEK 1,000,000,000 5. Specified Denomination", False),
    ("aggregate_nominal_amount", "750000000", "Aggregate Nominal Amount: CAD750,000,000 5.", True),
    ("specified_denomination", "100000", "Specified Denominations CHF 100,000 1(b)):", True),
    ("interest_rate", "2.0", "1.900 per cent. per annum, 2 years", False),
    ("interest_rate", "1.9", "Rate of Interest: 1.900 per cent. per annum", True),
    ("maturity_date", "2029-10-22", "Maturity Date: 22nd October, 2029", True),
    ("business_day_convention", "FOLLOWING", "B usiness Day Convention: Fo llowing", True),
    ("business_day_convention", "FOLLOWING", "Business Day Convention: Modified Following",
     False),
])
def test_value_in_text(name, value, text, found):
    assert value_in_text(name, value, [text]) is found


def test_redemption_written_per_denomination():
    text = "Final Redemption Amount of each GBP 1,000 per Specified Denomination Note"
    context = {"specified_denomination": "1000"}
    assert value_in_text("final_redemption_amount", "100", [text], context) is True
    assert value_in_text("final_redemption_amount", "101", [text], context) is False


def test_unchecked_fields_return_none():
    assert value_in_text("coupon_frequency", 1, ["annually"]) is None


def test_normalise_ignores_form():
    assert normalise("stated_coupon_amounts", []) is None
    assert normalise("rounding_rule", "NOT_STATED") is None
    assert normalise("interest_rate", "0.750") == normalise("interest_rate", "0.75")
    assert normalise("business_day", ["TARGET", "London"]) == normalise(
        "business_day", ["London", "TARGET"])
    assert normalise("record_date", "calendar day before due date") == normalise(
        "record_date", "Close of business on the calendar day before the due date for payment")
