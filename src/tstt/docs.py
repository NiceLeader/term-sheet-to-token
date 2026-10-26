"""Turn fetched PDF and EDGAR HTML documents into numbered pages of plain text.

PDF pages keep their printed order (page 1 is the first physical page). EDGAR HTML is split
on the page breaks the filer put in the document; HTML without page breaks is cut into
synthetic pages at paragraph boundaries so that citations stay short and checkable.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import pdfplumber
from bs4 import BeautifulSoup

from tstt.config import Settings
from tstt.fetch import doc_path, load_manifest

SYNTHETIC_PAGE_CHARS = 5000
WHITESPACE = re.compile(r"\s+")
PAGE_BREAK = re.compile(
    r"<[^>]+page-break-(?:before|after)\s*:\s*always[^>]*>", re.IGNORECASE
)


@dataclass
class Page:
    number: int
    text: str


@dataclass
class Document:
    doc_id: str
    title: str
    format: str
    pages: list[Page]

    def page(self, number: int) -> Page | None:
        if 1 <= number <= len(self.pages):
            return self.pages[number - 1]
        return None

    @property
    def char_count(self) -> int:
        return sum(len(p.text) for p in self.pages)


def _clean(text: str) -> str:
    text = text.replace("\xa0", " ").replace("​", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def parse_pdf(path: Path) -> list[Page]:
    pages = []
    with pdfplumber.open(path) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            pages.append(Page(index, _clean(page.extract_text() or "")))
    return pages


def _html_fragment_text(fragment: str) -> str:
    soup = BeautifulSoup(fragment, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    # Newlines inside HTML text are only layout; keep line breaks for real blocks.
    for node in soup.find_all(string=True):
        node.replace_with(WHITESPACE.sub(" ", str(node)))
    for row in soup.find_all("tr"):
        cells = [_clean(c.get_text(" ")) for c in row.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        row.replace_with(soup.new_string("\n" + " | ".join(cells) + "\n"))
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "li"]):
        block.insert_after("\n")
    return _clean(soup.get_text(""))


def _split_synthetic(text: str, size: int = SYNTHETIC_PAGE_CHARS) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for paragraph in text.split("\n"):
        if length + len(paragraph) > size and current:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(paragraph)
        length += len(paragraph) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def parse_html(path: Path) -> list[Page]:
    raw = _decode(path.read_bytes())
    fragments = PAGE_BREAK.split(raw)
    texts = [_html_fragment_text(f) for f in fragments]
    texts = [t for t in texts if t]
    if len(texts) <= 1 and texts and len(texts[0]) > SYNTHETIC_PAGE_CHARS * 1.5:
        texts = _split_synthetic(texts[0])
    return [Page(i, t) for i, t in enumerate(texts, start=1)]


def parse_document(settings: Settings, doc_id: str) -> Document:
    manifest = load_manifest()
    meta = manifest["documents"][doc_id]
    path = doc_path(settings, doc_id, meta)
    pages = parse_pdf(path) if meta["format"] == "pdf" else parse_html(path)
    return Document(doc_id=doc_id, title=meta["title"], format=meta["format"], pages=pages)


def cache_path(settings: Settings, doc_id: str) -> Path:
    return settings.parsed_dir / f"{doc_id}.json"


def load_document(settings: Settings, doc_id: str) -> Document:
    """Parsed document from cache, parsing the raw file on first use."""
    cached = cache_path(settings, doc_id)
    if cached.exists():
        data = json.loads(cached.read_text(encoding="utf-8"))
        return Document(
            doc_id=data["doc_id"], title=data["title"], format=data["format"],
            pages=[Page(**p) for p in data["pages"]],
        )
    document = parse_document(settings, doc_id)
    settings.parsed_dir.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(asdict(document), ensure_ascii=False), encoding="utf-8")
    return document


def render_for_model(document: Document, role: str) -> str:
    """Page-tagged text that the model reads and cites from."""
    parts = [f'<document id="{document.doc_id}" role="{role}" title="{document.title}">']
    for page in document.pages:
        parts.append(f'<page n="{page.number}">\n{page.text}\n</page>')
    parts.append("</document>")
    return "\n".join(parts)
