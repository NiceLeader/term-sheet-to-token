import json
import shutil

import pytest

from tstt import review
from tstt.config import Settings
from tstt.review import (
    _patched,
    corrected,
    discard,
    load_review,
    mark_issued,
    normalise,
    read_truth,
    record,
    review_path,
    reviewed_report_path,
    reviewed_values,
    truth_path,
    unreviewed,
)
from tstt.terms import FIELDS, TermsError

BOND = "ibrd-gbp-0750-2026"
RUN = "agent-test-model"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    truth = tmp_path / "groundtruth"
    truth.mkdir()
    shutil.copy(review.GROUND_TRUTH / f"{BOND}.json", truth)
    monkeypatch.setattr(review, "GROUND_TRUTH", truth)
    return Settings(tmp_path / "data", None, "", "", "", "", None, None)


def test_values_are_normalised_to_the_ground_truth_format():
    assert normalise("aggregate_nominal_amount", "1,500,000,000") == "1500000000"
    assert normalise("interest_rate", 0.75) == "0.75"
    assert normalise("interest_rate", "0.750") == "0.750"
    assert normalise("issue_date", "2020-02-04") == "2020-02-04"
    assert normalise("business_day", [" London", "", "New York"]) == ["London", "New York"]
    assert normalise("stated_coupon_amounts", [{"amount": "7.50", "per": "1,000"}]) == [
        {"date": None, "amount": "7.50", "per": "1000"}]
    assert normalise("record_date", "  ") is None


@pytest.mark.parametrize(("name", "value"), [
    ("day_count_fraction", "ACT/ACT"), ("accrual_adjusted", "yes"), ("issue_date", "4 Feb 2020"),
    ("interest_rate", "three"), ("interest_payment_dates", ["15 December"]),
    ("coupon_frequency", True), ("coupon_frequency", "twice"), ("coupon_frequency", 1.5),
    ("coupon_frequency", "1.5"), ("stated_coupon_amounts", "7.50 per 1000"), ("no_such", 1),
])
def test_invalid_corrections_are_refused(name, value):
    with pytest.raises(TermsError):
        normalise(name, value)


def test_untouched_ground_truth_files_are_reproduced_exactly():
    for path in sorted(review.GROUND_TRUTH.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        assert _patched(text, json.loads(text)) == text, path.name


def test_correcting_the_ground_truth_changes_only_that_line(settings):
    before = truth_path(BOND).read_bytes()
    record(settings, "groundtruth", BOND, "interest_rate", "correct", "0.750", "0.85")
    after = truth_path(BOND).read_bytes().decode("utf-8")
    changed = [(a, b) for a, b in zip(before.decode("utf-8").splitlines(), after.splitlines(),
                                      strict=True)
               if a != b]
    assert len(changed) == 1 and '"value": "0.85"' in changed[0][1]
    assert read_truth(BOND)["status"] == "draft"
    entry = load_review(settings, "groundtruth", BOND)["fields"]["interest_rate"]
    assert entry["status"] == "corrected" and entry["original"] == "0.750"

    record(settings, "groundtruth", BOND, "interest_rate", "reset", "0.85")
    assert truth_path(BOND).read_bytes() == before


def test_a_correction_back_to_the_source_value_is_an_approval(settings):
    record(settings, RUN, BOND, "currency", "correct", "EUR", "GBP")
    record(settings, RUN, BOND, "currency", "correct", "EUR", "EUR")
    assert load_review(settings, RUN, BOND)["fields"]["currency"]["status"] == "approved"


def test_reviewing_an_extraction_leaves_the_model_values_alone(settings):
    fields = {"currency": {"value": "USD", "stated_in": []},
              "interest_rate": {"value": "0.750", "stated_in": []}}
    record(settings, RUN, BOND, "currency", "correct", "USD", "GBP")
    record(settings, RUN, BOND, "interest_rate", "approve", "0.750")
    current = load_review(settings, RUN, BOND)
    assert reviewed_values(fields, current)["currency"]["value"] == "GBP"
    assert fields["currency"]["value"] == "USD"
    assert corrected(current)
    assert len(unreviewed(current)) == len(FIELDS) - 2


def test_approving_an_invalid_model_value_is_refused(settings):
    with pytest.raises(TermsError):
        record(settings, RUN, BOND, "day_count_fraction", "approve", "Actual/Actual")


def test_issuance_marks_the_ground_truth_verified_and_a_change_withdraws_it(settings):
    for spec in FIELDS:
        record(settings, "groundtruth", BOND, spec.name, "approve",
               read_truth(BOND)["fields"][spec.name]["value"])
    mark_issued(settings, "groundtruth", BOND, {"errors": 0})
    assert read_truth(BOND)["status"] == "verified"

    record(settings, "groundtruth", BOND, "currency", "approve", "GBP")
    assert load_review(settings, "groundtruth", BOND)["issued"] is not None

    record(settings, "groundtruth", BOND, "currency", "reset", "GBP")
    assert load_review(settings, "groundtruth", BOND)["issued"] is None
    assert read_truth(BOND)["status"] == "draft"


def test_a_verified_file_without_a_review_record_counts_as_reviewed(settings):
    data = read_truth(BOND)
    data["status"] = "verified"
    review.write_truth(BOND, data)
    current = load_review(settings, "groundtruth", BOND)
    assert not unreviewed(current) and current["issued"] is not None


def _replayed(settings):
    path = reviewed_report_path(settings, RUN, BOND)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    return path


def test_only_a_change_of_the_reviewed_terms_drops_their_replay(settings):
    record(settings, RUN, BOND, "currency", "correct", "USD", "GBP")
    report = _replayed(settings)
    record(settings, RUN, BOND, "interest_rate", "approve", "0.750")
    assert report.exists()
    record(settings, RUN, BOND, "currency", "reset", "USD")
    assert not report.exists()


def test_reading_a_bond_again_forgets_the_review_of_the_earlier_reading(settings):
    record(settings, RUN, BOND, "currency", "approve", "GBP")
    report = _replayed(settings)
    discard(settings, RUN, BOND)
    assert not review_path(settings, RUN, BOND).exists() and not report.exists()
    assert unreviewed(load_review(settings, RUN, BOND)) == [f.name for f in FIELDS]
