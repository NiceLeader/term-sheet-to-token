"""Extract the terms of one bond from its documents with a Meta model.

Two modes share one output schema:

* one pass: every document goes in as page-tagged text and the model answers once. This needs
  a long context (Muse Spark reads 1M tokens).
* agent: the model gets tools to list, search and read pages and submits the terms when it
  has found them. This fits a 128K context (Muse Glimmer) and works on any document size.

Every value comes back with verbatim quotes and page numbers, which cite.py checks against
the parsed pages.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tstt.config import Settings
from tstt.docs import Document, load_document, render_for_model
from tstt.fetch import load_manifest
from tstt.llm import LocalClient, MetaClient, Reply, Usage
from tstt.terms import FIELDS, FieldSpec

ROLE_ORDER = {"final_terms": 0, "term_sheet": 0, "pricing_supplement": 0, "supplement": 1,
              "base_prospectus": 2}

INSTRUCTIONS = """\
You read the offering documents of a fixed rate bond and return its economic terms as JSON.

Documents arrive as <document id=... role=...> blocks made of <page n=...> blocks. The final
terms (or pricing supplement or term sheet) complete the conditions in the base prospectus; a
supplement may replace parts of the base conditions. When they conflict, the final terms win,
then the supplement, then the base prospectus. If one document covers several tranches, use
only the tranche with the target identifier.

For every field:
- value: the value in the format the field describes, or null if the documents do not state
  it. Never guess a value that is not supported by the text.
- stated_in: one to three quotes where the value is stated. A quote is copied character for
  character from one page, 5 to 40 words, with no ellipsis and no text joined across pages.
  doc is the document id and page is the n of the <page> it comes from.
- defined_in: quotes where the meaning of the stated term is defined, usually in the terms and
  conditions of the base prospectus or a supplement. Required for day_count_fraction,
  business_day_convention, business_day and rounding_rule; otherwise an empty list.
- note: one short sentence if the value needed judgement, otherwise null.

Field rules:
- business_day_convention is the rule that moves a payment date that is not a business day.
  If the final terms say "Not Applicable" or that the dates are "not adjusted", but the
  conditions say the holder is paid on the next following business day, the value is
  FOLLOWING and accrual_adjusted is false. NONE means the payment is made on the date itself
  even when it is not a business day.
- accrual_adjusted is true only if the documents say that interest periods or amounts are
  adjusted for that rule. They are adjusted when the payment dates are "adjusted in accordance
  with" a convention and the interest periods end on those payment dates, also when the
  conditions say the periods end on Interest Period Dates that default to the Interest
  Payment Dates.
- record_date is the rule that fixes who receives a payment. Documents also call it record
  time, or say payment is made to the person shown on the register at the close of business
  on a day before the due date. Look in the conditions and in the provisions for notes in
  global or digital form.
- business_day lists the places or systems whose business days count for payments, using
  names such as London, New York, TARGET, Hong Kong, Tokyo, Sydney, Zurich, Stockholm.
- rounding_rule is how a computed fixed coupon amount is rounded to the currency sub-unit.
  A clause in the conditions that rounds all currency amounts payable, or the result of any
  calculation under the conditions, counts even if it stands next to the floating rate
  provisions; "anything less than a full unit being rounded down" is DOWN. If the only
  rounding clause is for floating rate notes or for rates, it is NOT_STATED.
- stated_coupon_amounts lists only amounts printed in the documents (fixed coupon amount,
  broken amount); never amounts you compute. per is the nominal amount they are paid on.
- Amounts are plain decimals without separators or currency, e.g. "100000000".
"""

AGENT_INSTRUCTIONS = INSTRUCTIONS + """
You cannot see the documents directly. Use list_documents, search and read_page to find each
term, read the pages you cite, and call submit_terms once with all fields. If submit_terms
returns errors, fix them and call it again.

