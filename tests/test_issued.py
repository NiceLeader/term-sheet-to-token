import json

from tstt.config import Settings
from tstt.issued import digest, issuance_record, load_record, record_path, save_record
from tstt.terms import FIELDS

MANIFEST = {
    "documents": {
        "ft": {"title": "Final Terms", "url": "https://example.test/ft.pdf",
               "sha256": "ab" * 32, "bytes": "1234"},
        "bp": {"title": "Base Prospectus", "url": "https://example.test/bp.pdf",
               "sha256": "cd" * 32, "bytes": "99"},
    },
    "bonds": [{"id": "bond", "isin": "XS0000000000", "label": "Test 1% 2030",
               "documents": [{"doc": "ft", "role": "final_terms"},
                             {"doc": "bp", "role": "base_prospectus"}]}],
}
REPORT = {"summary": {"payments": 4, "payments_ok": True, "probes": "5/5", "errors": 0,
                      "warnings": 1},
          "token": {"bond": "0x1", "cash": "0x2", "units": 1000}}


def _fields(**values):
    return {name: {"value": value} for name, value in values.items()}


def test_the_record_holds_documents_reading_decisions_terms_and_replay(tmp_path):
    review = {"run": "agent-spark", "bond_id": "bond", "issued": None, "fields": {
        "currency": {"status": "approved", "at": "2026-10-28T10:00:00+00:00"},
        "interest_rate": {"status": "corrected", "value": "1.000", "original": "1.100",
                          "at": "2026-10-28T10:01:00+00:00"}}}
    extraction = {"model": "muse-spark", "mode": "agent", "seconds": 12.5, "cost_usd": 0.01,
                  "tool_log": ["not copied"]}
    record = issuance_record("agent-spark", "bond", _fields(currency="GBP", interest_rate="1.000"),
                             extraction, review, REPORT, MANIFEST, now="2026-10-28T10:02:00+00:00")
    assert record["bond"] == {"id": "bond", "label": "Test 1% 2030", "isin": "XS0000000000",
                              "cusip": None}
    assert [d["doc"] for d in record["documents"]] == ["ft", "bp"]
    assert record["documents"][0]["sha256"] == "ab" * 32
    assert record["reading"] == {"run": "agent-spark", "model": "muse-spark", "mode": "agent",
                                 "seconds": 12.5, "cost_usd": 0.01}
    assert record["terms"]["currency"] == "GBP" and record["terms"]["issuer"] is None
    assert len(record["terms"]) == len(FIELDS)
    assert record["review"]["interest_rate"] == {
        "status": "corrected", "at": "2026-10-28T10:01:00+00:00", "read": "1.100",
        "corrected_to": "1.000"}
    assert record["review"]["issuer"] == {"status": None, "at": None}
    assert record["replay"] == REPORT
    assert record["sha256"] == digest(record)

    # The hash covers everything but itself, so a changed term no longer matches.
    changed = dict(record, terms=dict(record["terms"], currency="USD"))
    assert digest(changed) != record["sha256"]

    settings = Settings(tmp_path, None, "", "", "", "", None, None)
    path = save_record(settings, record)
    assert path == record_path(settings, "agent-spark", "bond")
    assert json.loads(path.read_text(encoding="utf-8")) == record
    assert load_record(settings, "agent-spark", "bond") == record
    assert load_record(settings, "agent-spark", "other") is None


def test_the_verified_terms_have_no_model_reading():
    review = {"run": "groundtruth", "bond_id": "bond", "issued": None, "fields": {}}
    record = issuance_record("groundtruth", "bond", _fields(currency="GBP"), None, review,
                             REPORT, MANIFEST)
    assert record["reading"] == {"run": "groundtruth", "source": "verified terms"}
    assert record["issued_at"].endswith("+00:00")
