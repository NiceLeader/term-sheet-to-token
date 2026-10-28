"""Consistency checks on extracted terms: red flags a reviewer should see before issuance."""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from tstt.calendars import Calendar
from tstt.cite import FieldCitations
from tstt.evidence import value_in_text
from tstt.schedule import Schedule, add_months, minor_unit, round_amount
from tstt.terms import FIELD_BY_NAME, BondTerms, BusinessDayConvention, DayCount, RoundingRule


@dataclass(frozen=True)
class Flag:
    severity: str  # error | warning | info
    code: str
    message: str
    fields: tuple[str, ...] = field(default_factory=tuple)


def stated_coupon_flags(terms: BondTerms, schedule: Schedule) -> list[Flag]:
    """Printed coupon amounts against the amounts computed from the terms."""
    flags: list[Flag] = []
    unit = minor_unit(terms.currency)
    rate = Fraction(terms.interest_rate) / 100
    for stated in terms.stated_coupon_amounts:
        if stated.date is None:
            periods = [p for p in schedule.periods if p.stub is None]
        else:
            periods = [p for p in schedule.periods
                       if stated.date in (p.unadjusted_payment_date, p.payment_date)]
            if not periods:
                flags.append(Flag("error", "stated_coupon_date",
                                  f"a coupon of {stated.amount} is printed for {stated.date}, "
                                  "which is not an interest payment date of the schedule",
                                  ("stated_coupon_amounts", "first_interest_payment_date")))
                continue
        for period in periods:
            computed = round_amount(rate * period.fraction * Fraction(stated.per), unit,
                                    terms.rounding_rule)
            if computed != stated.amount:
                flags.append(Flag(
                    "error", "stated_coupon_mismatch",
                    f"the documents print {stated.amount} per {stated.per} for "
                    f"{period.payment_date}, the terms give {computed}",
                    ("stated_coupon_amounts", "interest_rate", "day_count_fraction"),
                ))
                break
    return flags


def date_flags(terms: BondTerms, schedule: Schedule) -> list[Flag]:
    flags: list[Flag] = []
    if not terms.issue_date < terms.first_interest_payment_date <= terms.maturity_date:
        flags.append(Flag("error", "date_order",
                          "the first interest payment date must fall after the issue date and "
                          "on or before maturity",
                          ("issue_date", "first_interest_payment_date", "maturity_date")))
    if terms.interest_commencement_date > terms.first_interest_payment_date:
        flags.append(Flag("error", "date_order",
                          "interest starts after the first interest payment date",
                          ("interest_commencement_date", "first_interest_payment_date")))
    else:
        start, first = terms.interest_commencement_date, terms.first_interest_payment_date
        span = (first.year - start.year) * 12 + first.month - start.month - (first.day < start.day)
        regular = 12 // terms.coupon_frequency
        if span >= 2 * regular:
            # A long first coupon is shorter than two regular periods; otherwise a payment
            # date would fall inside it.
            flags.append(Flag("error", "first_period",
                              f"the first interest period runs {span} months, two or more "
                              f"regular periods of {regular} months: a payment date is missing",
                              ("interest_commencement_date", "first_interest_payment_date",
                               "coupon_frequency")))
    if terms.interest_commencement_date < terms.issue_date:
        flags.append(Flag("warning", "accrual_start",
                          "interest commences before the issue date, as only a further tranche "
                          "of an existing note would",
                          ("interest_commencement_date", "issue_date")))
    if terms.interest_payment_dates:
        months = 12 // terms.coupon_frequency
        cycle = sorted({add_months(terms.maturity_date, -k * months,
                                   terms.maturity_date.day).strftime("%m-%d")
                        for k in range(terms.coupon_frequency)})
        if sorted(terms.interest_payment_dates) != cycle:
            flags.append(Flag("error", "payment_dates",
                              f"interest payment dates {terms.interest_payment_dates} do not "
                              f"match a {terms.coupon_frequency} a year cycle ending at "
                              f"maturity ({cycle})",
                              ("interest_payment_dates", "coupon_frequency", "maturity_date")))
    for warning in schedule.warnings:
        if "unknown financial centre" in warning:
            flags.append(Flag("warning", "calendar", warning, ("business_day",)))
        elif "rounding" not in warning:
            flags.append(Flag("warning", "schedule", warning, ("first_interest_payment_date",)))
    return flags


