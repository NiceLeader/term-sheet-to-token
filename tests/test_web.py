import shutil
import threading

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from tstt import review, web  # noqa: E402
from tstt.config import Settings, load_settings  # noqa: E402
from tstt.web import create_app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    try:
        settings = load_settings()
    except RuntimeError:
        pytest.skip("TSTT_DATA_DIR is not set")
    if not (settings.parsed_dir / "ibrd-xs2113033281-ft.json").exists():
        pytest.skip("bond documents are not fetched")
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8000")


def test_bond_view_from_ground_truth(client):
    data = client.get("/api/bond/groundtruth/ibrd-gbp-0750-2026").json()
    rate = next(f for f in data["fields"] if f["name"] == "interest_rate")
    assert rate["value"] == "0.750"
    assert rate["cites"][0]["doc"] == "ibrd-xs2113033281-ft"
    assert [d["role"] for d in data["documents"]] == ["final_terms", "base_prospectus"]


def test_quote_is_located_on_the_page(client):
    view = client.get("/api/view/ibrd-xs2113033281-ft/2",
                      params={"q": ["0.750 per cent. per annum"]}).json()
    assert view["format"] == "pdf"
    assert len(view["marks"][0]["boxes"]) == 1


def test_unknown_bond_is_404(client):
    assert client.get("/api/bond/groundtruth/no-such-bond").status_code == 404


def test_other_hosts_are_refused(client):
    assert client.get("/api/runs", headers={"host": "attacker.example"}).status_code == 403


def test_post_from_another_site_is_refused(client):
    response = client.post("/api/extract/hosted/ibrd-gbp-0750-2026",
                           headers={"origin": "https://attacker.example"})
    assert response.status_code == 403


def test_run_and_document_ids_cannot_leave_the_data_folder(client):
    assert client.get("/api/bond/..%5C..%5Cx/ibrd-gbp-0750-2026").status_code == 404
    assert client.post("/api/verify/..%5C..%5Cx/ibrd-gbp-0750-2026").status_code == 404
    assert client.get("/api/search/..%5Cparsed%5Cx", params={"q": "a"}).status_code == 404


def test_bond_view_carries_the_review_state(client):
    data = client.get("/api/bond/groundtruth/ibrd-gbp-0750-2026").json()
    assert data["review"]["total"] == 22
    rate = next(f for f in data["fields"] if f["name"] == "interest_rate")
    assert rate["kind"] == "decimal"
    convention = next(f for f in data["fields"] if f["name"] == "business_day_convention")
    assert "MODIFIED_FOLLOWING" in convention["options"]


def test_review_refuses_an_invalid_correction(client):
    response = client.post("/api/review/groundtruth/ibrd-gbp-0750-2026/day_count_fraction",
                           json={"action": "correct", "value": "ACT/ACT"})
    assert response.status_code == 422
    response = client.post("/api/review/groundtruth/ibrd-gbp-0750-2026/no_such_field",
                           json={"action": "approve"})
    assert response.status_code == 404


def test_a_bond_being_read_again_cannot_be_reviewed(tmp_path, monkeypatch):
    truth = tmp_path / "groundtruth"
    truth.mkdir()
    shutil.copy(review.GROUND_TRUTH / "ibrd-gbp-0750-2026.json", truth)
    monkeypatch.setattr(review, "GROUND_TRUTH", truth)
    started, release = threading.Event(), threading.Event()

    def reading(settings, job):
        job.run = "agent-test-model"
        started.set()
        release.wait(5)
        job.done = True

    monkeypatch.setattr(web, "_run_job", reading)
    settings = Settings(tmp_path / "data", None, "", "", "", "", None, None)
    (settings.runs_dir / "extractions" / "agent-test-model").mkdir(parents=True)
    client = TestClient(create_app(settings), base_url="http://127.0.0.1:8000")
    try:
        assert client.post("/api/extract/local/ibrd-gbp-0750-2026").status_code == 200
        assert started.wait(5)
        bond = "agent-test-model/ibrd-gbp-0750-2026"
        approve = client.post(f"/api/review/{bond}/currency", json={"action": "approve"})
        assert approve.status_code == 409
        assert client.post(f"/api/issue/{bond}").status_code == 409
        assert client.post(f"/api/verify/{bond}").status_code == 409
        # The verified terms are not replaced by a reading, so they can be reviewed meanwhile.
        approve = client.post("/api/review/groundtruth/ibrd-gbp-0750-2026/currency",
                              json={"action": "approve"})
        assert approve.status_code == 200
    finally:
        release.set()


def test_the_page_is_checked_for_changes_on_every_load(client):
    assert client.get("/app.js").headers["cache-control"] == "no-cache"
