import json

import httpx
import pytest

from tstt.llm import MetaClient, ModelError, SpendLedger, SpendLimitReached

SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


def response(status="completed", input_tokens=1_000_000, output_tokens=0):
    return {"id": "resp_1", "status": status, "output_text": "{}",
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}


def client(ledger, replies, model="muse-spark-1.3"):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json=replies.pop(0))

    meta = MetaClient("key", model, spend=ledger)
    meta.http = httpx.Client(transport=httpx.MockTransport(handler))
    return meta, sent


def ask(meta):
    return meta.structured("instructions", "user", "terms", SCHEMA)


def test_no_request_is_sent_once_the_limit_is_reached(tmp_path):
    ledger = SpendLedger(tmp_path / "spend.jsonl", limit_usd=2.0)
    meta, sent = client(ledger, [response(), response(), response()])
    ask(meta)
    ask(meta)
    assert ledger.spent() == pytest.approx(2.5)
    with pytest.raises(SpendLimitReached, match=r"\$2.50 of \$2.00"):
        ask(meta)
    assert len(sent) == 2


def test_an_incomplete_response_is_still_counted(tmp_path):
    ledger = SpendLedger(tmp_path / "spend.jsonl", limit_usd=10.0)
    meta, _ = client(ledger, [response(status="incomplete", output_tokens=1_000_000)])
    with pytest.raises(ModelError, match="incomplete"):
        ask(meta)
    assert ledger.spent() == pytest.approx(1.25 + 4.25)


def test_a_model_without_a_price_is_refused_when_a_limit_is_set(tmp_path):
    meta, sent = client(SpendLedger(tmp_path / "spend.jsonl", limit_usd=10.0), [response()],
                        model="muse-spark-9")
    with pytest.raises(SpendLimitReached, match="no price"):
        ask(meta)
    assert not sent


def test_without_a_limit_spend_is_recorded_but_never_refused(tmp_path):
    ledger = SpendLedger(tmp_path / "spend.jsonl")
    meta, sent = client(ledger, [response() for _ in range(3)])
    for _ in range(3):
        ask(meta)
    assert len(sent) == 3 and ledger.spent() == pytest.approx(3.75)


def test_a_lost_conversation_is_sent_whole(tmp_path):
    call = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "search",
            "arguments": '{"query": "Interest Rate"}', "status": "completed"}
    lost = {"error": {"message": "referenced response not found or expired"}}
    replies = [(200, response() | {"output": [call]}), (400, lost), (200, response())]
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        status, body = replies.pop(0)
        return httpx.Response(status, json=body)

    meta = MetaClient("key", spend=SpendLedger(tmp_path / "spend.jsonl", None))
    meta.http = httpx.Client(transport=httpx.MockTransport(handler))
    first = meta.tool_step("instructions", [], user="read the bond")
    meta.tool_step("instructions", [], previous=first, results=[("call_1", "page 3")])
    assert sent[1]["previous_response_id"] == "resp_1"
    assert "previous_response_id" not in sent[2]
    assert [item.get("type") or item["role"] for item in sent[2]["input"]] == [
        "user", "function_call", "function_call_output"]
    assert sent[2]["input"][1] == {"type": "function_call", "call_id": "call_1",
                                   "name": "search", "arguments": '{"query": "Interest Rate"}'}
