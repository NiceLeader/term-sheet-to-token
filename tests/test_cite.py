from tstt.cite import Citations, check_fields
from tstt.docs import Document, Page

TERMS = Document("ft", "Final Terms", "pdf", [
    Page(1, "PART A - CONTRACTUAL TERMS\n1. Issuer: International Bank for Reconstruction "
            "and Development"),
    Page(2, "16. Fixed Rate Note Provisions: Applicable\n(i) Rate of Interest: 3.399 per cent. "
            "per annum pay-\nable annually in arrear\n(vi) Day Count Fraction | Actual/Actual "
            "(ICMA)"),
])
BASE = Document("base", "Prospectus", "pdf", [
    Page(1, "“Actual/Actual (ICMA)” means the actual number of days in the "
            "Calculation Period divided by the product of the number of days in the Determination "
            "Period and the number of Determination Periods in one year"),
])


def check(doc, page, quote):
    return Citations([TERMS, BASE]).check(doc, page, quote)


def test_exact_quote_ignores_punctuation_tables_and_hyphenation():
    assert check("ft", 2, "Rate of Interest: 3.399 per cent. per annum payable annually").status \
        == "exact"
    assert check("ft", 2, "Day Count Fraction: Actual/Actual (ICMA)").status == "exact"
    assert check("base", 1, '"Actual/Actual (ICMA)" means the actual number of days').status \
        == "exact"


def test_changed_number_is_never_a_near_match():
    quote = "Rate of Interest: 3.499 per cent. per annum payable annually in arrear"
    assert check("ft", 2, quote).valid is False
    assert check("ft", 2, quote.replace("3.499", "3.399").replace("in", "")).status == "fuzzy"
    assert check("ft", 2, "Rate of Interest: 4.25 per cent. per annum").valid is False


def test_quote_on_another_page_is_reported_with_the_right_page():
    result = check("ft", 1, "Rate of Interest: 3.399 per cent. per annum")
    assert result.status == "wrong_page"
    assert result.found_page == 2
    assert check("ft", 2, "the number of Determination Periods in one year").status == "wrong_doc"


def test_invented_quote_is_not_found():
    assert check("ft", 2, "interest is payable semi-annually on each Interest Payment Date") \
        .status == "not_found"


def test_check_fields_marks_support():
    fields = {"interest_rate": {
        "value": "3.399",
        "stated_in": [{"doc": "ft", "page": 2, "quote": "Rate of Interest: 3.399 per cent."}],
        "defined_in": [],
    }}
    result = check_fields(fields, Citations([TERMS, BASE]))
    assert result["interest_rate"].supported
