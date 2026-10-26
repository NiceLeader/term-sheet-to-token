"""Payment schedule engine: turns typed terms into dated, rounded coupon and redemption flows.

This is the code path that configures the token. reference.py recomputes the same flows
with QuantLib so that the two can be compared independently.
"""

from __future__ import annotations

import calendar as _cal
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_UP, Decimal, localcontext
from fractions import Fraction

from tstt.calendars import Calendar
from tstt.daycount import accrual_fraction
from tstt.terms import BondTerms, RoundingRule

ZERO_DECIMAL_CURRENCIES = {"JPY", "KRW", "ISK", "CLP", "VND"}


def minor_unit(currency: str) -> Decimal:
    return Decimal(1) if currency.upper() in ZERO_DECIMAL_CURRENCIES else Decimal("0.01")


def add_months(day: date, months: int, roll_day: int | None = None) -> date:
    month_index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(month_index, 12)
    month += 1
    last = _cal.monthrange(year, month)[1]
    return date(year, month, min(roll_day or day.day, last))


ROUNDING_MODES = {
    RoundingRule.HALF_UP: ROUND_HALF_UP,
    RoundingRule.HALF_EVEN: ROUND_HALF_EVEN,
    RoundingRule.DOWN: ROUND_DOWN,
    RoundingRule.UP: ROUND_UP,
    # Market default for fixed rate notes when the documents are silent.
    RoundingRule.NOT_STATED: ROUND_HALF_UP,
}


def round_amount(value: Fraction | Decimal, unit: Decimal, rule: RoundingRule) -> Decimal:
    if isinstance(value, Fraction):
        with localcontext() as ctx:
            ctx.prec = 40
            value = Decimal(value.numerator) / Decimal(value.denominator)
    return value.quantize(unit, rounding=ROUNDING_MODES[rule])


@dataclass(frozen=True)
class Segment:
    start: date
    end: date
    ref_start: date
    ref_end: date
    fraction: Fraction


@dataclass
class CouponPeriod:
    index: int
    accrual_start: date
    accrual_end: date
    unadjusted_payment_date: date
    payment_date: date
    segments: list[Segment]
    fraction: Fraction
    amount_per_basis: Decimal
    stub: str | None = None


@dataclass
class Schedule:
    terms: BondTerms
    basis: Decimal
    periods: list[CouponPeriod]
    redemption_date: date
    redemption_per_basis: Decimal
    warnings: list[str] = field(default_factory=list)


def _regular_dates(terms: BondTerms, months: int) -> tuple[list[date], list[str]]:
    """Unadjusted payment dates from the first payment date to maturity."""
    warnings: list[str] = []
    roll = terms.maturity_date.day
    backward = []
    k = 0
    while True:
        d = add_months(terms.maturity_date, -k * months, roll)
        if d < terms.first_interest_payment_date:
            break
        backward.append(d)
        k += 1
    dates = sorted(backward)
    if not dates or dates[0] != terms.first_interest_payment_date:
        warnings.append(
            "first interest payment date is not on the regular cycle that ends at maturity"
        )
        dates = sorted(set(dates) | {terms.first_interest_payment_date})
    return dates, warnings


def _first_period_segments(
    terms: BondTerms, start: date, end: date, months: int
) -> tuple[list[tuple[date, date, date, date]], str | None]:
    """Split the first accrual period into pieces that each sit in one notional period."""
    roll = end.day
    quasi_start = add_months(end, -months, roll)
    if start == quasi_start:
        return [(start, end, quasi_start, end)], None
    if start > quasi_start:
        return [(start, end, quasi_start, end)], "short_first"
    pieces = [(quasi_start, end, quasi_start, end)]
    ref_end = quasi_start
    while True:
        ref_start = add_months(ref_end, -months, roll)
        if start >= ref_start:
            pieces.append((start, ref_end, ref_start, ref_end))
            break
        pieces.append((ref_start, ref_end, ref_start, ref_end))
        ref_end = ref_start
    return list(reversed(pieces)), "long_first"


def build_schedule(terms: BondTerms) -> Schedule:
    if 12 % terms.coupon_frequency:
        raise ValueError(f"unsupported coupon frequency {terms.coupon_frequency}")
    months = 12 // terms.coupon_frequency
    cal = Calendar(terms.business_day)
    warnings = [f"unknown financial centre: {name}" for name in cal.unknown]
    if terms.rounding_rule == RoundingRule.NOT_STATED:
        warnings.append("rounding rule not stated; using half up")
    unit = minor_unit(terms.currency)
    basis = terms.coupon_basis
    rate = Fraction(terms.interest_rate) / 100

    dates, date_warnings = _regular_dates(terms, months)
    warnings += date_warnings
    adjusted = [cal.adjust(d, terms.business_day_convention) for d in dates]

    periods: list[CouponPeriod] = []
    accrual_start = terms.interest_commencement_date
    for i, (unadjusted, paid_on) in enumerate(zip(dates, adjusted, strict=True)):
        accrual_end = paid_on if terms.accrual_adjusted else unadjusted
        if i == 0:
            pieces, stub = _first_period_segments(terms, accrual_start, unadjusted, months)
            if terms.accrual_adjusted:
                pieces = [(s, accrual_end if e == unadjusted else e, rs, re)
                          for s, e, rs, re in pieces]
        else:
            ref_start = dates[i - 1]
            pieces, stub = [(accrual_start, accrual_end, ref_start, unadjusted)], None
            if add_months(unadjusted, -months, unadjusted.day) != ref_start:
                stub = "irregular"
        segments = [
            Segment(s, e, rs, re,
                    accrual_fraction(terms.day_count_fraction, s, e, rs, re,
                                     terms.coupon_frequency))
            for s, e, rs, re in pieces
        ]
        fraction = sum((seg.fraction for seg in segments), Fraction(0))
        amount = round_amount(rate * fraction * Fraction(basis), unit, terms.rounding_rule)
        periods.append(CouponPeriod(
            index=i, accrual_start=accrual_start, accrual_end=accrual_end,
            unadjusted_payment_date=unadjusted, payment_date=paid_on, segments=segments,
            fraction=fraction, amount_per_basis=amount, stub=stub,
        ))
        accrual_start = accrual_end

    if dates[-1] != terms.maturity_date:
        warnings.append("last interest payment date differs from the maturity date")
    redemption_date = cal.adjust(terms.maturity_date, terms.business_day_convention)
    redemption = (basis * terms.final_redemption_amount / 100).quantize(unit)
    return Schedule(terms, basis, periods, redemption_date, redemption, warnings)
