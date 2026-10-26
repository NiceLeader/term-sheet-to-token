"""Page images and quote positions for the review screen.

A PDF page is rendered once to PNG and cached. A quote is located on its page as a list of
line boxes (fractions of the page size), using the same word matching as the citation
check, so what the reviewer sees highlighted is what the check accepted. Filings in HTML
have no page images; for them the page text is returned with character spans to mark.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any

import pdfplumber
import pypdfium2

from tstt.cite import WORD, words
from tstt.config import Settings
from tstt.docs import load_document
from tstt.fetch import doc_path, load_manifest

RENDER_SCALE = 2.0
MIN_RATIO = 0.8
LINE_TOLERANCE = 3.0  # points
COLUMN_GAP = 12.0  # points between words before a box is split


def _meta(doc_id: str) -> dict[str, Any]:
    return load_manifest()["documents"][doc_id]


def page_image(settings: Settings, doc_id: str, number: int) -> Path:
    meta = _meta(doc_id)
    target = settings.data_dir / "pageimg" / doc_id / f"{number}.png"
    if target.exists():
        return target
    if meta["format"] != "pdf":
        raise ValueError(f"{doc_id} is not a PDF")
    pdf = pypdfium2.PdfDocument(str(doc_path(settings, doc_id, meta)))
    try:
        if not 1 <= number <= len(pdf):
            raise ValueError(f"{doc_id} has no page {number}")
        bitmap = pdf[number - 1].render(scale=RENDER_SCALE)
        target.parent.mkdir(parents=True, exist_ok=True)
        bitmap.to_pil().save(target)
    finally:
        pdf.close()
    return target


def locate(quote_words: list[str], stream: list[str]) -> tuple[int, int] | None:
    """Start and length of the best match of a quote in a word stream."""
    n = len(quote_words)
    if not n or not stream:
        return None
    for start in range(len(stream) - n + 1):
        if stream[start:start + n] == quote_words:
            return start, n
    first = set(quote_words[: max(3, n // 4)])
    best: tuple[float, int] | None = None
    for start in range(len(stream)):
        if stream[start] not in first:
            continue
        ratio = SequenceMatcher(None, quote_words, stream[start:start + n],
                                autojunk=False).ratio()
        if best is None or ratio > best[0]:
            best = (ratio, start)
    if best and best[0] >= MIN_RATIO:
        return best[1], min(n, len(stream) - best[1])
    return None


@lru_cache(maxsize=64)
def _pdf_tokens(path: str, number: int) -> tuple[float, float, tuple[dict[str, Any], ...]]:
    with pdfplumber.open(path) as pdf:
        page = pdf.pages[number - 1]
        tokens = tuple(page.extract_words())
        return float(page.width), float(page.height), tokens


def _line_boxes(tokens: list[dict[str, Any]], width: float, height: float) -> list[dict]:
    """One box per run of words on a line; a wide gap (a table column) starts a new box."""
    boxes: list[dict[str, float]] = []
    for t in sorted(tokens, key=lambda t: (round(t["top"]), t["x0"])):
        box = next((b for b in boxes if abs(b["top"] - t["top"]) <= LINE_TOLERANCE
                    and 0 <= t["x0"] - b["x1"] <= COLUMN_GAP), None)
        if box is None:
            boxes.append({"x0": t["x0"], "top": t["top"], "x1": t["x1"],
                          "bottom": t["bottom"]})
        else:
            box["x1"] = max(box["x1"], t["x1"])
            box["bottom"] = max(box["bottom"], t["bottom"])
    return [{"x": b["x0"] / width, "y": b["top"] / height, "w": (b["x1"] - b["x0"]) / width,
             "h": (b["bottom"] - b["top"]) / height} for b in boxes]


def _pdf_boxes(settings: Settings, doc_id: str, number: int, quote: str) -> list[dict]:
    path = doc_path(settings, doc_id, _meta(doc_id))
    width, height, tokens = _pdf_tokens(str(path), number)
    stream: list[tuple[str, int]] = [(w, i) for i, t in enumerate(tokens)
                                     for w in words(t["text"])]
    found = locate(words(quote), [w for w, _ in stream])
    if found is None:
        return []
    start, length = found
    picked = sorted({stream[j][1] for j in range(start, start + length)})
    return _line_boxes([tokens[i] for i in picked], width, height)


def _text_spans(text: str, quote: str) -> list[list[int]]:
    matches = list(WORD.finditer(text.lower()))
    found = locate(words(quote), [m.group() for m in matches])
    if found is None:
        return []
    start, length = found
    return [[matches[start].start(), matches[start + length - 1].end()]]


def page_view(settings: Settings, doc_id: str, number: int,
              quotes: list[str]) -> dict[str, Any]:
    """Everything the viewer needs to show one page with its quotes marked."""
    meta = _meta(doc_id)
    document = load_document(settings, doc_id)
    page = document.page(number)
    if page is None:
        raise ValueError(f"{doc_id} has no page {number}")
    view: dict[str, Any] = {"doc": doc_id, "title": meta["title"], "format": meta["format"],
                            "page": number, "pages": len(document.pages), "url": meta["url"]}
    if meta["format"] == "pdf":
        view["image"] = f"/api/page/{doc_id}/{number}.png"
        view["marks"] = [{"quote": q, "boxes": _pdf_boxes(settings, doc_id, number, q)}
                         for q in quotes if q]
    else:
        text = unicodedata.normalize("NFKC", page.text)
        view["text"] = text
        view["marks"] = [{"quote": q, "spans": _text_spans(text, q)} for q in quotes if q]
    return view


def search_pages(settings: Settings, doc_id: str, phrase: str, limit: int = 20) -> list[int]:
    """Pages of a document that contain every word of a phrase, in order of position."""
    wanted = words(phrase)
    if not wanted:
        return []
    pattern = re.compile(r"\b" + r"\W+".join(map(re.escape, wanted)) + r"\b")
    document = load_document(settings, doc_id)
    hits = [p.number for p in document.pages
            if pattern.search(" ".join(words(p.text)))]
    return hits[:limit]