Not every bond states every field. Look for each term in the final terms and in the
conditions of the base prospectus; if two or three searches with different wording do not
find it, it is not stated: use null and move on. Do not repeat a search or a page you have
already seen.
"""


# ---------------------------------------------------------------- schema


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _strict_object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


CITATION = _strict_object({
    "doc": {"type": "string"},
    "page": {"type": "integer"},
    "quote": {"type": "string"},
})


def _value_schema(spec: FieldSpec) -> dict[str, Any]:
    match spec.kind:
        case "int":
            return _nullable({"type": "integer"})
        case "bool":
            return _nullable({"type": "boolean"})
        case "enum":
            return _nullable({"type": "string", "enum": list(spec.enum)})
        case "list":
            return _nullable({"type": "array", "items": {"type": "string"}})
        case "coupons":
            return _nullable({"type": "array", "items": _strict_object({
                "date": _nullable({"type": "string"}),
                "amount": {"type": "string"},
                "per": {"type": "string"},
            })})
        case _:
            return _nullable({"type": "string"})


def extraction_schema() -> dict[str, Any]:
    fields = {}
    for spec in FIELDS:
        value = _value_schema(spec)
        value["description"] = spec.description
        fields[spec.name] = _strict_object({
            "value": value,
            "stated_in": {"type": "array", "items": CITATION},
            "defined_in": {"type": "array", "items": CITATION},
            "note": _nullable({"type": "string"}),
        })
    return _strict_object({"fields": _strict_object(fields)})


# ---------------------------------------------------------------- inputs


@dataclass
class BondSource:
    bond_id: str
    label: str
    identifier: str
    documents: list[tuple[Document, str]]  # (document, role), most specific first


def bond_source(settings: Settings, bond_id: str) -> BondSource:
    manifest = load_manifest()
    bond = next((b for b in manifest["bonds"] if b["id"] == bond_id), None)
    if bond is None:
        raise KeyError(f"unknown bond {bond_id}")
    entries = sorted(bond["documents"], key=lambda d: ROLE_ORDER.get(d["role"], 3))
    documents = [(load_document(settings, d["doc"]), d["role"]) for d in entries]
    identifier = f"ISIN {bond['isin']}" if bond.get("isin") else f"CUSIP {bond.get('cusip')}"
    return BondSource(bond_id, bond["label"], identifier, documents)


def target_line(source: BondSource) -> str:
    return f"Target bond: {source.label} ({source.identifier})."


# ---------------------------------------------------------------- results


@dataclass
class Extraction:
    bond_id: str
    mode: str  # one_pass | agent
    model: str
    fields: dict[str, dict[str, Any]]
    usage: Usage
    seconds: float
    cost_usd: float | None
    steps: int = 1
    tool_log: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def values(self) -> dict[str, Any]:
        return {name: f.get("value") for name, f in self.fields.items()}

    def save(self, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{self.bond_id}.json"
        path.write_text(json.dumps(asdict(self), indent=1, ensure_ascii=False, default=str),
                        encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> Extraction:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["usage"] = Usage(**data["usage"])
        return cls(**data)


def one_pass(client: MetaClient | LocalClient, source: BondSource) -> Extraction:
    # Documents first, target last: repeated runs on the same bond reuse the cached prefix.
    body = "\n\n".join(render_for_model(doc, role) for doc, role in source.documents)
    user = f"{body}\n\n{target_line(source)}"
    reply = client.structured(INSTRUCTIONS, user, "bond_terms", extraction_schema())
    fields = reply.json()["fields"]
    return Extraction(source.bond_id, "one_pass", client.model, fields, reply.usage,
                      reply.seconds, reply.usage.cost(client.model))


# ---------------------------------------------------------------- agent


TOKEN = re.compile(r"[a-z0-9]+(?:[.,][0-9]+)*")


class PageIndex:
    """A small BM25 index over the pages of a bond's documents. A page that contains the
    query as a phrase ranks above pages that only share its words."""

    def __init__(self, documents: list[tuple[Document, str]]):
        self.documents = {doc.doc_id: (doc, role) for doc, role in documents}
        self.pages: list[tuple[str, int, Counter[str], str]] = []
        for doc, _ in documents:
            for page in doc.pages:
                words = TOKEN.findall(page.text.lower())
                self.pages.append((doc.doc_id, page.number, Counter(words), " ".join(words)))
        self.df: Counter[str] = Counter()
        for _, _, terms, _ in self.pages:
            self.df.update(terms.keys())
        self.avg = sum(sum(t.values()) for _, _, t, _ in self.pages) / max(len(self.pages), 1)

    def search(self, query: str, doc: str | None = None,
               k: int = 6) -> list[tuple[str, int, float]]:
        words = TOKEN.findall(query.lower())
        phrase = " ".join(words)
        n = len(self.pages)
        scored = []
        for doc_id, number, terms, flat in self.pages:
            if doc and doc_id != doc:
                continue
            length = sum(terms.values())
            score = 0.0
            for word in words:
                tf = terms.get(word, 0)
                if not tf:
                    continue
                idf = math.log(1 + (n - self.df[word] + 0.5) / (self.df[word] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / self.avg))
            if score > 0 and len(words) > 1 and f" {phrase} " in f" {flat} ":
                score *= 2
            if score > 0:
                scored.append((doc_id, number, score))
        return sorted(scored, key=lambda s: -s[2])[:k]


def _snippet(text: str, query: str, width: int = 300) -> str:
    """The text around the query phrase, or around its first word found on the page."""
    lower = text.lower()
    phrase = re.search(r"\W+".join(map(re.escape, TOKEN.findall(query.lower()))), lower)
    if phrase:
        start = max(phrase.start() - width // 3, 0)
    else:
        positions = [lower.find(w) for w in TOKEN.findall(query.lower()) if w in lower]
        start = max(min(positions) - width // 3, 0) if positions else 0
    return " ".join(text[start:start + width].split())


TOOLS = [
    {"name": "list_documents",
     "description": "List the documents of this bond with their role and number of pages.",
     "parameters": _strict_object({})},
    {"name": "search",
     "description": "Keyword search over the pages. Returns the best pages with a snippet. "
                    "Use the words a prospectus would use, e.g. 'Day Count Fraction'.",
     "parameters": _strict_object({"query": {"type": "string"},
                                   "doc": _nullable({"type": "string"})})},
    {"name": "read_page",
     "description": "Full text of one page.",
     "parameters": _strict_object({"doc": {"type": "string"}, "page": {"type": "integer"}})},
    {"name": "submit_terms",
     "description": "Submit all fields. Call once you have read the pages you cite.",
     "parameters": extraction_schema()},
]

PAGE_LIMIT = 12_000
REMIND_STEPS = 10
WRAP_UP_STEPS = 3


def _run_tool(index: PageIndex, name: str, args: dict[str, Any]) -> str:
    if name == "list_documents":
        return json.dumps([{"doc": doc.doc_id, "role": role, "title": doc.title,
                            "pages": len(doc.pages)}
                           for doc, role in index.documents.values()])
    if name == "search":
        hits = index.search(args.get("query", ""), args.get("doc"))
        out = []
        for doc_id, number, score in hits:
            text = index.documents[doc_id][0].page(number).text
            out.append({"doc": doc_id, "page": number, "score": round(score, 2),
                        "snippet": _snippet(text, args.get("query", ""))})
        return json.dumps(out) if out else "no matches"
    if name == "read_page":
        entry = index.documents.get(args.get("doc", ""))
        page = entry[0].page(int(args.get("page", 0))) if entry else None
        if page is None:
            return "no such page"
        text = page.text if len(page.text) <= PAGE_LIMIT else page.text[:PAGE_LIMIT] + " [...]"
        return f'<page doc="{entry[0].doc_id}" n="{page.number}">\n{text}\n</page>'
    return f"unknown tool {name}"


def _check_submission(fields: dict[str, Any]) -> list[str]:
    from tstt.terms import TermsError, build_terms

    try:
        build_terms({name: f.get("value") for name, f in fields.items()})
    except TermsError as exc:
        return [str(exc)]
    return []


def _call_key(name: str, args: dict[str, Any]) -> str:
    """The same call whatever the case, quoting or spacing of its arguments."""
    return name + json.dumps({k: " ".join(TOKEN.findall(v.lower())) if isinstance(v, str) else v
                              for k, v in sorted(args.items())})


def agent(client: MetaClient | LocalClient, source: BondSource, max_steps: int = 50,
          on_step: Callable[[dict[str, Any]], None] | None = None) -> Extraction:
    """Let the model search and read the documents with tools until it submits the terms.

    A repeated call gets a pointer to the earlier result instead of the same text again. Ten
    steps before the limit the model is reminded; in the last steps it can only submit. If no
    submission passes validation, the last one is kept with its errors so the report can show
    what was missing.

    `on_step` receives every tool call as it happens, with the time and tokens so far.
    """
    index = PageIndex(source.documents)
    usage = Usage()
    log: list[dict[str, Any]] = []
    seen: dict[str, tuple[int, str]] = {}
    start = time.monotonic()

    def record(entry: dict[str, Any]) -> None:
        log.append(entry)
        if on_step:
            on_step(entry | {"seconds": round(time.monotonic() - start, 1),
                             "input_tokens": usage.input_tokens,
                             "output_tokens": usage.output_tokens})

    def finish(fields: dict[str, Any], steps: int, error: str | None = None) -> Extraction:
        return Extraction(source.bond_id, "agent", client.model, fields, usage,
                          time.monotonic() - start, usage.cost(client.model), steps=steps,
                          tool_log=log, error=error)

    reply: Reply | None = None
    results: list[tuple[str, str]] = []
    prompt: str | None = target_line(source)
    rejected: tuple[dict[str, Any], list[str]] | None = None
    for step in range(1, max_steps + 1):
        wrap_up = max_steps - step < WRAP_UP_STEPS
        reply = client.tool_step(AGENT_INSTRUCTIONS, TOOLS[-1:] if wrap_up else TOOLS,
                                 user=prompt, previous=reply, results=results,
                                 tool_choice="required" if wrap_up else "auto")
        usage.add(reply.usage)
        prompt, results = None, []
        if max_steps - step == REMIND_STEPS:
            prompt = (f"{REMIND_STEPS} steps left. Find what is still missing, then call "
                      "submit_terms; use null for any term the documents do not state.")
        elif max_steps - step == WRAP_UP_STEPS:
            prompt = ("No steps left for reading. Call submit_terms now with what you have "
                      "found; use null for any term you could not find.")
        if not reply.tool_calls:
            # A model that answers in prose is sent back to the tools.
            prompt = "Use the tools to find the terms, then call submit_terms with all fields."
            record({"step": step, "tool": None, "text": reply.text[:500]})
            continue
        for call in reply.tool_calls:
            if call.error:
                record({"step": step, "tool": call.name, "errors": [call.error]})
                results.append((call.call_id, f"The arguments were not valid JSON "
                                              f"({call.error}). Call {call.name} again."))
                continue
            if call.name == "submit_terms":
                fields = call.arguments.get("fields", {})
                errors = _check_submission(fields)
                record({"step": step, "tool": call.name, "errors": errors})
                if not errors:
                    return finish(fields, step)
                rejected = (fields, errors)
                results.append((call.call_id, "Not accepted: " + "; ".join(errors)))
                continue
            key = _call_key(call.name, call.arguments)
            if key in seen and seen[key][1] not in client.elided:
                output = (f"Same call as step {seen[key][0]}; its result has not changed. Use "
                          "what you found, try other words, or call submit_terms.")
                record({"step": step, "tool": call.name, "args": call.arguments,
                        "repeat_of": seen[key][0]})
            else:
                seen[key] = (step, call.call_id)
                output = _run_tool(index, call.name, call.arguments)
                record({"step": step, "tool": call.name, "args": call.arguments,
                        "chars": len(output)})
            results.append((call.call_id, output))
    if rejected:
        return finish(rejected[0], max_steps,
                      "last submission not accepted: " + "; ".join(rejected[1]))
    return finish({}, max_steps, f"no submission within {max_steps} steps")
