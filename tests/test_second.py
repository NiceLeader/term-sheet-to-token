import json
import os

import pytest

from tstt.config import Settings
from tstt.extract import Extraction, Usage
from tstt.second import compare_runs, differing, pick_second, readings

TERMS = {"currency": "GBP", "interest_rate": "0.750", "business_day": ["London", "TARGET"],
         "rounding_rule": "HALF_UP", "stated_coupon_amounts": [], "record_date": None}


def test_differences_of_form_do_not_count():
    other = TERMS | {"interest_rate": "0.75", "business_day": ["T2", "London"],
                     "stated_coupon_amounts": None, "record_date": ""}
    assert differing(TERMS, other) == []


def test_a_different_value_or_a_missing_one_is_a_disagreement():
    other = TERMS | {"rounding_rule": "DOWN", "currency": None}
    assert differing(TERMS, other) == ["currency", "rounding_rule"]


def _save(folder, bond_id, model, mode, values, error=None):
    fields = {name: {"value": value, "stated_in": [], "defined_in": [], "note": None}
              for name, value in values.items()}
    Extraction(bond_id, mode, model, fields, Usage(0, 0, 0), 1.0, None, error=error).save(folder)


def test_the_default_second_reading_is_another_model(tmp_path):
    settings = Settings(tmp_path, None, "", "", "", "", None, None)
    runs = settings.runs_dir / "extractions"
    _save(runs / "agent-spark", "bond", "spark", "agent", TERMS)
    _save(runs / "one_pass-spark", "bond", "spark", "one_pass", TERMS)
    _save(runs / "agent-glimmer", "bond", "glimmer", "agent", TERMS)
    _save(runs / "agent-failed", "bond", "other", "agent", {}, error="timed out")
    choices = readings(settings, "bond")
    assert sorted(choices) == ["agent-glimmer", "agent-spark", "one_pass-spark"]
    choices.pop("agent-spark")
    assert pick_second(choices, "spark") == "agent-glimmer"
    choices.pop("agent-glimmer")
    assert pick_second(choices, "spark") == "one_pass-spark"
    assert pick_second({}, "spark") is None


def test_two_runs_are_compared_against_the_verified_terms(tmp_path):
    settings = Settings(tmp_path, None, "", "", "", "", None, None)
    runs = settings.runs_dir / "extractions"
    truth = tmp_path / "groundtruth"
    truth.mkdir()
    (truth / "bond.json").write_text(json.dumps(
        {"fields": {name: {"value": value} for name, value in TERMS.items()}}), encoding="utf-8")
    # A reads the rate wrong, B the rounding; both read the currency wrong in the same way.
    _save(runs / "a", "bond", "spark", "agent",
          TERMS | {"interest_rate": "0.7", "currency": "GBX"})
    _save(runs / "b", "bond", "glimmer", "agent",
          TERMS | {"rounding_rule": "DOWN", "currency": "GBX"})
    (settings.runs_dir / "eval").mkdir()
    (settings.runs_dir / "eval" / "a.json").write_text(json.dumps(
        {"bonds": [{"bond_id": "bond", "wrong_flagged": ["currency"]}]}), encoding="utf-8")
    result = compare_runs(settings, "a", "b", truth)
    row = result["bonds"][0]
    assert row["differing"] == ["interest_rate", "rounding_rule"]
    assert row["wrong_a"] == {"currency": {"differs": False, "flagged": True},
                              "interest_rate": {"differs": True, "flagged": False}}
    assert row["wrong_b"] == {"currency": {"differs": False, "flagged": False},
                              "rounding_rule": {"differs": True, "flagged": False}}
    summary = result["summary"]
    assert (summary["wrong_values"], summary["wrong_differing"],
            summary["wrong_flagged_by_checks"], summary["wrong_caught_either"]) == (4, 2, 1, 3)
    assert summary["checks_from_eval"] is False  # b was never scored
    assert summary["checks_missing"] == ["b"]

    # A score older than the reading it scored, or one that misses a bond, does not count.
    scored = settings.runs_dir / "eval" / "a.json"
    old = scored.stat().st_mtime - 100
    os.utime(scored, (old, old))
    result = compare_runs(settings, "a", "b", truth)
    assert result["summary"]["checks_missing"] == ["a", "b"]
    assert result["bonds"][0]["wrong_a"]["currency"]["flagged"] is False
    assert result["summary"]["wrong_flagged_by_checks"] == 0


def test_a_missing_run_or_bond_is_an_error(tmp_path):
    settings = Settings(tmp_path, None, "", "", "", "", None, None)
    runs = settings.runs_dir / "extractions"
    _save(runs / "a", "bond", "spark", "agent", TERMS)
    _save(runs / "b", "bond", "glimmer", "agent", TERMS)
    with pytest.raises(FileNotFoundError, match="no run folder c"):
        compare_runs(settings, "a", "c", tmp_path)
    with pytest.raises(FileNotFoundError, match="no reading of other"):
        compare_runs(settings, "a", "b", tmp_path, ["bond", "other"])
    assert compare_runs(settings, "a", "b", tmp_path, ["bond"])["summary"]["bonds"] == 1
