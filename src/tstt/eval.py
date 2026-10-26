"""Score extraction runs against the verified ground truth.

Per bond: which fields match, whether the cash flows built from the extracted terms equal
the cash flows built from the ground truth (every date and amount per unit), how many quotes
are found on the page they cite, the review flags raised, and cost and time. A field can
differ in form and not in effect (an empty list for null), so values are normalised first.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tstt.calendars import normalise_centre
from tstt.checks import check_terms
from tstt.cite import Citations, check_fields
from tstt.config import Settings
from tstt.extract import Extraction, bond_source
from tstt.schedule import Schedule, build_schedule
from tstt.terms import FIELD_BY_NAME, TermsError, build_terms
from tstt.token import record_rule

EMPTY = (None, "", [], "NOT_STATED")


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def normalise(name: str, value: Any) -> Any:
    """A comparable form of a field value."""
    if value in EMPTY:
        return None
    spec = FIELD_BY_NAME.get(name)
    kind = spec.kind if spec else "text"
    if kind == "decimal":
        number = _decimal(value)
        return number.normalize() if number is not None else str(value)
    if kind == "int":
        return int(value)
    if name == "business_day":
        # T2 and TARGET, or New York City and New York, are the same calendar.
        return tuple(sorted({normalise_centre(str(v)) or str(v).strip().lower()
                             for v in value}))
    if kind == "list":
        return tuple(sorted(str(v).strip().lower() for v in value))
    if kind == "coupons":
        return tuple(sorted(((c.get("date") or ""), str(_decimal(c.get("amount"))),
                             str(_decimal(c.get("per")))) for c in value))
    if name == "record_date":
        rule = record_rule(str(value))
        return rule.kind, rule.days, rule.fixed
    if kind == "text":
        return re.sub(r"\W+", " ", str(value)).strip().lower()
    return value


def _flows(schedule: Schedule) -> list[tuple[str, str, Decimal]]:
    """Every payment per unit of the smallest denomination."""
    terms = schedule.terms
    ratio = terms.specified_denomination / schedule.basis
    flows = [("coupon", str(p.payment_date), (p.amount_per_basis * ratio).normalize())
             for p in schedule.periods]
    flows.append(("redemption", str(schedule.redemption_date),
                  (schedule.redemption_per_basis * ratio).normalize()))
    return flows


@dataclass
class BondScore:
    bond_id: str
    fields_total: int
    fields_correct: int
    wrong: dict[str, dict[str, Any]]
    flows_match: bool | None  # None if no schedule could be built from the extraction
    flows_detail: str
    payments: int
    payments_matching: int
    quotes: int
    quotes_on_page: int
    errors: int
    warnings: int
    wrong_flagged: list[str]  # wrong fields that a review flag points at
    seconds: float
    steps: int
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost_usd: float | None
    error: str | None = None


@dataclass
class RunScore:
    run: str
    bonds: list[BondScore] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        done = [b for b in self.bonds if b.error is None]
        total = sum(b.fields_total for b in done)
        wrong = sum(len(b.wrong) for b in done)
        cost = [b.cost_usd for b in done if b.cost_usd is not None]
        return {
            "run": self.run, "bonds": len(self.bonds), "submitted": len(done),
            "field_accuracy": round(1 - wrong / total, 4) if total else None,
            "bonds_all_fields_correct": sum(not b.wrong for b in done),
            "bonds_cash_flows_match": sum(bool(b.flows_match) for b in done),
            "payments_matching": f"{sum(b.payments_matching for b in done)}/"
                                 f"{sum(b.payments for b in done)}",
            "quotes_on_page": f"{sum(b.quotes_on_page for b in done)}/"
                              f"{sum(b.quotes for b in done)}",
            "wrong_fields_flagged": f"{sum(len(b.wrong_flagged) for b in done)}/{wrong}",
            "cost_usd": round(sum(cost), 4) if cost else None,
            "seconds_per_bond": round(sum(b.seconds for b in done) / len(done), 1)
            if done else None,
        }

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"summary": self.summary(),
                                    "bonds": [asdict(b) for b in self.bonds]},
                                   indent=1, default=str, ensure_ascii=False),
                        encoding="utf-8")
        return path


def score_bond(settings: Settings, extraction: Extraction, truth_path: Path) -> BondScore:
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    true_values = {n: f.get("value") for n, f in truth["fields"].items()}
    values = extraction.values()
    wrong = {}
    for name, expected in true_values.items():
        got = values.get(name)
        if normalise(name, got) != normalise(name, expected):
            wrong[name] = {"expected": expected, "extracted": got}

    documents = [doc for doc, _ in bond_source(settings, extraction.bond_id).documents]
    checked = check_fields(extraction.fields, Citations(documents))
    quotes = [c for f in checked.values() for c in f.stated + f.defined]

    flows_match, detail, payments, matching = None, "", 0, 0
    flagged: set[str] = set()
    errors = warnings = 0
    expected_flows = _flows(build_schedule(build_terms(true_values)))
    try:
        terms = build_terms(values)
        schedule = build_schedule(terms)
    except (TermsError, ValueError, TypeError) as exc:
        detail = f"no schedule: {exc}"
        payments = len(expected_flows)
    else:
        got_flows = _flows(schedule)
        payments = max(len(expected_flows), len(got_flows))
        matching = sum(a == b for a, b in zip(expected_flows, got_flows, strict=False))
        same_units = (normalise("currency", values.get("currency"))
                      == normalise("currency", true_values.get("currency"))
                      and _decimal(values.get("aggregate_nominal_amount"))
                      == _decimal(true_values.get("aggregate_nominal_amount")))
        flows_match = got_flows == expected_flows and same_units
        if not flows_match:
            first = next((f"{a} vs {b}" for a, b in zip(expected_flows, got_flows,
                                                        strict=False) if a != b),
                         f"{len(got_flows)} payments, expected {len(expected_flows)}"
                         if len(got_flows) != len(expected_flows)
                         else "currency or amount issued differs")
            detail = first
        flags = check_terms(terms, schedule, checked, values)
        errors = sum(f.severity == "error" for f in flags)
        warnings = sum(f.severity == "warning" for f in flags)
        flagged = {name for f in flags if f.severity != "info" for name in f.fields}

    usage = extraction.usage
    return BondScore(
        bond_id=extraction.bond_id, fields_total=len(true_values),
        fields_correct=len(true_values) - len(wrong), wrong=wrong,
        flows_match=flows_match, flows_detail=detail, payments=payments,
        payments_matching=matching, quotes=len(quotes),
        quotes_on_page=sum(c.valid for c in quotes), errors=errors, warnings=warnings,
        wrong_flagged=sorted(set(wrong) & flagged), seconds=round(extraction.seconds, 1),
        steps=extraction.steps, input_tokens=usage.input_tokens,
        cached_tokens=usage.cached_tokens, output_tokens=usage.output_tokens,
        cost_usd=extraction.cost_usd,
    )


def score_run(settings: Settings, folder: Path, ground_truth: Path) -> RunScore:
    run = RunScore(folder.name)
    for path in sorted(folder.glob("*.json")):
        truth = ground_truth / path.name
        if not truth.exists():
            continue
        extraction = Extraction.load(path)
        if extraction.error:
            run.bonds.append(BondScore(
                extraction.bond_id, 0, 0, {}, None, "", 0, 0, 0, 0, 0, 0, [],
                round(extraction.seconds, 1), extraction.steps, extraction.usage.input_tokens,
                extraction.usage.cached_tokens, extraction.usage.output_tokens,
                extraction.cost_usd, error=extraction.error))
            continue
        run.bonds.append(score_bond(settings, extraction, truth))
    return run
