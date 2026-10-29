import pytest
from test_schedule import terms
from test_token import _have_foundry

from tstt.audit import Difference, audit_token, compare, shown
from tstt.schedule import build_schedule
from tstt.token import Chain, artifact, build_contracts, timestamp, token_config


def test_values_are_shown_as_a_reviewer_reads_them():
    assert shown("rate", 339_900_000) == "3.399%"
    assert shown("redemptionPercent", 10_000_000_000) == "100%"
    assert shown("maturityPaymentDate", timestamp(build_schedule(terms()).redemption_date)) \
        == "2026-10-23"
    assert shown("dayCount", 1) == "THIRTY_360"
    assert shown("rounding", 2) == "DOWN"
    assert shown("isin", "XS1") == "XS1"


def test_compare_lists_only_what_differs():
    config = token_config(build_schedule(terms()), "0xc", "0xi", "0xa")
    assert compare(config, config.terms, config.segments, config.payments) == []
    other = token_config(build_schedule(terms(interest_rate="3.339", rounding_rule="DOWN")),
                         "0xd", "0xj", "0xb")
    payments = list(other.payments)
    date, record, first, count, paid = payments[1]
    payments[1] = (date + 86_400, record, first, count, True)
    assert compare(config, other.terms, other.segments, payments) == [
        Difference("rate", "3.399%", "3.339%"),
        Difference("rounding", "HALF_UP", "DOWN"),
        Difference("payment 1 date", "2025-10-23", "2025-10-24"),
    ]


def _deploy(chain: Chain, out, config) -> str:
    abi, code = artifact(out, "BondToken")
    constructor = next(item for item in abi if item["type"] == "constructor")
    values = tuple(config.terms[c["name"]] for c in constructor["inputs"][0]["components"])
    contract, _ = chain.deploy(abi, code, chain.accounts[0], values, config.segments,
                               config.payments)
    return contract.address


@pytest.mark.skipif(not _have_foundry(), reason="Foundry is not installed")
def test_audit_reads_a_deployed_token():
    schedule = build_schedule(terms())
    out = build_contracts()
    with Chain(timestamp(schedule.terms.issue_date)) as chain:
        cash, issuer, agent = chain.accounts[:3]
        faithful = _deploy(chain, out, token_config(schedule, cash, issuer, agent))
        result = audit_token(schedule, chain.w3, faithful, out)
        assert result.clean
        assert result.payments == 3
        assert result.parties == {"cash": cash, "issuer": issuer, "payingAgent": agent}

        keyed = build_schedule(terms(interest_rate="3.339"))  # two digits swapped
        miskeyed = _deploy(chain, out, token_config(keyed, cash, issuer, agent))
        result = audit_token(schedule, chain.w3, miskeyed, out)
        assert result.differences == [Difference("rate", "3.399%", "3.339%")]


def test_a_timestamp_that_is_not_a_date_is_still_shown():
    assert shown("maturityPaymentDate", 1_800_000_000_000).endswith("(not a date)")
