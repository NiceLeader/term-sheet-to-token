"""Configure, deploy and run a bond token on a local anvil chain.

The token is configured from the schedule engine: dated accrual segments, payment dates,
record dates and the conventions. The token then computes every amount itself, so the
replay checks the contract against the engine rather than echoing it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from web3 import Web3
from web3.exceptions import ContractCustomError, ContractLogicError
from web3.logs import DISCARD

from tstt.calendars import Calendar
from tstt.config import REPO_ROOT
from tstt.schedule import Schedule, minor_unit
from tstt.terms import DayCount, RoundingRule

CONTRACTS_DIR = REPO_ROOT / "contracts"

DAY_COUNTS = {
    DayCount.ACT_ACT_ICMA: 0,
    DayCount.THIRTY_360: 1,
    DayCount.THIRTY_E_360: 2,
    DayCount.ACT_365_FIXED: 3,
    DayCount.ACT_360: 4,
    DayCount.RBA_BOND_BASIS: 5,
    DayCount.ACT_ACT_CANADIAN_COMPOUND: 6,
}
ROUNDINGS = {
    RoundingRule.HALF_UP: 0,
    RoundingRule.HALF_EVEN: 1,
    RoundingRule.DOWN: 2,
    RoundingRule.UP: 3,
    RoundingRule.NOT_STATED: 0,
}
PAY_HOUR = 9 * 3_600  # payments run at 09:00 UTC on the payment date
RATE_SCALE = 10**8
PERCENT_SCALE = 10**8
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december")
WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "five": 5, "seven": 7, "eight": 8, "ten": 10,
                "fifteen": 15, "first": 1, "second": 2, "third": 3, "fifth": 5, "seventh": 7,
                "eighth": 8, "tenth": 10, "fifteenth": 15}


class TokenError(RuntimeError):
    pass


# ---------------------------------------------------------------- record dates


@dataclass(frozen=True)
class RecordRule:
    kind: str  # calendar | business | fixed
    days: int = 1
    fixed: tuple[tuple[int, int], ...] = ()
    note: str | None = None


def record_rule(phrase: str | None) -> RecordRule:
    """Interpret a record date phrase from the documents."""
    if not phrase:
        return RecordRule("business", 1, note="record date not stated; using one business "
                                              "day before the due date")
    # Times of day ("11:30 p.m.") and side remarks in brackets ("15th day if definitive")
    # are not part of the rule for a global or token form.
    text = re.sub(r"\([^)]*\)", " ", phrase.lower())
    text = re.sub(r"\b\d{1,2}[:.]\d{2}\s*(?:a\.?m\.?|p\.?m\.?)?", " ", text)
    fixed = re.findall(r"(" + "|".join(MONTHS) + r")\s+(\d{1,2})", text)
    if fixed:
        return RecordRule("fixed", fixed=tuple((MONTHS.index(m) + 1, int(d)) for m, d in fixed))
    count = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b|\b(" + "|".join(WORD_NUMBERS) + r")\b",
                      text)
    days = 1
    if count:
        days = int(count.group(1)) if count.group(1) else WORD_NUMBERS[count.group(2)]
    if "whether or not" in text or "calendar day" in text:
        return RecordRule("calendar", days)
    if "business day" in text:
        return RecordRule("business", days)
    return RecordRule("calendar", days)


def record_date(rule: RecordRule, due: date, calendar: Calendar) -> date:
    if rule.kind == "fixed":
        candidates = [date(due.year - k, m, d) for k in (0, 1) for m, d in rule.fixed]
        return max(c for c in candidates if c < due)
    if rule.kind == "calendar":
        return due - timedelta(days=rule.days)
    day, moved = due, 0
    while moved < rule.days:
        day -= timedelta(days=1)
        if calendar.is_business_day(day):
            moved += 1
    return day


# ---------------------------------------------------------------- configuration


def timestamp(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


def from_timestamp(value: int) -> date:
    return datetime.fromtimestamp(value, tz=UTC).date()


def to_minor(amount: Decimal, currency: str) -> int:
    value = amount / minor_unit(currency)
    if value != value.to_integral_value():
        raise TokenError(f"{amount} {currency} is not a whole number of minor units")
    return int(value)


def scaled(value: Decimal, scale: int, what: str) -> int:
    result = value * scale
    if result != result.to_integral_value():
        raise TokenError(f"{what} {value} has more digits than the token stores")
    return int(result)


@dataclass
class PaymentPlan:
    index: int
    kind: str  # coupon | redemption
    date: date
    record_date: date
    per_basis_minor: int  # what the engine expects per calculation amount (or per unit)


@dataclass
class TokenConfig:
    terms: dict[str, Any]
    segments: list[tuple[int, int, int, int]]
    payments: list[tuple[int, int, int, int, bool]]
    plan: list[PaymentPlan]
    total_units: int
    notes: list[str] = field(default_factory=list)


def token_config(schedule: Schedule, cash: str, issuer: str, agent: str) -> TokenConfig:
    """Constructor arguments for BondToken from an engine schedule."""
    terms = schedule.terms
    notes: list[str] = []
    if terms.day_count_fraction not in DAY_COUNTS:
        raise TokenError(f"the token has no day count for {terms.day_count_fraction}")
    if terms.rounding_rule == RoundingRule.NOT_STATED:
        notes.append("rounding rule not stated; the token rounds half up")
    currency = terms.currency
    denomination = to_minor(terms.specified_denomination, currency)
    basis = to_minor(schedule.basis, currency)
    if basis == 0 or denomination % basis:
        raise TokenError("the denomination is not a multiple of the calculation amount")
    total_units = terms.aggregate_nominal_amount / terms.specified_denomination
    if total_units != total_units.to_integral_value():
        raise TokenError("the aggregate amount is not a whole number of denominations")

    rule = record_rule(terms.record_date)
    if rule.note:
        notes.append(rule.note)
    calendar = Calendar(terms.business_day)

    segments: list[tuple[int, int, int, int]] = []
    payments: list[tuple[int, int, int, int, bool]] = []
    plan: list[PaymentPlan] = []
    for period in schedule.periods:
        first = len(segments)
        for s in period.segments:
            segments.append((timestamp(s.start), timestamp(s.end), timestamp(s.ref_start),
                             timestamp(s.ref_end)))
        record = record_date(rule, period.payment_date, calendar)
        payments.append((timestamp(period.payment_date), timestamp(record), first,
                         len(period.segments), False))
        plan.append(PaymentPlan(period.index, "coupon", period.payment_date, record,
                                to_minor(period.amount_per_basis, currency)))
    redemption_record = record_date(rule, schedule.redemption_date, calendar)
    plan.append(PaymentPlan(len(schedule.periods), "redemption", schedule.redemption_date,
                            redemption_record,
                            to_minor(schedule.redemption_per_basis, currency)
                            * (denomination // basis)))

    config_terms = {
        "isin": terms.isin or "",
        "currency": currency,
        "cash": cash,
        "issuer": issuer,
        "payingAgent": agent,
        "totalUnits": int(total_units),
        "denomination": denomination,
        "calculationAmount": basis,
        "rate": scaled(terms.interest_rate, RATE_SCALE, "interest rate"),
        "frequency": terms.coupon_frequency,
        "dayCount": DAY_COUNTS[terms.day_count_fraction],
        "rounding": ROUNDINGS[terms.rounding_rule],
        "redemptionPercent": scaled(terms.final_redemption_amount, PERCENT_SCALE,
                                    "redemption amount"),
        "maturityPaymentDate": timestamp(schedule.redemption_date),
        "redemptionRecordDate": timestamp(redemption_record),
    }
    return TokenConfig(config_terms, segments, payments, plan, int(total_units), notes)


# ---------------------------------------------------------------- build and chain


def forge_binary(name: str) -> str:
    folder = os.environ.get("FOUNDRY_BIN")
    candidates = [Path(folder) / name] if folder else []
    candidates.append(Path.home() / ".foundry" / "bin" / name)
    for candidate in candidates:
        for path in (candidate, candidate.with_suffix(".exe")):
            if path.exists():
                return str(path)
    found = shutil.which(name)
    if found:
        return found
    raise TokenError(f"{name} not found; install Foundry or set FOUNDRY_BIN")


def build_contracts(root: Path = CONTRACTS_DIR) -> Path:
    """Compile the contracts under root and return the artifacts folder."""
    result = subprocess.run([forge_binary("forge"), "build", "--root", str(root)],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", check=False)
    if result.returncode != 0:
        raise TokenError(f"forge build failed.\n{result.stdout}\n{result.stderr}")
    return root / "out"


def artifact(out: Path, name: str) -> tuple[list[dict[str, Any]], str]:
    data = json.loads((out / f"{name}.sol" / f"{name}.json").read_text(encoding="utf-8"))
    return data["abi"], data["bytecode"]["object"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Chain:
    """A throwaway anvil node with a genesis time chosen by the caller."""

    def __init__(self, genesis: int):
        self.port = _free_port()
        self.process = subprocess.Popen(
            [forge_binary("anvil"), "--port", str(self.port), "--host", "127.0.0.1",
             "--timestamp", str(genesis), "--accounts", "6", "--silent"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{self.port}"))
        self.errors: list[dict[str, Any]] = []  # error ABI of every deployed contract
        for _ in range(100):
            if self.process.poll() is not None:
                raise TokenError("anvil exited at start")
            try:
                if self.w3.is_connected():
                    break
            except Exception:  # noqa: BLE001 - the node is still starting
                pass
            time.sleep(0.1)
        else:
            self.close()
            raise TokenError("anvil did not start")
        self.accounts = self.w3.eth.accounts

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)

    def __enter__(self) -> Chain:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def now(self) -> int:
        return self.w3.eth.get_block("latest")["timestamp"]

    def warp(self, when: int) -> None:
        """Mine an empty block at `when`, so calls and the next transactions see that time."""
        self.w3.provider.make_request("evm_mine", [max(when, self.now() + 1)])

    def deploy(self, abi: list[dict[str, Any]], bytecode: str, sender: str, *args: Any):
        factory = self.w3.eth.contract(abi=abi, bytecode=bytecode)
        self.errors += [item for item in abi if item.get("type") == "error"]
        receipt = self.send(factory.constructor(*args), sender)
        return self.w3.eth.contract(address=receipt["contractAddress"], abi=abi), receipt

    def send(self, call: Any, sender: str) -> dict[str, Any]:
        tx_hash = call.transact({"from": sender, "gas": 25_000_000})
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        if receipt["status"] != 1:
            name = getattr(call, "fn_name", "constructor")
            reason = revert_reason(self.errors, call, sender) if hasattr(call, "call") else None
            raise TokenError(f"{name} reverted with {reason or 'no reason'}: {tx_hash.hex()}")
        return receipt


def revert_reason(abi: list[dict[str, Any]], call: Any, sender: str) -> str | None:
    """Name of the custom error a call would revert with, or None if it would succeed."""
    try:
        call.call({"from": sender})
    except ContractCustomError as exc:
        data = exc.data if isinstance(exc.data, str) else str(exc.args[0])
        selector = data.removeprefix("0x")[:8]
        for item in abi:
            if item.get("type") == "error":
                signature = f"{item['name']}({','.join(i['type'] for i in item['inputs'])})"
                if Web3.keccak(text=signature).hex().removeprefix("0x")[:8] == selector:
                    return item["name"]
        return f"0x{selector}"
    except ContractLogicError as exc:
        return str(exc)
    return None


# ---------------------------------------------------------------- life cycle


@dataclass
class ExecutedPayment:
    index: int
    kind: str
    scheduled: date
    executed: date
    record_date: date
    per_basis_minor: int  # coupon per calculation amount, or redemption per unit
    total_minor: int
    holders: dict[str, tuple[int, int]]  # holder -> (units of record, amount paid)
    gas_used: int
    expected_units: dict[str, int] = field(default_factory=dict)  # from our own ledger


@dataclass
class Probe:
    name: str
    expected: str
    actual: str | None

    @property
    def passed(self) -> bool:
        return self.actual == self.expected


@dataclass
class Replay:
    config: TokenConfig
    bond_address: str
    cash_address: str
    holders: dict[str, str]  # label -> address
    transfers: list[dict[str, Any]]
    payments: list[ExecutedPayment]
    probes: list[Probe]
    allocation: dict[str, int]
    final_units: dict[str, int]
    deploy_gas: int


def holdings_of_record(allocation: dict[str, int], transfers: list[dict[str, Any]],
                       record: date) -> dict[str, int]:
    """Holdings at the close of the record date, kept independently of the contract."""
    close = timestamp(record) + 86_400
    units = dict(allocation)
    for t in transfers:
        if t["at"] < close:
            units[t["from"]] -= t["units"]
            units[t["to"]] += t["units"]
    return {k: v for k, v in units.items() if v}


def replay(schedule: Schedule, out: Path | None = None) -> Replay:
    """Deploy the token for a schedule and run every payment through to redemption."""
    out = out or build_contracts()
    bond_abi, bond_code = artifact(out, "BondToken")
    cash_abi, cash_code = artifact(out, "CashToken")
    first = schedule.terms.issue_date
    with Chain(timestamp(first) - 86_400) as chain:
        operator, issuer, agent, *investors = chain.accounts
        holders = dict(zip(("A", "B", "C"), investors[:3], strict=True))
        decimals = 0 if minor_unit(schedule.terms.currency) == 1 else 2
        cash, _ = chain.deploy(cash_abi, cash_code, operator,
                               f"{schedule.terms.currency} deposit token",
                               f"{schedule.terms.currency}D", decimals)
        config = token_config(schedule, cash.address, issuer, agent)
        constructor = next(item for item in bond_abi if item["type"] == "constructor")
        terms_tuple = tuple(config.terms[c["name"]] for c in constructor["inputs"][0]["components"])
        bond, deployed = chain.deploy(bond_abi, bond_code, operator, terms_tuple,
                                      config.segments, config.payments)

        units = config.total_units
        allocation = {"A": units // 2, "B": units * 3 // 10}
        allocation["C"] = units - allocation["A"] - allocation["B"]
        chain.warp(timestamp(first))
        chain.send(bond.functions.issue([holders[k] for k in allocation],
                                        list(allocation.values())), issuer)

        per_unit_coupons = sum(p.per_basis_minor for p in config.plan if p.kind == "coupon")
        ratio = config.terms["denomination"] // config.terms["calculationAmount"]
        funding = units * (per_unit_coupons * ratio + config.plan[-1].per_basis_minor)
        chain.send(cash.functions.mint(agent, funding), operator)
        chain.send(cash.functions.approve(bond.address, 2**256 - 1), agent)

        probes: list[Probe] = []
        transfers: list[dict[str, Any]] = []
        executed: list[ExecutedPayment] = []
        last_coupon = max(p.index for p in config.plan if p.kind == "coupon")

        def move(when: int, seller: str, buyer: str, units: int, note: str) -> dict[str, Any]:
            chain.warp(when)
            receipt = chain.send(bond.functions.transfer(holders[buyer], units),
                                 holders[seller])
            at = chain.w3.eth.get_block(receipt["blockNumber"])["timestamp"]
            return {"from": seller, "to": buyer, "units": units, "at": at,
                    "on": from_timestamp(at).isoformat(), "note": note}

        for plan in config.plan:
            if plan.kind == "coupon" and plan.index == 0:
                # Sell after the record date: the seller must still receive this coupon.
                when = timestamp(plan.record_date) + 86_400 + 3_600
                sell = when < timestamp(plan.date) + PAY_HOUR
                just_before = timestamp(plan.date) - 60
                if sell and when < just_before:
                    transfers.append(move(when, "A", "C", allocation["A"] // 5,
                                          "after the record date"))
                chain.warp(just_before)
                probes.append(Probe("payment a minute before its date", "TooEarly",
                                    revert_reason(bond.abi, bond.functions.payCoupon(0), agent)))
                if sell and when >= just_before:
                    transfers.append(move(when, "A", "C", allocation["A"] // 5,
                                          "after the record date"))
            if plan.kind == "coupon" and plan.index == 1:
                # Buy on the record date itself: the buyer must receive this coupon.
                when = timestamp(plan.record_date) + 12 * 3_600
                transfers.append(move(when, "B", "A", allocation["B"] // 10,
                                      "on the record date"))
            chain.warp(timestamp(plan.date) + PAY_HOUR)
            if plan.kind == "coupon":
                if plan.index == last_coupon and plan.date >= schedule.redemption_date:
                    probes.append(Probe("redemption with the final coupon unpaid",
                                        "CouponsOutstanding",
                                        revert_reason(bond.abi, bond.functions.redeem(), agent)))
                if plan.index == 0:
                    probes.append(Probe("payment by someone other than the paying agent",
                                        "NotPayingAgent",
                                        revert_reason(bond.abi, bond.functions.payCoupon(0),
                                                      holders["A"])))
                receipt = chain.send(bond.functions.payCoupon(plan.index), agent)
                events = bond.events.CouponPaid().process_receipt(receipt, errors=DISCARD)
                summary = bond.events.PaymentExecuted().process_receipt(
                    receipt, errors=DISCARD)[0]["args"]
                per_basis, total = summary["perCalculationAmount"], summary["total"]
                if plan.index == 0:
                    probes.append(Probe("second payment of the same coupon", "AlreadyPaid",
                                        revert_reason(bond.abi, bond.functions.payCoupon(0),
                                                      agent)))
                    probes.append(Probe("redemption before maturity", "TooEarly",
                                        revert_reason(bond.abi, bond.functions.redeem(), agent)))
            else:
                receipt = chain.send(bond.functions.redeem(), agent)
                events = bond.events.Redeemed().process_receipt(receipt, errors=DISCARD)
                summary = bond.events.RedemptionExecuted().process_receipt(
                    receipt, errors=DISCARD)[0]["args"]
                per_basis, total = summary["perUnit"], summary["total"]
            label = {address: name for name, address in holders.items()}
            paid = {label[e["args"]["holder"]]: (e["args"]["units"], e["args"]["amount"])
                    for e in events}
            block = chain.w3.eth.get_block(receipt["blockNumber"])
            executed.append(ExecutedPayment(
                index=plan.index, kind=plan.kind, scheduled=plan.date,
                executed=from_timestamp(block["timestamp"]), record_date=plan.record_date,
                per_basis_minor=per_basis, total_minor=total, holders=paid,
                gas_used=receipt["gasUsed"],
                expected_units=holdings_of_record(allocation, transfers, plan.record_date),
            ))
        final_units = {k: bond.functions.balanceOf(a).call() for k, a in holders.items()}
        return Replay(config, bond.address, cash.address, holders, transfers, executed,
                      probes, allocation, final_units, deployed["gasUsed"])
