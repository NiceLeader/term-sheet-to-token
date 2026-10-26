"""Day count fractions, written from the ISDA and ICMA definitions.

Fractions are exact rationals so that the token, this engine and QuantLib can be compared
to the last cent without floating point noise.
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction

from tstt.terms import DayCount


def _thirty_360(start: date, end: date, european: bool) -> Fraction:
    d1, d2 = start.day, end.day
    if european:
        d1 = min(d1, 30)
        d2 = min(d2, 30)
    else:
        # 30/360 (Bond Basis): D2 is cut to 30 only when D1 is 30 or 31.
        d1 = min(d1, 30)
        if d2 == 31 and d1 == 30:
            d2 = 30
    days = 360 * (end.year - start.year) + 30 * (end.month - start.month) + (d2 - d1)
    return Fraction(days, 360)


def accrual_fraction(
    convention: DayCount,
    start: date,
    end: date,
    ref_start: date,
    ref_end: date,
    frequency: int,
) -> Fraction:
    """Fraction of a year for the accrual from start to end.

    ref_start and ref_end bound the regular (notional) coupon period that contains the
    accrual. Conventions that do not need a reference period ignore them.
    """
    days = (end - start).days
    regular = start == ref_start and end == ref_end
    match convention:
        case DayCount.ACT_ACT_ICMA:
            return Fraction(days, frequency * (ref_end - ref_start).days)
        case DayCount.THIRTY_360:
            return _thirty_360(start, end, european=False)
        case DayCount.THIRTY_E_360:
            return _thirty_360(start, end, european=True)
        case DayCount.ACT_365_FIXED:
            return Fraction(days, 365)
        case DayCount.ACT_360:
            return Fraction(days, 360)
        case DayCount.RBA_BOND_BASIS | DayCount.ACT_ACT_CANADIAN_COMPOUND:
            # Regular periods pay rate / frequency; irregular periods accrue actual/365.
            return Fraction(1, frequency) if regular else Fraction(days, 365)
    raise ValueError(f"unsupported day count {convention}")
