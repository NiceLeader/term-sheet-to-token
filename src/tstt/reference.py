"""Independent cash flow calculation with QuantLib.

Shares only the typed terms with the schedule engine: dates, calendars, day counts and the
bond cash flows all come from QuantLib.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import QuantLib as ql

from tstt.calendars import normalise_centre
from tstt.schedule import minor_unit, round_amount
from tstt.terms import BondTerms, BusinessDayConvention, DayCount

_CALENDARS = {
    "target": lambda: ql.TARGET(),
    "london": lambda: ql.UnitedKingdom(ql.UnitedKingdom.Settlement),
    "new york": lambda: ql.UnitedStates(ql.UnitedStates.Settlement),
    "zurich": lambda: ql.Switzerland(),
    "sydney": lambda: ql.Australia(),
    "hong kong": lambda: ql.HongKong(),
    "stockholm": lambda: ql.Sweden(),
    "tokyo": lambda: ql.Japan(),
    "beijing": lambda: ql.China(),
    "seoul": lambda: ql.SouthKorea(),
    "toronto": lambda: ql.Canada(),
    "frankfurt": lambda: ql.Germany(ql.Germany.Settlement),
}

_CONVENTIONS = {
    BusinessDayConvention.FOLLOWING: ql.Following,
    BusinessDayConvention.MODIFIED_FOLLOWING: ql.ModifiedFollowing,
    BusinessDayConvention.PRECEDING: ql.Preceding,
    BusinessDayConvention.NONE: ql.Unadjusted,
}

_FREQUENCIES = {1: ql.Annual, 2: ql.Semiannual, 4: ql.Quarterly, 12: ql.Monthly}


@dataclass(frozen=True)
class ReferenceFlow:
    kind: str  # coupon | redemption
    payment_date: date
    amount: Decimal  # rounded to the currency minor unit
    accrual_start: date | None = None
    accrual_end: date | None = None
    fraction: float | None = None


@dataclass
class ReferenceResult:
    flows: list[ReferenceFlow]
    notes: list[str]


def to_ql(day: date) -> ql.Date:
    return ql.Date(day.day, day.month, day.year)


def from_ql(day: ql.Date) -> date:
    return date(day.year(), day.month(), day.dayOfMonth())


def _calendar(centres: list[str], notes: list[str]) -> ql.Calendar:
    calendars = []
    for name in centres:
        key = normalise_centre(name)
        if key in _CALENDARS:
            calendars.append(_CALENDARS[key]())
        else:
            notes.append(f"QuantLib has no calendar for {name}; weekends only")
    if not calendars:
        return ql.WeekendsOnly()
    joint = calendars[0]
    for extra in calendars[1:]:
        joint = ql.JointCalendar(joint, extra)
    return joint


def _day_counter(terms: BondTerms, schedule: ql.Schedule, notes: list[str]) -> ql.DayCounter:
    match terms.day_count_fraction:
        case DayCount.ACT_ACT_ICMA:
            return ql.ActualActual(ql.ActualActual.ISMA, schedule)
        case DayCount.THIRTY_360:
            return ql.Thirty360(ql.Thirty360.BondBasis)
        case DayCount.THIRTY_E_360:
            return ql.Thirty360(ql.Thirty360.European)
        case DayCount.ACT_365_FIXED:
            return ql.Actual365Fixed()
        case DayCount.ACT_360:
            return ql.Actual360()
        case DayCount.ACT_ACT_CANADIAN_COMPOUND:
            return ql.Actual365Fixed(ql.Actual365Fixed.Canadian)
        case DayCount.RBA_BOND_BASIS:
            notes.append("RBA Bond Basis approximated by Actual/Actual (ISMA) in QuantLib; "
                         "equal for regular periods only")
            return ql.ActualActual(ql.ActualActual.ISMA, schedule)
    raise ValueError(f"QuantLib mapping missing for {terms.day_count_fraction}")


def _trimmed(value: float) -> Decimal:
    """QuantLib's double without its float noise: thirteen significant digits."""
    raw = Decimal(repr(value))
    return raw.quantize(Decimal(1).scaleb(raw.adjusted() - 12)) if raw else raw


def reference_flows(terms: BondTerms, notional: Decimal | None = None) -> ReferenceResult:
    """Coupons and redemption on `notional`, by default on the calculation amount."""
    notes: list[str] = []
    calendar = _calendar(terms.business_day, notes)
    accrual_convention = (
        _CONVENTIONS[terms.business_day_convention] if terms.accrual_adjusted else ql.Unadjusted
    )
    schedule = ql.Schedule(
        to_ql(terms.interest_commencement_date),
        to_ql(terms.maturity_date),
        ql.Period(_FREQUENCIES[terms.coupon_frequency]),
        calendar,
        accrual_convention,
        ql.Unadjusted,
        ql.DateGeneration.Forward,
        False,
        to_ql(terms.first_interest_payment_date),
    )
    day_counter = _day_counter(terms, schedule, notes)
    basis = float(terms.coupon_basis if notional is None else notional)
    bond = ql.FixedRateBond(
        0,
        basis,
        schedule,
        [float(terms.interest_rate) / 100],
        day_counter,
        _CONVENTIONS[terms.business_day_convention],
        float(terms.final_redemption_amount),
        to_ql(terms.issue_date),
    )
    unit = minor_unit(terms.currency)
    flows: list[ReferenceFlow] = []
    for cashflow in bond.cashflows():
        coupon = ql.as_fixed_rate_coupon(cashflow)
        # QuantLib returns doubles; trim float noise before applying the bond's rounding rule.
        amount = round_amount(_trimmed(cashflow.amount()), unit, terms.rounding_rule)
        if coupon is not None:
            flows.append(ReferenceFlow(
                kind="coupon",
                payment_date=from_ql(cashflow.date()),
                amount=amount,
                accrual_start=from_ql(coupon.accrualStartDate()),
                accrual_end=from_ql(coupon.accrualEndDate()),
                fraction=coupon.accrualPeriod(),
            ))
        else:
            flows.append(ReferenceFlow("redemption", from_ql(cashflow.date()), amount))
    return ReferenceResult(flows, notes)
