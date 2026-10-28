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
