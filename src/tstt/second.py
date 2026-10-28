"""A second reading of the same documents.

Two readers make different mistakes. Where another extraction of the bond, by a different model
or in a different mode, gives a different value, the reviewer sees the disagreement beside the
flags of the consistency checks; where the readings agree and the checks are clean, approval is
quick. Differences of form only (0.75 and 0.750, T2 and TARGET) do not count, as in the scoring.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tstt.config import Settings
from tstt.eval import normalise
from tstt.extract import Extraction
from tstt.terms import FIELDS


def _same(name: str, a: Any, b: Any) -> bool:
    try:
        return normalise(name, a) == normalise(name, b)
    except (ValueError, TypeError, KeyError):
        return a == b


def differing(values: dict[str, Any], other: dict[str, Any]) -> list[str]:
    """Fields where the second reading gives a different value."""
    return [s.name for s in FIELDS if not _same(s.name, values.get(s.name), other.get(s.name))]


_loaded: dict[Path, tuple[tuple[int, int], Extraction]] = {}


def _load(file: Path) -> Extraction:
    """An extraction file holds the tool log too, so a reading once loaded is kept
    until the file changes: the review screen asks for the readings on every refresh."""
    stat = file.stat()
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _loaded.get(file)
    if cached is None or cached[0] != stamp:
        cached = (stamp, Extraction.load(file))
        _loaded[file] = cached
    return cached[1]


def readings(settings: Settings, bond_id: str) -> dict[str, Extraction]:
    """Saved readings of the bond that ended without an error, by run."""
    folder = settings.runs_dir / "extractions"
    out = {}
    for path in sorted(folder.iterdir()) if folder.exists() else []:
        file = path / f"{bond_id}.json"
        if path.is_dir() and file.exists():
            extraction = _load(file)
            if not extraction.error:
                out[path.name] = extraction
    return out


def pick_second(choices: dict[str, Extraction], model: str | None) -> str | None:
    """Another model's reading if there is one, otherwise another mode of the same model."""
    different = [run for run, e in choices.items() if e.model != model]
    return (different or list(choices) or [None])[0]


def reading_label(extraction: Extraction) -> str:
    return f"{extraction.model}, {extraction.mode.replace('_', '-')}"


# ---------------------------------------------------------------- measuring two runs


def _wrong(values: dict[str, Any], truth: dict[str, Any]) -> set[str]:
    return {n for n, expected in truth.items() if not _same(n, values.get(n), expected)}


def _flagged_by_checks(settings: Settings, run: str) -> tuple[dict[str, set[str]], float | None]:
    """Wrong fields that the consistency checks flagged, per bond, from the run's last eval,
    and when that eval was made."""
    path = settings.runs_dir / "eval" / f"{run}.json"
    if not path.exists():
        return {}, None
    scored = json.loads(path.read_text(encoding="utf-8"))
    return ({b["bond_id"]: set(b.get("wrong_flagged", [])) for b in scored["bonds"]},
            path.stat().st_mtime)


def compare_runs(settings: Settings, run_a: str, run_b: str, ground_truth: Path,
                 bonds: list[str] | None = None) -> dict[str, Any]:
    """Where two runs disagree, and which wrong values the disagreement would have caught.

    A wrong value counts as caught by the second reading when the other run gives a different
    value, and as caught by the checks when the run's eval lists the field as flagged. The
    eval counts only while it is newer than the readings it scored and covers every bond
    compared; `checks_missing` names the runs to score again.
    """
    folder = settings.runs_dir / "extractions"
    for run in (run_a, run_b):
        if not (folder / run).is_dir():
            raise FileNotFoundError(f"no run folder {run}")
    if bonds:
        unknown = [b for b in bonds if not (folder / run_a / f"{b}.json").exists()
                   and not (folder / run_b / f"{b}.json").exists()]
        if unknown:
            raise FileNotFoundError(f"no reading of {', '.join(unknown)} in either run")
    pairs = []
    for path in sorted((folder / run_a).glob("*.json")):
        other = folder / run_b / path.name
        if (not bonds or path.stem in bonds) and other.exists():
            a, b = Extraction.load(path), Extraction.load(other)
            if not (a.error or b.error):
                pairs.append((path.stem, a, b, {run_a: path, run_b: other}))
    # An eval counts only while it is newer than every reading compared and covers each of
    # them; a stale eval contributes no flags at all.
    flagged, checks_missing = {}, []
    for run in (run_a, run_b):
        found, scored_at = _flagged_by_checks(settings, run)
        fresh = scored_at is not None and all(
            bond_id in found and files[run].stat().st_mtime <= scored_at
            for bond_id, _, _, files in pairs)
        flagged[run] = found if fresh else {}
        if not fresh:
            checks_missing.append(run)
    rows = []
    for bond_id, a, b, _ in pairs:
        values_a, values_b = a.values(), b.values()
        differ = differing(values_a, values_b)
        row: dict[str, Any] = {"bond_id": bond_id, "fields": len(FIELDS), "differing": differ}
        truth_path = ground_truth / f"{bond_id}.json"
        if truth_path.exists():
            truth = json.loads(truth_path.read_text(encoding="utf-8"))
            true_values = {n: f.get("value") for n, f in truth["fields"].items()}
            for side, values, run in (("a", values_a, run_a), ("b", values_b, run_b)):
                wrong = sorted(_wrong(values, true_values))
                row[f"wrong_{side}"] = {
                    name: {"differs": name in differ,
                           "flagged": name in flagged[run].get(bond_id, set())}
                    for name in wrong}
        rows.append(row)

    wrong = [w for r in rows for side in ("a", "b") for w in r.get(f"wrong_{side}", {}).values()]
    summary = {
        "runs": [run_a, run_b], "bonds": len(rows),
        "fields_compared": sum(r["fields"] for r in rows),
        "fields_differing": sum(len(r["differing"]) for r in rows),
        "wrong_values": len(wrong),
        "wrong_differing": sum(w["differs"] for w in wrong),
        "wrong_flagged_by_checks": sum(w["flagged"] for w in wrong),
        "wrong_caught_either": sum(w["differs"] or w["flagged"] for w in wrong),
        "checks_from_eval": not checks_missing, "checks_missing": checks_missing,
    }
    return {"summary": summary, "bonds": rows}


def save_comparison(settings: Settings, result: dict[str, Any]) -> Path:
    run_a, run_b = result["summary"]["runs"]
    path = settings.runs_dir / "eval" / f"second-{run_a}-vs-{run_b}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    return path
