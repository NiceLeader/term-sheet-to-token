"""The economic terms of a fixed rate bond, each tied to the text that states and defines it.

Field names follow the ground truth format in eval/README.md. The `bdt` column names the
closest element of the ICMA Bond Data Taxonomy; it is an alignment aid, not a copy of the
official schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any


class DayCount(StrEnum):
    ACT_ACT_ICMA = "ACT_ACT_ICMA"
    THIRTY_360 = "THIRTY_360"
    THIRTY_E_360 = "THIRTY_E_360"
    ACT_365_FIXED = "ACT_365_FIXED"
    ACT_360 = "ACT_360"
    RBA_BOND_BASIS = "RBA_BOND_BASIS"
    ACT_ACT_CANADIAN_COMPOUND = "ACT_ACT_CANADIAN_COMPOUND"
    OTHER = "OTHER"


class RoundingRule(StrEnum):
    HALF_UP = "HALF_UP"
    HALF_EVEN = "HALF_EVEN"
    DOWN = "DOWN"
    UP = "UP"
    NOT_STATED = "NOT_STATED"


class BusinessDayConvention(StrEnum):
    FOLLOWING = "FOLLOWING"
    MODIFIED_FOLLOWING = "MODIFIED_FOLLOWING"
    PRECEDING = "PRECEDING"
    NONE = "NONE"


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: str  # text | decimal | date | enum | int | bool | list | coupons
    description: str
    bdt: str
    enum: tuple[str, ...] = ()
    needs_definition: bool = False


FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("isin", "text", "ISIN of this bond, or null if only a CUSIP is given.",
              "InstrumentIdentifier/ISIN"),
    FieldSpec("issuer", "text", "Issuer name as written.", "Issuer/Name"),
    FieldSpec("currency", "text", "ISO 4217 code of the specified currency.",
              "SpecifiedCurrency"),
    FieldSpec("aggregate_nominal_amount", "decimal",
              "Aggregate nominal amount of this tranche as a plain decimal.",
              "TotalAggregateAmount/Amount"),
    FieldSpec("specified_denomination", "decimal", "Smallest specified denomination.",
              "MinimumDenomination/Amount"),
    FieldSpec("calculation_amount", "decimal",
              "Calculation Amount if the documents define one, else null.",
              "CalculationAmount"),
    FieldSpec("issue_date", "date", "Issue date, YYYY-MM-DD.", "IssueDate"),
    FieldSpec("interest_commencement_date", "date",
              "Date interest starts to accrue, YYYY-MM-DD. Equal to the issue date unless "
              "stated otherwise.", "InterestCommencementDate"),
    FieldSpec("maturity_date", "date", "Unadjusted maturity date, YYYY-MM-DD.", "MaturityDate"),
    FieldSpec("interest_rate", "decimal", "Fixed rate in percent per annum, e.g. 3.399.",
              "Interest/FixedRate/Rate"),
    FieldSpec("coupon_frequency", "int", "Interest payments per year: 1, 2, 4 or 12.",
              "Interest/PaymentFrequency"),
    FieldSpec("interest_payment_dates", "list",
              "Regular interest payment dates as MM-DD strings, e.g. [\"04-10\", \"10-10\"].",
              "Interest/PaymentDates"),
    FieldSpec("first_interest_payment_date", "date", "First interest payment date, YYYY-MM-DD.",
              "Interest/FirstPaymentDate"),
    FieldSpec("day_count_fraction", "enum", "Day count fraction used to compute interest.",
              "Interest/DayCountFraction", tuple(DayCount), needs_definition=True),
    FieldSpec("business_day_convention", "enum",
              "Convention that moves a payment falling on a non business day.",
              "Interest/BusinessDayConvention", tuple(BusinessDayConvention),
              needs_definition=True),
    FieldSpec("accrual_adjusted", "bool",
              "True if interest periods move with the business day convention, false if "
              "interest accrues on unadjusted dates.", "Interest/AdjustedIndicator"),
    FieldSpec("business_day", "list",
              "Financial centres whose business days count for payments, e.g. "
              "[\"London\", \"TARGET\", \"New York\"].", "BusinessCentres",
              needs_definition=True),
    FieldSpec("final_redemption_amount", "decimal",
              "Final redemption amount in percent of nominal, e.g. 100.",
              "Redemption/FinalRedemptionAmount"),
    FieldSpec("rounding_rule", "enum",
              "How a coupon amount is rounded to the currency sub-unit.",
              "Interest/RoundingConvention", tuple(RoundingRule), needs_definition=True),
    FieldSpec("stated_coupon_amounts", "coupons",
              "Coupon amounts printed in the documents: fixed coupon amounts (date null) "
              "and broken amounts for a short or long first or last period.",
              "Interest/FixedCouponAmount, BrokenAmount"),
    FieldSpec("record_date", "text", "Record date or record time rule as a short phrase, or null.",
              "RecordDate"),
)

FIELD_BY_NAME = {spec.name: spec for spec in FIELDS}


@dataclass(frozen=True)
class Citation:
    doc: str
    page: int
    quote: str


@dataclass
class TermField:
    name: str
    value: Any
    stated_in: list[Citation] = field(default_factory=list)
    defined_in: list[Citation] = field(default_factory=list)
    note: str | None = None


@dataclass(frozen=True)
class StatedCoupon:
    date: date | None
    amount: Decimal
    per: Decimal


@dataclass
class BondTerms:
    """Typed terms used by the schedule engine, the token config and QuantLib."""

    isin: str | None
    issuer: str | None
    currency: str
    aggregate_nominal_amount: Decimal
    specified_denomination: Decimal
    calculation_amount: Decimal | None
    issue_date: date
    interest_commencement_date: date
    maturity_date: date
    interest_rate: Decimal
    coupon_frequency: int
    interest_payment_dates: list[str]
    first_interest_payment_date: date
    day_count_fraction: DayCount
    business_day_convention: BusinessDayConvention
    accrual_adjusted: bool
    business_day: list[str]
    final_redemption_amount: Decimal
    rounding_rule: RoundingRule = RoundingRule.NOT_STATED
    stated_coupon_amounts: list[StatedCoupon] = field(default_factory=list)
    record_date: str | None = None

    @property
    def coupon_basis(self) -> Decimal:
        """Amount on which one coupon is computed and rounded."""
        return self.calculation_amount or self.specified_denomination


class TermsError(ValueError):
    pass


def _decimal(value: Any, name: str) -> Decimal:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError) as exc:
        raise TermsError(f"{name}: not a decimal: {value!r}") from exc


def _date(value: Any, name: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise TermsError(f"{name}: not an ISO date: {value!r}") from exc


def parse_value(spec: FieldSpec, value: Any) -> Any:
    """Convert a raw JSON value into the typed value for one field."""
    if value is None:
        return None
    match spec.kind:
        case "decimal":
            return _decimal(value, spec.name)
        case "date":
            return _date(value, spec.name)
        case "int":
            return int(value)
        case "bool":
            if isinstance(value, bool):
                return value
            raise TermsError(f"{spec.name}: not a boolean: {value!r}")
        case "enum":
            if value not in spec.enum:
                raise TermsError(f"{spec.name}: unknown value {value!r}")
            return value
        case "list":
            return [str(v) for v in value]
        case "coupons":
            return [
                StatedCoupon(
                    date=_date(c["date"], spec.name) if c.get("date") else None,
                    amount=_decimal(c["amount"], spec.name),
                    per=_decimal(c["per"], spec.name),
                )
                for c in value
            ]
        case _:
            return str(value)


REQUIRED_FOR_SCHEDULE = (
    "currency", "aggregate_nominal_amount", "specified_denomination", "issue_date",
    "maturity_date", "interest_rate", "coupon_frequency", "first_interest_payment_date",
    "day_count_fraction", "business_day_convention", "final_redemption_amount",
)


def build_terms(values: dict[str, Any]) -> BondTerms:
    """Typed terms from a mapping of field name to raw value (model output or ground truth)."""
    typed = {spec.name: parse_value(spec, values.get(spec.name)) for spec in FIELDS}
    missing = [name for name in REQUIRED_FOR_SCHEDULE if typed[name] is None]
    if missing:
        raise TermsError(f"missing terms needed for the schedule: {', '.join(missing)}")
    typed["interest_commencement_date"] = typed["interest_commencement_date"] or typed["issue_date"]
    typed["accrual_adjusted"] = bool(typed["accrual_adjusted"])
    typed["interest_payment_dates"] = typed["interest_payment_dates"] or []
    typed["business_day"] = typed["business_day"] or []
    typed["stated_coupon_amounts"] = typed["stated_coupon_amounts"] or []
    typed["day_count_fraction"] = DayCount(typed["day_count_fraction"])
    typed["business_day_convention"] = BusinessDayConvention(typed["business_day_convention"])
    typed["rounding_rule"] = RoundingRule(typed["rounding_rule"] or RoundingRule.NOT_STATED)
    return BondTerms(**typed)
