"""Reviewer sign-off: every term is approved or corrected by a person before the token is issued.

A review of a model's extraction is kept in the data folder beside the run and leaves the
model's output as it was, so the run can still be scored. For the verified terms the reviewer
checks the ground truth itself: a correction is written into eval/groundtruth, and the file
becomes "verified" once every field is reviewed and the token replays without a red flag.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tstt.config import REPO_ROOT, Settings
from tstt.terms import FIELD_BY_NAME, FIELDS, TermsError, parse_value

GROUND_TRUTH = REPO_ROOT / "eval" / "groundtruth"
TRUTH = "groundtruth"
FIELD_START = re.compile(r'    "([a-z_]+)": ')
ONE_LINE = 120


def normalise(name: str, value: Any) -> Any:
    """A value in the ground truth format (decimals and dates as strings), or TermsError."""
    spec = FIELD_BY_NAME.get(name)
    if spec is None:
        raise TermsError(f"unknown field {name}")
    if value in (None, "") or (spec.kind == "list" and value == []):
        return None
    if spec.kind == "int" and (isinstance(value, bool)
                               or not re.fullmatch(r"\s*\d+\s*", str(value))):
        raise TermsError(f"{name}: not a whole number: {value!r}")
    if spec.kind == "list" and not isinstance(value, list):
        raise TermsError(f"{name}: expected a list")
    if spec.kind == "coupons" and not (isinstance(value, list)
                                       and all(isinstance(c, dict) for c in value)):
        raise TermsError(f"{name}: expected a list of {{date, amount, per}}")
    try:
        typed = parse_value(spec, value)
    except TermsError:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise TermsError(f"{name}: {exc}") from exc
    match spec.kind:
        case "decimal":
            return _plain(typed)
        case "date":
            return typed.isoformat()
        case "list":
            items = [v.strip() for v in typed if v.strip()]
            if name == "interest_payment_dates" and not all(
                    re.fullmatch(r"\d{2}-\d{2}", v) for v in items):
                raise TermsError(f"{name}: dates are MM-DD")
            return items or None
        case "coupons":
            return [{"date": c.date.isoformat() if c.date else None,
                     "amount": _plain(c.amount), "per": _plain(c.per)} for c in typed]
        case "text":
            return typed.strip() or None
    return typed


def _plain(value: Decimal) -> str:
    return format(value, "f")


def _same(name: str, a: Any, b: Any) -> bool:
    try:
        return normalise(name, a) == normalise(name, b)
    except TermsError:
        return False


# ---------------------------------------------------------------- ground truth file


def truth_path(bond_id: str) -> Path:
    return GROUND_TRUTH / f"{bond_id}.json"


def read_truth(bond_id: str) -> dict[str, Any]:
    return json.loads(truth_path(bond_id).read_text(encoding="utf-8"))


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _inline(d: dict[str, Any]) -> str:
    return "{" + ", ".join(f"{_dump(k)}: {_dump(v)}" for k, v in d.items()) + "}"


def format_field(name: str, f: dict[str, Any]) -> str:
    """One field of a ground truth file in the layout the files are written in by hand."""
    coupons = isinstance(f.get("value"), list) and any(isinstance(c, dict) for c in f["value"])
    line = f"    {_dump(name)}: {_inline(f)}"
    if set(f) <= {"value", "doc", "page"} and not coupons and len(line) <= ONE_LINE:
        return line
    parts = []
    for key, value in f.items():
        if key == "page" and "doc" in f:
            continue
        if key == "doc":
            parts.append(f'      "doc": {_dump(value)}'
                         + (f', "page": {_dump(f["page"])}' if "page" in f else ""))
        elif key == "value" and coupons:
            rows = ",\n".join(f"        {_inline(c)}" for c in value)
            parts.append(f'      "value": [\n{rows}\n      ]')
        elif isinstance(value, dict):
            parts.append(f"      {_dump(key)}: {_inline(value)}")
        else:
            parts.append(f"      {_dump(key)}: {_dump(value)}")
    return f"    {_dump(name)}: {{\n" + ",\n".join(parts) + "\n    }"


def _patched(text: str, data: dict[str, Any]) -> str:
    """The file text with only the changed fields and top-level values rewritten, so a
    review leaves a diff of the lines it changed."""
    old = json.loads(text)
    lines = text.split("\n")
    start = lines.index('  "fields": {')
    end = next(i for i in range(start + 1, len(lines)) if lines[i] in ("  }", "  },"))
    blocks: list[tuple[str, int, int]] = []
    for i in range(start + 1, end):
        match = FIELD_START.match(lines[i])
        if match:
            if blocks:
                blocks[-1] = (*blocks[-1][:2], i)
            blocks.append((match.group(1), i, end))
    out = lines[:start + 1]
    for name, first, stop in blocks:
        block = lines[first:stop]
        if old["fields"].get(name) != data["fields"].get(name):
            comma = "," if block[-1].endswith(",") else ""
            block = (format_field(name, data["fields"][name]) + comma).split("\n")
        out += block
    out += lines[end:]
    for key in ("status",):
        if old.get(key) != data.get(key):
            out = [f"  {_dump(key)}: {_dump(data[key])}," if line.startswith(f'  "{key}": ')
                   else line for line in out]
    return "\n".join(out)


def write_truth(bond_id: str, data: dict[str, Any]) -> None:
    path = truth_path(bond_id)
    text = path.read_bytes().decode("utf-8")
    crlf = "\r\n" in text
    text = text.replace("\r\n", "\n")
    try:
        new = _patched(text, data)
        if json.loads(new) != data:
            raise ValueError("patch did not reproduce the data")
    except (ValueError, StopIteration):
        new = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if crlf:
        new = new.replace("\n", "\r\n")
    path.write_bytes(new.encode("utf-8"))


# ---------------------------------------------------------------- review record


def review_path(settings: Settings, run: str, bond_id: str) -> Path:
    return settings.runs_dir / "reviews" / run / f"{bond_id}.json"


def reviewed_report_path(settings: Settings, run: str, bond_id: str) -> Path:
    """Report of the reviewed terms. For the ground truth the corrections are in the file, so
    its report is the ordinary one."""
    if run == TRUTH:
        return settings.runs_dir / "reports" / run / f"{bond_id}.json"
    return settings.runs_dir / "reviews" / run / f"{bond_id}.report.json"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def load_review(settings: Settings, run: str, bond_id: str) -> dict[str, Any]:
    path = review_path(settings, run, bond_id)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    review: dict[str, Any] = {"run": run, "bond_id": bond_id, "fields": {}, "issued": None}
    if run == TRUTH and truth_path(bond_id).exists() and read_truth(bond_id).get(
            "status") == "verified":
        # Verified before this review record existed: every field counts as approved.
        review["fields"] = {spec.name: {"status": "approved"} for spec in FIELDS}
        review["issued"] = {"at": None}
    return review


def save_review(settings: Settings, review: dict[str, Any]) -> None:
    path = review_path(settings, review["run"], review["bond_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(review, indent=1, ensure_ascii=False), encoding="utf-8")


def discard(settings: Settings, run: str, bond_id: str) -> None:
    """Forget the review and the reports of a model's earlier reading when the bond is read
    again."""
    if run != TRUTH:
        review_path(settings, run, bond_id).unlink(missing_ok=True)
        reviewed_report_path(settings, run, bond_id).unlink(missing_ok=True)
        (settings.runs_dir / "reports" / run / f"{bond_id}.json").unlink(missing_ok=True)


def reviewed_values(fields: dict[str, dict[str, Any]],
                    review: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The source fields with the reviewer's corrections in place of the source values."""
    out = {}
    for name, f in fields.items():
        entry = review["fields"].get(name)
        out[name] = dict(f, value=entry["value"]) if entry and entry["status"] == "corrected" \
            else f
    return out


