from tstt.docs import Document, Page
from tstt.extract import BondSource, PageIndex, _snippet, agent
from tstt.llm import Reply, ToolCall, Usage

DOC = Document("ft", "Final Terms", "pdf", [
    Page(1, "The Calculation Agent is named in the Agency Agreement. The amount of each "
            "payment is set out below."),
    Page(2, "Specified Denomination: GBP 1,000. There is no separate amount for calculation."),
    Page(3, "Calculation Amount: GBP 1,000 for the purposes of the Fixed Coupon Amount."),
])
SOURCE = BondSource("test-bond", "Test bond", "XS0000000000", [(DOC, "final_terms")])


class ScriptedClient:
    """Plays back a list of tool calls, one list per step, and records what it was sent."""

    model = "scripted"

    def __init__(self, script: list[list[ToolCall]]):
        self.script = script
        self.sent: list[dict] = []
        self.elided: set[str] = set()

    def tool_step(self, instructions, tools, user=None, previous=None, results=None,
                  tool_choice="auto"):
        self.sent.append({"tools": [t["name"] for t in tools], "tool_choice": tool_choice,
                          "user": user, "results": results or []})
        calls = self.script.pop(0) if self.script else []
        return Reply(self.model, "", Usage(input_tokens=10, output_tokens=1), 0.0,
                     tool_calls=calls)


def search(call_id: str, query: str) -> ToolCall:
    return ToolCall(call_id, "search", {"query": query, "doc": None})


def test_phrase_ranks_first_and_snippet_starts_near_it():
    hits = PageIndex([(DOC, "final_terms")]).search("Calculation Amount")
    assert hits[0][:2] == ("ft", 3)
    assert _snippet(DOC.pages[2].text, '"calculation  amount"').startswith("Calculation Amount")


def test_repeated_call_is_not_run_again():
    client = ScriptedClient([[search("a", "Calculation Amount")],
                             [search("b", '"calculation amount"')]])
    extraction = agent(client, SOURCE, max_steps=6)
    first, second = client.sent[1]["results"][0], client.sent[2]["results"][0]
    assert first[0] == "a" and '"page": 3' in first[1]
    assert second[0] == "b" and second[1].startswith("Same call as step 1")
    assert extraction.tool_log[1]["repeat_of"] == 1


def test_repeat_is_allowed_after_its_output_was_removed():
    client = ScriptedClient([[search("a", "Calculation Amount")],
                             [search("b", "Calculation Amount")]])
    client.elided.add("a")
    agent(client, SOURCE, max_steps=6)
    assert '"page": 3' in client.sent[2]["results"][0][1]


def test_last_steps_can_only_submit_and_rejected_submission_is_kept():
    bad = ToolCall("s", "submit_terms", {"fields": {"currency": {"value": "GBP"}}})
    client = ScriptedClient([[search(str(n), f"word{n}")] for n in range(7)] + [[bad]])
    extraction = agent(client, SOURCE, max_steps=8)
    assert [s["tool_choice"] for s in client.sent] == ["auto"] * 5 + ["required"] * 3
    assert client.sent[-1]["tools"] == ["submit_terms"]
    assert extraction.fields == {"currency": {"value": "GBP"}}
    assert extraction.error.startswith("last submission not accepted")


def test_invalid_arguments_go_back_to_the_model():
    client = ScriptedClient([[ToolCall("x", "submit_terms", {}, error="Expecting value")]])
    agent(client, SOURCE, max_steps=4)
    assert "not valid JSON (Expecting value)" in client.sent[1]["results"][0][1]


def test_rejected_forced_choice_is_sent_again_as_auto(monkeypatch):
    from tstt import llm

    bodies = []

    def fake_post(client, url, body, **kw):
        bodies.append(body)
        if body.get("tool_choice") == "required":
            raise llm.ModelError(f"{url} returned 400: tool_choice not supported")
        return {"ok": True}

    monkeypatch.setattr(llm, "_post", fake_post)
    assert llm._post_tools(None, "u", {"tool_choice": "required"}) == {"ok": True}
    assert [b["tool_choice"] for b in bodies] == ["required", "auto"]
