"""Does a value appear in the text it is cited from?

A model can quote the right clause and still report the wrong number. This module renders a
value in the ways prospectuses write it (dates, amounts, rates, convention names) and looks
for any of them in the cited text. Fields that cannot be checked this way return None.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from tstt.terms import FIELD_BY_NAME

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")

DAY_COUNT_PHRASES = {
    "ACT_ACT_ICMA": [r"actual\s*/\s*actual\s*[-(\[]?\s*\(?icma", r"act\s*/\s*act\s*\(?icma",
                     r"actual\s*/\s*actual\s*\(?isma",
                     # The ICMA definition written out, as in a standalone offering memorandum.
                     r"actual number of days in (?:the|such) (?:calculation|accrual) period "
                     r"(?:is )?divided by the product of"],
    "THIRTY_360": [r"30\s*/\s*360",
                   r"360[- ]day year,? (?:consisting )?of (?:12|twelve) 30[- ]day months",
                   # The ISDA definition; with "without regard to the date of the first day or
                   # last day" it is 30E/360 instead.
                   r"year of 360 days with (?:12|twelve) 30[- ]day months(?!,? without regard)"],
    "THIRTY_E_360": [r"30e\s*/\s*360", r"eurobond basis",
                     # The ISDA definition: 30E/360 is the one that ignores the first and
                     # last day of the period.
                     r"year of 360 days with (?:12|twelve) 30[- ]day months,? without regard "
                     r"to the date of the first day or last day"],
    "ACT_365_FIXED": [r"actual\s*/\s*365\s*\(?fixed", r"act\s*/\s*365\s*\(?f",
                      r"actual number of days .{0,80}divided by 365"],
    "ACT_360": [r"actual\s*/\s*360", r"divided by 360"],
    "RBA_BOND_BASIS": [r"rba bond basis"],
    "ACT_ACT_CANADIAN_COMPOUND": [r"actual\s*/\s*actual\s*\(?canadian"],
}
CONVENTION_PHRASES = {
    "MODIFIED_FOLLOWING": [r"modified following"],
    "FOLLOWING": [r"following business day",
                  # "the next day which is a New York business day", "the next day that is a
                  # Business Day". The place is a closed list, so the pattern stays bounded
                  # when compared without spaces.
                  r"next (?:following )?(?:day (?:which|that) is a )?"
                  r"(?:(?:new york|london|target2?|t2|tokyo|hong kong|toronto|sydney|"
                  r"frankfurt|luxembourg|stockholm|zurich|beijing|seoul|relevant|applicable) )?"
                  r"(?:business|payment|banking) day", r"next succeeding",
                  r"convention:?\s*following"],
    # Only the convention named as such: the definition of Modified Following also speaks of
    # "the preceding Business Day".
    "PRECEDING": [r"preceding business day convention", r"convention:?\s*preceding"],
}
# A page that names another dollar; a bare "$" on it is not evidence of US dollars.
OTHER_DOLLARS = (r"(?<![a-z])(?:cad|c\$|cdn\.?\s?\$|canadian dollars?|aud|a\$|australian dollars?|"
                 r"nzd|nz\$|new zealand dollars?|hkd|hk\$|hong kong dollars?|sgd|s\$|"
                 r"singapore dollars?)(?![a-z])")
# Regular expressions, matched between non-letters so that EUR is not found in "European".
CURRENCY_NAMES = {
    "EUR": [r"eur", r"euros?", "€"],
    "USD": [r"usd", r"u\.s\. dollars?", r"us dollars?", r"us\$", r"u\.s\.\$"],
    "GBP": [r"gbp", r"sterling", "£"], "AUD": [r"aud", r"australian dollars?", r"a\$"],
    "CHF": [r"chf", r"swiss francs?"], "SEK": [r"sek", r"swedish kron(?:a|or)"],
    "CAD": [r"cad", r"canadian dollars?"],
    "HKD": [r"hkd", r"hong kong dollars?"], "CNY": [r"cny", r"renminbi", r"rmb", r"cnh"],
    "JPY": [r"jpy", r"yen"],
}


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace(" ", " ")).lower()


def date_patterns(day: date) -> list[str]:
    d, m, y = day.day, MONTHS[day.month - 1].lower(), day.year
    ordinal = r"(?:st|nd|rd|th)?"
    return [
        rf"\b{d}{ordinal}\s+(?:day of\s+)?{m},?\s+{y}\b",
        rf"\b{m}\s+{d}{ordinal},?\s+{y}\b",
        rf"\b{y}-{day.month:02d}-{d:02d}\b",
        rf"\b0?{d}[/.]0?{day.month}[/.]{y}\b",
    ]


def amount_pattern(value: Decimal, zeros: str = "{1,2}") -> str:
    """An amount with any thousands separator and optional trailing zeros after the point.

    `zeros` bounds the zero decimals a whole number may carry; a rate followed by "per cent"
    may carry any number ("3.000 per cent.").
    """
    sign, digits, exponent = value.normalize().as_tuple()
    text = format(value.normalize(), "f")
    whole, _, fraction = text.partition(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    sep = r"[,. ' ]?"
    body = sep.join(groups)
    if fraction:
        body += rf"[.,]{fraction}0*"
    else:
        # By default at most two zero decimals, so 1,000,000 does not match inside
        # 1,000,000,000.
        body += rf"(?:[.,]0{zeros})?"
    return rf"(?<![\d.,]){body}(?![\d]|[.,']\d)"


PERCENT = r"\s*(?:%|per\s*cent|percent)"


PAR_PHRASES = [r"\bat par\b",
               r"redemption amount:? (?:the )?(?:aggregate nominal|principal) amount",
               r"redeemed at (?:their|its) (?:aggregate )?principal amount"]


def redemption_patterns(percent: Decimal, context: dict[str, object]) -> list[str]:
    """Redemption as a percentage, as an amount per denomination, or as par."""
    patterns = [amount_pattern(percent) + PERCENT]
    for basis in ("calculation_amount", "specified_denomination"):
        if context.get(basis):
            amount = Decimal(str(context[basis])) * percent / 100
            patterns.append(amount_pattern(amount) + r"\s*per\b")
    if percent == 100:
        patterns += PAR_PHRASES
    return patterns


def value_patterns(name: str, value: object,
                   context: dict[str, object] | None = None) -> list[str] | None:
    spec = FIELD_BY_NAME.get(name)
    if spec is None or value in (None, "", []):
        return None
    if spec.kind == "date":
        return date_patterns(date.fromisoformat(str(value)))
    if name in ("aggregate_nominal_amount", "specified_denomination", "calculation_amount"):
        return [amount_pattern(Decimal(str(value)))]
    if name == "interest_rate":
        return [amount_pattern(Decimal(str(value)), zeros="+") + PERCENT]
    if name == "final_redemption_amount":
        return redemption_patterns(Decimal(str(value)), context or {})
    if name == "day_count_fraction":
        return DAY_COUNT_PHRASES.get(str(value))
    if name == "business_day_convention":
        return CONVENTION_PHRASES.get(str(value))
    if name == "currency":
        names = CURRENCY_NAMES.get(str(value), [re.escape(str(value).lower())])
        return [rf"(?<![a-z]){n}(?![a-z])" for n in names]
    if name == "isin":
        return [re.escape(str(value).lower())]
    return None


def value_in_text(name: str, value: object, texts: list[str],
                  context: dict[str, object] | None = None) -> bool | None:
    """True if the value is written in any of the texts, None if the field is not checkable.

    `context` holds the other extracted values, used where a field is written relative to
    another one (a redemption amount per denomination).
    """
    patterns = value_patterns(name, value, context)
    if not patterns:
        return None
    flat = [_flat(t) for t in texts]
    if name in ("day_count_fraction", "business_day_convention"):
        # PDF text extraction splits words ("Actual/A ctual"); compare without spaces too.
        flat += [t.replace(" ", "") for t in flat]
        patterns = patterns + [p.replace(r"\s*", "").replace(r"\s+", "").replace(" ", "")
                               for p in patterns]
    found = any(re.search(p, t) for p in patterns for t in flat)
    if not found and name == "currency" and str(value) == "USD":
        # A bare dollar sign before an amount, as in a US prospectus ("$4,000,000,000 4.375%
        # Notes"), unless the page names another dollar.
        found = any(re.search(r"(?<![a-z])\$\s?\d", t) and not re.search(OTHER_DOLLARS, t)
                    for t in flat)
    return found