def corrected(review: dict[str, Any]) -> bool:
    return any(e["status"] == "corrected" for e in review["fields"].values())


def _key(entry: dict[str, Any] | None) -> tuple | None:
    return None if entry is None else (entry["status"], _dump(entry.get("value")))


def unreviewed(review: dict[str, Any]) -> list[str]:
    return [spec.name for spec in FIELDS if spec.name not in review["fields"]]


def record(settings: Settings, run: str, bond_id: str, name: str, action: str,
           source_value: Any, value: Any = None) -> dict[str, Any]:
    """Approve, correct or reset one field, and return the updated review.

    `source_value` is the value now in the source (the model's, or the ground truth file's).
    Any change withdraws an earlier issuance; for the ground truth it also sets the file back
    to draft and writes the corrected value into it.
    """
    if name not in FIELD_BY_NAME:
        raise TermsError(f"unknown field {name}")
    review = load_review(settings, run, bond_id)
    before = review["fields"].get(name)
    original = before["original"] if before and before["status"] == "corrected" else source_value
    match action:
        case "approve":
            if before and before["status"] == "corrected":
                entry = before
            else:
                normalise(name, source_value)
                entry = {"status": "approved", "at": _now()}
        case "correct":
            new_value = normalise(name, value)
            if _same(name, new_value, original):
                entry = {"status": "approved", "at": _now()}
            else:
                entry = {"status": "corrected", "value": new_value, "original": original,
                         "at": _now()}
        case "reset":
            entry = None
        case _:
            raise TermsError(f"unknown action {action}")
    if _key(entry) == _key(before):
        return review
    if entry is None:
        review["fields"].pop(name, None)
    else:
        review["fields"][name] = entry
    review["issued"] = None
    if run == TRUTH:
        truth = read_truth(bond_id)
        truth["status"] = "draft"
        if name in truth["fields"]:
            truth["fields"][name]["value"] = (entry["value"] if entry
                                              and entry["status"] == "corrected" else original)
        write_truth(bond_id, truth)
    elif any(e and e["status"] == "corrected" for e in (before, entry)):
        # The reviewed terms changed, so their earlier replay no longer applies.
        reviewed_report_path(settings, run, bond_id).unlink(missing_ok=True)
    save_review(settings, review)
    return review


def mark_issued(settings: Settings, run: str, bond_id: str,
                summary: dict[str, Any]) -> dict[str, Any]:
    review = load_review(settings, run, bond_id)
    review["issued"] = {"at": _now(), "summary": summary}
    save_review(settings, review)
    if run == TRUTH:
        truth = read_truth(bond_id)
        truth["status"] = "verified"
        write_truth(bond_id, truth)
    return review
