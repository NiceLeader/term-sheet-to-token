"""Check that every quote a model returns is really on the page it cites.

Quotes and pages are compared as streams of lower-case words, so differences in quotes,
dashes, table separators and line-break hyphens do not count, while any change of wording or
numbers does.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from tstt.docs import Document

WORD = re.compile(r"[a-z0-9%]+")
FUZZY_MIN = 0.9
SHORT_WORDS = 3


def words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"-\s*\n\s*", "", text)  # hyphenation at line breaks
    return WORD.findall(text)


@dataclass(frozen=True)
class QuoteCheck:
    status: str  # exact | fuzzy | wrong_page | wrong_doc | not_found | empty
    doc: str
    page: int
    found_doc: str | None = None
    found_page: int | None = None
    score: float = 0.0
    short: bool = False

    @property
    def valid(self) -> bool:
        return self.status in ("exact", "fuzzy")


def _best_window(quote: list[str], page: list[str]) -> float:
    if not quote or not page:
        return 0.0
    joined_quote, joined_page = " ".join(quote), " ".join(page)
    if joined_quote in joined_page:
        return 1.0
    n = len(quote)
    first = set(quote[: max(3, n // 4)])
    # A near match may differ in wording, never in a number.
    numbers = {w for w in quote if any(ch.isdigit() for ch in w)}
    best = 0.0
    for start in range(0, max(len(page) - n + 1, 1)):
        if page[start] not in first:
            continue
        window = page[start:start + n + 2]
        if not numbers <= set(window):
            continue
        ratio = SequenceMatcher(None, quote, window, autojunk=False).ratio()
        if ratio > best:
            best = ratio
            if best == 1.0:
                break
    return best


class Citations:
    """Word streams of a bond's pages, built once and reused for every quote."""

    def __init__(self, documents: list[Document]):
        self.pages: dict[tuple[str, int], list[str]] = {}
        self.text: dict[tuple[str, int], str] = {}
        for doc in documents:
            for page in doc.pages:
                self.pages[(doc.doc_id, page.number)] = words(page.text)
                self.text[(doc.doc_id, page.number)] = page.text

    def _match(self, quote: list[str], key: tuple[str, int]) -> float:
        page = self.pages.get(key)
        return _best_window(quote, page) if page else 0.0

    def check(self, doc: str, page: int, quote: str) -> QuoteCheck:
        q = words(quote)
        if not q:
            return QuoteCheck("empty", doc, page)
        short = len(q) < SHORT_WORDS
        score = self._match(q, (doc, page))
        if score == 1.0:
            return QuoteCheck("exact", doc, page, doc, page, 1.0, short)
        if score >= FUZZY_MIN:
            return QuoteCheck("fuzzy", doc, page, doc, page, score, short)
        best: tuple[float, str, int] | None = None
        for (other_doc, other_page) in self.pages:
            if (other_doc, other_page) == (doc, page):
                continue
            s = self._match(q, (other_doc, other_page))
            if s >= FUZZY_MIN and (best is None or s > best[0]):
                best = (s, other_doc, other_page)
                if s == 1.0 and other_doc == doc:
                    break
        if best:
            status = "wrong_page" if best[1] == doc else "wrong_doc"
            return QuoteCheck(status, doc, page, best[1], best[2], best[0], short)
        return QuoteCheck("not_found", doc, page, score=score, short=short)


@dataclass
class FieldCitations:
    name: str
    stated: list[QuoteCheck]
    defined: list[QuoteCheck]
    texts: list[str] = field(default_factory=list)  # the quotes and the pages they cite

    @property
    def supported(self) -> bool:
        """At least one quote for the value is on its cited page."""
        return any(c.valid for c in self.stated)


def check_fields(fields: dict[str, dict], citations: Citations) -> dict[str, FieldCitations]:
    out = {}
    for name, f in fields.items():
        stated = [citations.check(c["doc"], int(c["page"]), c["quote"])
                  for c in f.get("stated_in") or []]
        defined = [citations.check(c["doc"], int(c["page"]), c["quote"])
                   for c in f.get("defined_in") or []]
        cited = (f.get("stated_in") or []) + (f.get("defined_in") or [])
        texts = [c["quote"] for c in cited if c.get("quote")]
        texts += [citations.text[key] for key in dict.fromkeys(
            (c["doc"], int(c["page"])) for c in cited) if key in citations.text]
        out[name] = FieldCitations(name, stated, defined, texts)
    return out
