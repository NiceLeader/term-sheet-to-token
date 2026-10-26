"""Business day calendars for the financial centres named in bond documents.

Built on the `holidays` package. QuantLib keeps its own calendars, so the reference
calculation in reference.py does not share this code.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import cache

import holidays

from tstt.terms import BusinessDayConvention

# Normalised centre name -> factory for a holiday set covering the given years.
_CENTRES = {
    "target": lambda years: holidays.financial_holidays("XECB", years=years),
    "london": lambda years: holidays.country_holidays("GB", subdiv="ENG", years=years),
    "new york": lambda years: holidays.country_holidays("US", years=years),
    "zurich": lambda years: holidays.country_holidays("CH", subdiv="ZH", years=years),
    "sydney": lambda years: holidays.country_holidays("AU", subdiv="NSW", years=years),
    "hong kong": lambda years: holidays.country_holidays("HK", years=years),
    "stockholm": lambda years: holidays.country_holidays("SE", years=years),
    "tokyo": lambda years: holidays.country_holidays("JP", years=years),
    "beijing": lambda years: holidays.country_holidays("CN", years=years),
    "seoul": lambda years: holidays.country_holidays("KR", years=years),
    "toronto": lambda years: holidays.country_holidays("CA", subdiv="ON", years=years),
    "luxembourg": lambda years: holidays.country_holidays("LU", years=years),
    "frankfurt": lambda years: holidays.country_holidays("DE", subdiv="HE", years=years),
    "manila": lambda years: holidays.country_holidays("PH", years=years),
}

_ALIASES = {
    "target2": "target", "t2": "target", "target system": "target", "euro": "target",
    "new york city": "new york", "nyc": "new york",
    "hongkong": "hong kong", "hk": "hong kong",
    "prc": "beijing", "mainland china": "beijing",
}

YEARS = range(2018, 2041)


def normalise_centre(name: str) -> str | None:
    key = name.strip().lower().rstrip(".")
    key = _ALIASES.get(key, key)
    return key if key in _CENTRES else None


@cache
def _holidays_for(centre: str) -> frozenset[date]:
    return frozenset(_CENTRES[centre](list(YEARS)).keys())


class Calendar:
    """Joint calendar: a day is a business day only if it is one in every centre."""

    def __init__(self, centres: list[str]):
        self.centres: list[str] = []
        self.unknown: list[str] = []
        for name in centres:
            normalised = normalise_centre(name)
            if normalised:
                self.centres.append(normalised)
            else:
                self.unknown.append(name)

    def is_business_day(self, day: date) -> bool:
        if day.weekday() >= 5:
            return False
        return not any(day in _holidays_for(c) for c in self.centres)

    def adjust(self, day: date, convention: BusinessDayConvention) -> date:
        if convention == BusinessDayConvention.NONE:
            return day
        if convention in (BusinessDayConvention.FOLLOWING,
                          BusinessDayConvention.MODIFIED_FOLLOWING):
            moved = day
            while not self.is_business_day(moved):
                moved += timedelta(days=1)
            if convention == BusinessDayConvention.MODIFIED_FOLLOWING and moved.month != day.month:
                return self.adjust(day, BusinessDayConvention.PRECEDING)
            return moved
        moved = day
        while not self.is_business_day(moved):
            moved -= timedelta(days=1)
        return moved
