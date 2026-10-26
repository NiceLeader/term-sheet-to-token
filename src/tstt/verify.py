"""One report per bond: every payment from the documents to the chain, checked three ways.

For each coupon and the redemption the report holds the amount from the schedule engine,
the independent QuantLib amount, the amount the token computed and paid on chain, and the
amount printed in the documents where there is one, next to the clauses it follows from.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from tstt.checks import Flag, check_terms
from tstt.cite import FieldCitations
from tstt.reference import reference_flows
from tstt.schedule import Schedule, build_schedule, minor_unit, round_amount
from tstt.terms import BondTerms
from tstt.token import Replay, replay

CLAUSE_FIELDS = {
    "coupon": ("interest_rate", "day_count_fraction", "business_day_convention",
               "business_day", "rounding_rule", "record_date"),
    "redemption": ("final_redemption_amount", "maturity_date", "business_day_convention",
                   "record_date"),
}


@dataclass
class PaymentRow:
    index: int
    kind: str
    scheduled: str
    executed: str
    record_date: str
    accrual: str | None
    per: str
    engine: str
    quantlib: str | None
    chain: str
    stated: str | None
    holders_paid: dict[str, list[int]]
    total_paid: str
    gas_used: int
    status: str  # ok | mismatch
    problems: list[str] = field(default_factory=list)


@dataclass
class Report:
    bond_id: str
    source: str
    currency: str
    terms: dict[str, Any]
    rows: list[PaymentRow]
    probes: list[dict[str, Any]]
    flags: list[Flag]
    transfers: list[dict[str, Any]]
    token: dict[str, Any]
    clauses: dict[str, list[dict[str, Any]]]
    notes: list[str]

    @property
    def payments_ok(self) -> bool:
        return all(r.status == "ok" for r in self.rows)

    @property
    def probes_ok(self) -> bool:
        return all(p["passed"] for p in self.probes)

    @property
    def errors(self) -> list[Flag]:
        return [f for f in self.flags if f.severity == "error"]

    def summary(self) -> dict[str, Any]:
        return {"bond_id": self.bond_id, "source": self.source,
                "payments": len(self.rows), "payments_ok": self.payments_ok,
                "probes": f"{sum(p['passed'] for p in self.probes)}/{len(self.probes)}",
                "errors": len(self.errors),
                "warnings": sum(f.severity == "warning" for f in self.flags)}

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self) | {"summary": self.summary()}
        path.write_text(json.dumps(data, indent=1, default=str, ensure_ascii=False),
                        encoding="utf-8")
        return path


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return str(value)


def _stated(terms: BondTerms, schedule: Schedule, index: int) -> tuple[str, str] | None:
    """Printed amount for a coupon, scaled to the calculation basis when it is exact."""
    period = schedule.periods[index]
    for stated in terms.stated_coupon_amounts:
        dated = stated.date in (period.unadjusted_payment_date, period.payment_date)
        if dated or (stated.date is None and period.stub is None):
            return str(stated.amount), str(stated.per)
    return None


def _stated_matches(terms: BondTerms, schedule: Schedule, index: int,
                    amount: str, per: str) -> bool:
    period = schedule.periods[index]
    computed = round_amount(Fraction(terms.interest_rate) / 100 * period.fraction
                            * Fraction(Decimal(per)), minor_unit(terms.currency),
                            terms.rounding_rule)
    return computed == Decimal(amount)


def _clauses(values_cited: dict[str, dict[str, Any]] | None,
             checked: dict[str, FieldCitations] | None) -> dict[str, list[dict[str, Any]]]:
    """Quotes behind each field, with their check status, for the report and the UI."""
    if not values_cited:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for name, f in values_cited.items():
        quotes = []
        for role in ("stated_in", "defined_in"):
            results = checked[name].stated if checked and role == "stated_in" else (
                checked[name].defined if checked else [])
            for i, c in enumerate(f.get(role) or []):
                status = results[i].status if i < len(results) else "unchecked"
                found = results[i].found_page if i < len(results) else None
                quotes.append({"role": role, "doc": c.get("doc"), "page": c.get("page"),
                               "quote": c.get("quote"), "status": status,
                               "found_page": found})
        out[name] = quotes
    return out


def verify(bond_id: str, terms: BondTerms, source: str,
           fields: dict[str, dict[str, Any]] | None = None,
           checked: dict[str, FieldCitations] | None = None,
           out: Path | None = None) -> Report:
    schedule = build_schedule(terms)
    reference = reference_flows(terms)
    ql_coupons = [f for f in reference.flows if f.kind == "coupon"]
    ql_redemption = next(f for f in reference.flows if f.kind == "redemption")
    result: Replay = replay(schedule, out)
    unit = minor_unit(terms.currency)
    ratio = result.config.terms["denomination"] // result.config.terms["calculationAmount"]
    rows: list[PaymentRow] = []
    for executed, plan in zip(result.payments, result.config.plan, strict=True):
        problems: list[str] = []
        chain_amount = Decimal(executed.per_basis_minor) * unit
        engine_amount = Decimal(plan.per_basis_minor) * unit
        if executed.kind == "coupon":
            period = schedule.periods[executed.index]
            ql = ql_coupons[executed.index] if executed.index < len(ql_coupons) else None
            ql_amount = ql.amount if ql else None
            if ql is None or ql.payment_date != period.payment_date:
                problems.append("QuantLib has a different payment date")
            accrual = f"{period.accrual_start} to {period.accrual_end}"
            per = str(schedule.basis)
            stated = _stated(terms, schedule, executed.index)
            if stated and not _stated_matches(terms, schedule, executed.index, *stated):
                problems.append(f"printed amount {stated[0]} per {stated[1]} differs")
        else:
            ql_amount = ql_redemption.amount * ratio
            if ql_redemption.payment_date != schedule.redemption_date:
                problems.append("QuantLib has a different redemption date")
            accrual, stated = None, None
            per = str(terms.specified_denomination)
        if chain_amount != engine_amount:
            problems.append(f"token computed {chain_amount}, engine {engine_amount}")
        if ql_amount is not None and ql_amount != engine_amount:
            problems.append(f"QuantLib computed {ql_amount}, engine {engine_amount}")
        if executed.executed < executed.scheduled:
            problems.append("paid before the payment date")
        per_unit = plan.per_basis_minor * (ratio if executed.kind == "coupon" else 1)
        paid_units = {holder: units for holder, (units, _) in executed.holders.items()}
        if paid_units != executed.expected_units:
            problems.append(f"paid holders {paid_units}, holders of record "
                            f"{executed.expected_units}")
        for holder, units in executed.expected_units.items():
            paid = executed.holders.get(holder, (0, 0))[1]
            if paid != units * per_unit:
                problems.append(f"holder {holder} received {paid}, expected {units * per_unit}")
        rows.append(PaymentRow(
            index=executed.index, kind=executed.kind, scheduled=str(executed.scheduled),
            executed=str(executed.executed), record_date=str(executed.record_date),
            accrual=accrual, per=per, engine=str(engine_amount),
            quantlib=None if ql_amount is None else str(ql_amount), chain=str(chain_amount),
            stated=None if not stated else f"{stated[0]} per {stated[1]}",
            holders_paid={k: list(v) for k, v in executed.holders.items()},
            total_paid=str(Decimal(executed.total_minor) * unit), gas_used=executed.gas_used,
            status="mismatch" if problems else "ok", problems=problems,
        ))
    values = {name: f.get("value") for name, f in (fields or {}).items()}
    flags = check_terms(terms, schedule, checked, values)
    token = {"bond": result.bond_address, "cash": result.cash_address,
             "deploy_gas": result.deploy_gas, "units": result.config.total_units,
             "allocation": result.allocation, "final_units": result.final_units,
             "config": result.config.terms}
    notes = schedule.warnings + reference.notes + result.config.notes
    return Report(
        bond_id=bond_id, source=source, currency=terms.currency,
        terms={k: _plain(v) for k, v in asdict(terms).items()},
        rows=rows,
        probes=[{"name": p.name, "expected": p.expected, "actual": p.actual,
                 "passed": p.passed} for p in result.probes],
        flags=flags, transfers=result.transfers, token=token,
        clauses=_clauses(fields, checked), notes=notes,
    )


def ground_truth_fields(path: Path) -> dict[str, dict[str, Any]]:
    """Ground truth in the field shape the extractor returns, citing pages without quotes."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {name: {"value": f.get("value"), "stated_in": [], "defined_in": [],
                   "doc": f.get("doc"), "page": f.get("page")}
            for name, f in data["fields"].items()}