def amount_flags(terms: BondTerms) -> list[Flag]:
    flags: list[Flag] = []
    if terms.aggregate_nominal_amount % terms.specified_denomination:
        flags.append(Flag("error", "denomination",
                          "the aggregate amount is not a whole number of denominations",
                          ("aggregate_nominal_amount", "specified_denomination")))
    if terms.calculation_amount and terms.specified_denomination % terms.calculation_amount:
        flags.append(Flag("error", "calculation_amount",
                          "the denomination is not a multiple of the calculation amount",
                          ("specified_denomination", "calculation_amount")))
    if terms.final_redemption_amount != 100:
        flags.append(Flag("info", "redemption",
                          f"final redemption at {terms.final_redemption_amount}% of nominal",
                          ("final_redemption_amount",)))
    return flags


def convention_flags(terms: BondTerms) -> list[Flag]:
    flags: list[Flag] = []
    if terms.day_count_fraction == DayCount.OTHER:
        flags.append(Flag("error", "day_count", "the day count fraction is not supported",
                          ("day_count_fraction",)))
    if terms.rounding_rule == RoundingRule.NOT_STATED:
        flags.append(Flag("warning", "rounding",
                          "no rounding rule for fixed coupon amounts; half up is assumed",
                          ("rounding_rule",)))
    if terms.business_day_convention == BusinessDayConvention.NONE:
        flags.append(Flag("warning", "no_convention",
                          "no business day convention: a payment due on a holiday is made on "
                          "that day; most conditions move it to the next business day",
                          ("business_day_convention",)))
    if not terms.record_date:
        flags.append(Flag("info", "record_date",
                          "no record date rule; one business day before payment is assumed",
                          ("record_date",)))
    unknown = Calendar(terms.business_day).unknown
    if not terms.business_day:
        flags.append(Flag("warning", "calendar", "no business day centres; weekends only",
                          ("business_day",)))
    elif unknown:
        flags.append(Flag("warning", "calendar", f"no holiday calendar for {unknown}",
                          ("business_day",)))
    return flags


def citation_flags(values: dict, citations: dict[str, FieldCitations]) -> list[Flag]:
    flags: list[Flag] = []
    for name, cited in citations.items():
        if values.get(name) in (None, [], ""):
            continue
        if not cited.supported:
            flags.append(Flag("warning", "unsupported_value",
                              f"{name}: no quote for the value was found on its cited page",
                              (name,)))
        if value_in_text(name, values[name], cited.texts, values) is False:
            flags.append(Flag("warning", "value_not_in_source",
                              f"{name}: {values[name]} is not written in the quotes or on the "
                              "pages they cite", (name,)))
        spec = FIELD_BY_NAME.get(name)
        if spec and spec.needs_definition and not any(c.valid for c in cited.defined):
            flags.append(Flag("warning", "missing_definition",
                              f"{name}: no verified quote of where the term is defined",
                              (name,)))
    return flags


def check_terms(terms: BondTerms, schedule: Schedule,
                citations: dict[str, FieldCitations] | None = None,
                values: dict | None = None) -> list[Flag]:
    flags = (stated_coupon_flags(terms, schedule) + date_flags(terms, schedule)
             + amount_flags(terms) + convention_flags(terms))
    if citations is not None:
        flags += citation_flags(values or {}, citations)
    order = {"error": 0, "warning": 1, "info": 2}
    return sorted(flags, key=lambda f: order[f.severity])
