"""Inject known errors and measure how many the checks catch.

Two families:

* term errors: realistic slips in extracted terms (a digit in the rate, a swapped day count,
  a shifted date) applied to verified terms. Each is checked by three layers: the value is
  not written on its cited page, a consistency check fails, or the schedule cannot be built.
  Errors that change no cash flow are counted separately as without economic effect.
* contract mutants: small edits to BondToken.sol (a day count rule, a rounding branch, the
  record date lookup). Each mutant is compiled and every bond is replayed on chain; a mutant
  is caught when a payment differs from the engine and QuantLib or a probe fails.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from tstt.checks import check_terms
from tstt.config import Settings
from tstt.docs import load_document
from tstt.evidence import value_in_text
from tstt.schedule import add_months, build_schedule
from tstt.terms import TermsError, build_terms
from tstt.token import CONTRACTS_DIR, TokenError, build_contracts, forge_binary

# ---------------------------------------------------------------- term errors

Values = dict[str, Any]


def _shift_rate(v: Values) -> Any:
    return str(Decimal(v["interest_rate"]) + Decimal("0.1"))


def _swap_rate_digits(v: Values) -> Any:
    text = str(v["interest_rate"])
    digits = [i for i, ch in enumerate(text) if ch.isdigit()]
    if len(digits) < 2:
        return None
    i, j = digits[-2], digits[-1]
    swapped = text[:i] + text[j] + text[i + 1:j] + text[i] + text[j + 1:]
    return swapped if swapped != text else None


DAY_COUNT_SWAPS = {"ACT_ACT_ICMA": "ACT_365_FIXED", "THIRTY_360": "THIRTY_E_360",
                   "THIRTY_E_360": "THIRTY_360", "ACT_365_FIXED": "ACT_360",
                   "ACT_360": "ACT_365_FIXED"}
BDC_SWAPS = {"FOLLOWING": "MODIFIED_FOLLOWING", "MODIFIED_FOLLOWING": "FOLLOWING",
             "NONE": "FOLLOWING", "PRECEDING": "FOLLOWING"}
ROUNDING_SWAPS = {"HALF_UP": "DOWN", "DOWN": "HALF_UP", "HALF_EVEN": "HALF_UP",
                  "NOT_STATED": "DOWN", "UP": "HALF_UP"}
CURRENCY_SWAPS = {"USD": "EUR", "EUR": "USD", "GBP": "EUR", "AUD": "NZD", "CHF": "EUR",
                  "SEK": "NOK", "CAD": "USD", "HKD": "USD", "CNY": "HKD"}


def _months(v: Values) -> int:
    return 12 // int(v["coupon_frequency"])


def _iso(day: date) -> str:
    return day.isoformat()


@dataclass(frozen=True)
class TermError:
    name: str
    field: str
    description: str
    apply: Callable[[Values], Any]


TERM_ERRORS: tuple[TermError, ...] = (
    TermError("rate_plus_0.1", "interest_rate", "rate read 0.1 point too high", _shift_rate),
    TermError("rate_digits_swapped", "interest_rate", "last two digits of the rate swapped",
              _swap_rate_digits),
    TermError("day_count_swapped", "day_count_fraction", "a neighbouring day count",
              lambda v: DAY_COUNT_SWAPS.get(v["day_count_fraction"])),
    TermError("frequency_wrong", "coupon_frequency", "annual read as semi-annual or back",
              lambda v: {1: 2, 2: 1, 4: 2, 12: 4}.get(int(v["coupon_frequency"]))),
    TermError("maturity_year", "maturity_date", "maturity one year late",
              lambda v: _iso(add_months(date.fromisoformat(v["maturity_date"]), 12))),
    TermError("maturity_day", "maturity_date", "maturity one day late",
              lambda v: _iso(date.fromisoformat(v["maturity_date"]) + timedelta(days=1))),
    TermError("first_payment_late", "first_interest_payment_date",
              "first interest payment one period late",
              lambda v: _iso(add_months(date.fromisoformat(v["first_interest_payment_date"]),
                                        _months(v)))),
    TermError("accrual_start_early", "interest_commencement_date",
              "interest commencement date one month early",
              lambda v: _iso(add_months(date.fromisoformat(v["interest_commencement_date"]),
                                        -1))),
    TermError("denomination_x10", "specified_denomination", "denomination ten times larger",
              lambda v: str(Decimal(v["specified_denomination"]) * 10)),
    TermError("aggregate_x10", "aggregate_nominal_amount", "aggregate amount ten times larger",
              lambda v: str(Decimal(v["aggregate_nominal_amount"]) * 10)),
    TermError("redemption_101", "final_redemption_amount", "redemption at 101 per cent",
              lambda v: "101" if Decimal(v["final_redemption_amount"]) == 100 else "100"),
    TermError("business_day_convention_swapped", "business_day_convention",
              "following and modified following confused",
              lambda v: BDC_SWAPS.get(v["business_day_convention"])),
    TermError("business_day_convention_preceding", "business_day_convention",
              "payment moved to the preceding business day",
              lambda v: None if v["business_day_convention"] == "PRECEDING" else "PRECEDING"),
    TermError("rounding_swapped", "rounding_rule", "rounding rule confused",
              lambda v: ROUNDING_SWAPS.get(v.get("rounding_rule") or "NOT_STATED")),
    TermError("currency_swapped", "currency", "wrong currency",
              lambda v: CURRENCY_SWAPS.get(v["currency"])),
    TermError("calendar_dropped", "business_day", "one financial centre left out",
              lambda v: v["business_day"][1:] if len(v.get("business_day") or []) > 1
              else None),
)


@dataclass
class TermCase:
    bond_id: str
    error: str
    field: str
    original: Any
    mutated: Any
    economic_effect: bool
    on_page: bool | None  # True if the mutated value is still written on the cited page
    new_errors: list[str]
    build_failed: str | None

    @property
    def caught_by(self) -> list[str]:
        layers = []
        if self.on_page is False:
            layers.append("value not on cited page")
        if self.new_errors:
            layers.append("consistency check")
        if self.build_failed:
            layers.append("schedule rejected")
        return layers


def _flows(values: Values) -> list[tuple[str, str]] | str:
    try:
        schedule = build_schedule(build_terms(values))
    except (TermsError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    flows = [(str(p.payment_date), str(p.amount_per_basis)) for p in schedule.periods]
    return flows + [(str(schedule.redemption_date), str(schedule.redemption_per_basis)),
                    ("currency", values["currency"]), ("basis", str(schedule.basis)),
                    ("units", str(Decimal(values["aggregate_nominal_amount"])
                                  / Decimal(values["specified_denomination"])))]


def _error_codes(values: Values) -> list[str]:
    terms = build_terms(values)
    return sorted({f.code for f in check_terms(terms, build_schedule(terms))
                   if f.severity == "error"})


def _cited_texts(settings: Settings, entry: dict[str, Any]) -> list[str]:
    texts = []
    for ref in (entry, entry.get("definition") or {}):
        if ref.get("doc") and ref.get("page"):
            page = load_document(settings, ref["doc"]).page(int(ref["page"]))
            if page:
                texts.append(page.text)
    return texts


def run_term_errors(settings: Settings, ground_truth: list[Path]) -> list[TermCase]:
    cases: list[TermCase] = []
    for path in ground_truth:
        data = json.loads(path.read_text(encoding="utf-8"))
        values = {name: f.get("value") for name, f in data["fields"].items()}
        baseline_flows = _flows(values)
        baseline_errors = _error_codes(values)
        for error in TERM_ERRORS:
            if values.get(error.field) in (None, [], ""):
                continue
            mutated_value = error.apply(values)
            if mutated_value is None or mutated_value == values[error.field]:
                continue
            mutated = dict(values) | {error.field: mutated_value}
            flows = _flows(mutated)
            built = None if not isinstance(flows, str) else flows
            new_errors: list[str] = []
            if built is None:
                new_errors = [c for c in _error_codes(mutated) if c not in baseline_errors]
            texts = _cited_texts(settings, data["fields"][error.field])
            original_on_page = value_in_text(error.field, values[error.field], texts, values)
            # The page check only counts where it confirms the true value.
            on_page = (value_in_text(error.field, mutated_value, texts, mutated)
                       if original_on_page else None)
            cases.append(TermCase(
                bond_id=data["bond_id"], error=error.name, field=error.field,
                original=values[error.field], mutated=mutated_value,
                economic_effect=built is not None or flows != baseline_flows,
                on_page=on_page, new_errors=new_errors, build_failed=built,
            ))
    return cases


def summarise_terms(cases: list[TermCase]) -> list[dict[str, Any]]:
    rows = []
    for error in TERM_ERRORS:
        mine = [c for c in cases if c.error == error.name]
        effective = [c for c in mine if c.economic_effect]
        rows.append({
            "error": error.name, "description": error.description, "cases": len(mine),
            "with_effect": len(effective),
            "caught": sum(bool(c.caught_by) for c in effective),
            "by_page": sum(c.on_page is False for c in effective),
            "by_checks": sum(bool(c.new_errors) for c in effective),
            "by_schedule": sum(bool(c.build_failed) for c in effective),
            "missed": [c.bond_id for c in effective if not c.caught_by],
        })
    return rows


# ---------------------------------------------------------------- contract mutants


@dataclass(frozen=True)
class Mutant:
    name: str
    description: str
    old: str
    new: str


MUTANTS: tuple[Mutant, ...] = (
    Mutant("thirty360_d1_rule", "30/360 no longer cuts a 31st start date to the 30th",
           "        if (d1 == 31) d1 = 30;\n", ""),
    Mutant("thirty360_as_european", "30/360 cuts every 31st end date, as 30E/360 does",
           "} else if (d2 == 31 && d1 == 30) {", "} else if (d2 == 31) {"),
    Mutant("icma_fixed_365", "Actual/Actual (ICMA) divides by 365 instead of the period",
           "uint256(terms.frequency) * ((s.refEnd - s.refStart) / DAY)",
           "uint256(terms.frequency) * 365"),
    Mutant("icma_no_frequency", "Actual/Actual (ICMA) forgets the number of periods a year",
           "uint256(terms.frequency) * ((s.refEnd - s.refStart) / DAY)",
           "((s.refEnd - s.refStart) / DAY)"),
    Mutant("act365_as_360", "Actual/365 (Fixed) divides by 360",
           "if (dc == DayCount.Act365Fixed) return (actualDays, 365);",
           "if (dc == DayCount.Act365Fixed) return (actualDays, 360);"),
    Mutant("segments_not_summed", "a long first coupon pays only its last segment",
           "num = num * d + n * den;\n            den = den * d;",
           "num = n;\n            den = d;"),
    Mutant("half_up_rounds_half_down", "half up rounds an exact half down",
           "if (2 * r >= den) q += 1;", "if (2 * r > den) q += 1;"),
    Mutant("half_even_as_half_up", "half even rounds every half up",
           "if (2 * r > den || (2 * r == den && q % 2 == 1)) q += 1;",
           "if (2 * r >= den) q += 1;"),
    Mutant("down_as_up", "round down rounds up",
           "} else if (rule == Rounding.Up) {", "} else {"),
    Mutant("record_date_ignored", "coupons go to current holders, not holders of record",
           "uint256 units = balanceOfRecord(holder, p.recordDate);",
           "uint256 units = balanceOf(holder);"),
    Mutant("record_date_start_of_day", "holders of record taken at the start of the record date",
           "return balanceAt(holder, day + uint64(DAY) - 1);",
           "return balanceAt(holder, day - 1);"),
    Mutant("pays_a_day_early", "a coupon can be paid a day before its date",
           "if (block.timestamp < p.date) revert TooEarly();",
           "if (block.timestamp + 1 days < p.date) revert TooEarly();"),
    Mutant("double_payment", "the same coupon can be paid twice",
           "if (p.paid) revert AlreadyPaid();", ""),
    Mutant("anyone_can_pay", "anyone can trigger a coupon payment",
           "if (msg.sender != terms.payingAgent) revert NotPayingAgent();\n"
           "        Payment storage p = payments[i];",
           "Payment storage p = payments[i];"),
    Mutant("redeem_with_coupon_unpaid", "redemption allowed with a coupon unpaid",
           "if (!payments[i].paid) revert CouponsOutstanding();", ""),
    Mutant("redemption_at_par_always", "redemption ignores the redemption percentage",
           "return terms.denomination * terms.redemptionPercent / (100 * PERCENT_SCALE);",
           "return terms.denomination;"),
    Mutant("seller_keeps_units", "a transfer credits the buyer without debiting the seller",
           "        _write(msg.sender, balance - units);\n", ""),
)


@dataclass
class MutantResult:
    name: str
    description: str
    caught: bool
    bonds_caught: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    failed_tests: list[str] = field(default_factory=list)
    build_error: str | None = None

    @property
    def caught_by(self) -> list[str]:
        layers = []
        if self.bonds_caught:
            layers.append("bond replay")
        if self.failed_tests:
            layers.append("unit tests")
        return layers


def _mutant_tree(settings: Settings, mutant: Mutant) -> Path:
    root = settings.data_dir / "mutants" / mutant.name
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(CONTRACTS_DIR, root, ignore=shutil.ignore_patterns("out", "cache"))
    source = root / "src" / "BondToken.sol"
    text = source.read_text(encoding="utf-8")
    if text.count(mutant.old) != 1:
        raise ValueError(f"mutant {mutant.name}: pattern found {text.count(mutant.old)} times")
    source.write_text(text.replace(mutant.old, mutant.new), encoding="utf-8")
    return root


def _failing_tests(root: Path) -> list[str]:
    """Names of the contract unit tests that fail on this tree."""
    result = subprocess.run([forge_binary("forge"), "test", "--root", str(root), "--json"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", check=False)
    try:
        suites = json.loads(result.stdout[result.stdout.index("{"):])
    except ValueError:
        return ["forge test did not report results"]
    return sorted(name.split("(")[0] for suite in suites.values()
                  for name, test in suite["test_results"].items()
                  if test["status"] != "Success")


def _replay_bond(bond_id: str, values: Values, out: Path) -> str | None:
    """Why this bond exposes the contract, or None if every check passed."""
    from tstt.verify import verify

    try:
        report = verify(bond_id, build_terms(values), "mutant", out=out)
    except (TokenError, ValueError) as exc:
        return f"replay failed: {str(exc).splitlines()[0][:160]}"
    problems = [p for r in report.rows for p in r.problems]
    failed = [p["name"] for p in report.probes if not p["passed"]]
    if problems or failed:
        return "; ".join((problems + [f"probe failed: {n}" for n in failed])[:3])
    return None


def run_mutants(settings: Settings, ground_truth: list[Path], workers: int = 6,
                only: set[str] | None = None) -> list[MutantResult]:
    bonds = {}
    for path in ground_truth:
        data = json.loads(path.read_text(encoding="utf-8"))
        bonds[data["bond_id"]] = {n: f.get("value") for n, f in data["fields"].items()}
    results = []
    for mutant in MUTANTS:
        if only and mutant.name not in only:
            continue
        try:
            out = build_contracts(_mutant_tree(settings, mutant))
        except (TokenError, ValueError) as exc:
            results.append(MutantResult(mutant.name, mutant.description, False,
                                        build_error=str(exc)[:300]))
            continue
        with ThreadPoolExecutor(workers) as pool:
            reasons = dict(zip(bonds, pool.map(_replay_bond, bonds, bonds.values(),
                                               [out] * len(bonds)), strict=True))
        caught = {b: r for b, r in reasons.items() if r}
        failed_tests = _failing_tests(out.parent)
        results.append(MutantResult(mutant.name, mutant.description,
                                    bool(caught) or bool(failed_tests), sorted(caught), caught,
                                    failed_tests))
    return results


def save(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, default=lambda o: asdict(o)
                               if hasattr(o, "__dataclass_fields__") else str(o)),
                    encoding="utf-8")
    return path
