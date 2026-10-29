from tstt.cli import ground_truth_bonds, main, run_folder_name
from tstt.extract import ROLE_ORDER
from tstt.fetch import load_manifest
from tstt.review import GROUND_TRUTH, RUN_NAME


def test_default_reading_leaves_out_the_held_out_bonds():
    held_out = {bond["id"] for bond in load_manifest()["bonds"] if bond.get("held_out")}
    labelled = {path.stem for path in GROUND_TRUTH.glob("*.json")}
    assert len(held_out) == 7
    assert set(ground_truth_bonds()) == labelled - held_out
    assert set(ground_truth_bonds(held_out=True)) == held_out


def test_held_out_reading_gets_a_folder_of_its_own():
    model = "muse-spark-1.3-contributor"
    assert run_folder_name("agent", model) == "agent-muse-spark-1.3-contributor"
    assert run_folder_name("agent", model, held_out=True) == \
        "agent-muse-spark-1.3-contributor-unseen"
    assert RUN_NAME.fullmatch(run_folder_name("one_pass", "muse-glimmer-30b", held_out=True))


def test_run_name_stays_inside_the_runs_folder(capsys):
    for bad in ("../elsewhere", "a/b", "a\\b", "Agent", ".x", ""):
        assert main(["extract", "--run", bad]) == 2
    assert "run name" in capsys.readouterr().err


def test_offering_memorandum_is_read_like_final_terms():
    assert ROLE_ORDER["offering_memorandum"] == ROLE_ORDER["final_terms"]


def test_shipped_readings_are_copied_into_the_data_folder(tmp_path, monkeypatch, capsys):
    source = tmp_path / "readings" / "agent-muse-spark-1.3-contributor"
    source.mkdir(parents=True)
    (source / "ibrd-gbp-0750-2026.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("TSTT_DATA_DIR", str(tmp_path / "data"))
    assert main(["import", str(tmp_path / "readings")]) == 0
    target = tmp_path / "data" / "runs" / "extractions" / source.name / "ibrd-gbp-0750-2026.json"
    assert target.read_text(encoding="utf-8") == "{}"
    (source / "ibrd-gbp-0750-2026.json").write_text("{\"a\": 1}", encoding="utf-8")
    assert main(["import", str(tmp_path / "readings")]) == 0
    assert target.read_text(encoding="utf-8") == "{}"  # a reading already there is kept
    assert main(["import", str(tmp_path / "readings"), "--force"]) == 0
    assert target.read_text(encoding="utf-8") == "{\"a\": 1}"
    assert "1 copied" in capsys.readouterr().out


def test_audit_needs_verified_terms_for_the_bond(capsys):
    assert main(["audit", "no-such-bond", "--rpc", "http://127.0.0.1:1",
                 "--address", "0x" + "1" * 40]) == 2
    assert "no verified terms" in capsys.readouterr().err


def test_import_refuses_what_the_review_screen_would_not_show(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TSTT_DATA_DIR", str(tmp_path / "data"))
    assert main(["import", str(tmp_path / "nowhere")]) == 2
    source = tmp_path / "readings" / "Test Run"
    source.mkdir(parents=True)
    (source / "ibrd-gbp-0750-2026.json").write_text("{}", encoding="utf-8")
    assert main(["import", str(tmp_path / "readings")]) == 2
    err = capsys.readouterr().err
    assert "is not a folder" in err and "'Test Run'" in err
    assert not (tmp_path / "data" / "runs").exists()
