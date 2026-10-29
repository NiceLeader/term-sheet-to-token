"""Compare the terms a deployed BondToken holds with the terms read from the documents.

A bond already outstanding exists twice: as prose in its documents and as the configuration of
its token. The reading of the documents gives the constructor arguments the token should have
been given; this module reads what the contract holds and lists every value that differs.
Only the chain's RPC endpoint and the token's address are needed, so the check runs against a
token on any EVM network.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from web3 import Web3

from tstt.schedule import Schedule
from tstt.token import (
    DAY_COUNTS,
    PERCENT_SCALE,
    RATE_SCALE,
    ROUNDINGS,
    TokenConfig,
    TokenError,
    artifact,
    build_contracts,
    from_timestamp,
    token_config,
)

PARTIES = ("cash", "issuer", "payingAgent")  # addresses: reported, not compared
TIMESTAMPS = {"maturityPaymentDate", "redemptionRecordDate", "date", "recordDate",
              "start", "end", "refStart", "refEnd"}
PAYMENT_FIELDS = ("date", "recordDate", "firstSegment", "segmentCount")  # then `paid`
SEGMENT_FIELDS = ("start", "end", "refStart", "refEnd")
NOBODY = "0x0000000000000000000000000000000000000000"


@dataclass(frozen=True)
class Difference:
    what: str
    documents: str
    token: str


@dataclass(frozen=True)
class Audit:
    address: str
    parties: dict[str, str]
    payments: int
    differences: list[Difference]

    @property
    def clean(self) -> bool:
        return not self.differences


def shown(name: str, value: Any) -> str:
    """A constructor value as a reviewer reads it: dates, percentages, names of enums."""
    if name in TIMESTAMPS:
        try:
            return from_timestamp(int(value)).isoformat()
        except (OverflowError, OSError, ValueError):
            return f"{value} (not a date)"
    if name == "rate":
        return f"{Decimal(value) / RATE_SCALE:f}%"
    if name == "redemptionPercent":
        return f"{Decimal(value) / PERCENT_SCALE:f}%"
    if name == "dayCount":
        return next((str(k) for k, v in DAY_COUNTS.items() if v == value), str(value))
    if name == "rounding":
        return next((str(k) for k, v in ROUNDINGS.items() if v == value), str(value))
    return str(value)


def read_token(w3: Web3, address: str, abi: list[dict[str, Any]]) \
        -> tuple[dict[str, Any], list[tuple], list[tuple]]:
    """The terms, segments and payments a deployed BondToken holds."""
    contract = w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)
    spec = next(item for item in abi if item.get("name") == "getTerms")
    raw = contract.functions.getTerms().call()
    if isinstance(raw, dict):
        terms = dict(raw)
    else:
        names = [c["name"] for c in spec["outputs"][0]["components"]]
        terms = dict(zip(names, raw, strict=True))
    count = contract.functions.paymentCount().call()
    payments = [tuple(contract.functions.payments(i).call()) for i in range(count)]
    segment_count = max((p[2] + p[3] for p in payments), default=0)
    segments = [tuple(contract.functions.segments(j).call()) for j in range(segment_count)]
    return terms, segments, payments


def compare(config: TokenConfig, terms: dict[str, Any], segments: list[tuple],
            payments: list[tuple]) -> list[Difference]:
    """Every constructor value where the documents and the token disagree."""
    differences = []
    for name, expected in config.terms.items():
        if name in PARTIES:
            continue
        got = terms.get(name)
        if got != expected:
            differences.append(Difference(name, shown(name, expected), shown(name, got)))
    if len(payments) != len(config.payments):
        differences.append(Difference("payments", str(len(config.payments)),
                                      str(len(payments))))
    for i, (expected, got) in enumerate(zip(config.payments, payments, strict=False)):
        for name, e, g in zip(PAYMENT_FIELDS, expected, got, strict=False):
            if e != g:
                differences.append(Difference(f"payment {i} {name}", shown(name, e),
                                              shown(name, g)))
    if len(segments) != len(config.segments):
        differences.append(Difference("segments", str(len(config.segments)),
                                      str(len(segments))))
    for i, (expected, got) in enumerate(zip(config.segments, segments, strict=False)):
        for name, e, g in zip(SEGMENT_FIELDS, expected, got, strict=True):
            if e != g:
                differences.append(Difference(f"segment {i} {name}", shown(name, e),
                                              shown(name, g)))
    return differences


def audit_token(schedule: Schedule, w3: Web3, address: str, out: Path | None = None) -> Audit:
    """Read the token at `address` and compare it with the schedule built from the documents."""
    if not w3.is_connected():
        raise TokenError("no chain answers at the RPC endpoint")
    if not w3.eth.get_code(Web3.to_checksum_address(address)):
        raise TokenError(f"no contract at {address}")
    abi, _ = artifact(out or build_contracts(), "BondToken")
    config = token_config(schedule, NOBODY, NOBODY, NOBODY)
    terms, segments, payments = read_token(w3, address, abi)
    parties = {name: str(terms.get(name, "")) for name in PARTIES}
    return Audit(address, parties, len(payments), compare(config, terms, segments, payments))
