"""Fetch the public bond documents listed in sources/manifest.json.

Files are written to $TSTT_DATA_DIR/raw and checked against the SHA-256 recorded in the
manifest. A document without a recorded hash is trusted on first download and its hash and
size are written back with --record. Downloaded files are only ever parsed, never executed.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from tstt.config import MANIFEST_PATH, Settings

MAX_BYTES = 50_000_000
SEC_HOST = "www.sec.gov"
SEC_DELAY_SECONDS = 1.0
EXTENSIONS = {"pdf": ".pdf", "html": ".htm"}


def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(manifest: dict, path: Path = MANIFEST_PATH) -> None:
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def doc_path(settings: Settings, doc_id: str, doc: dict) -> Path:
    return settings.raw_dir / f"{doc_id}{EXTENSIONS[doc['format']]}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _looks_like(fmt: str, head: bytes) -> bool:
    if fmt == "pdf":
        return head.startswith(b"%PDF")
    lowered = head.lower()
    return b"<html" in lowered or b"<!doctype html" in lowered or b"<document>" in lowered


def _headers_for(url: str, settings: Settings) -> dict[str, str]:
    if urlparse(url).hostname == SEC_HOST:
        if not settings.sec_user_agent:
            raise RuntimeError("SEC EDGAR requires SEC_USER_AGENT with a contact address.")
        return {"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"}
    return {"User-Agent": "term-sheet-to-token/0.1"}


def _download(client: httpx.Client, url: str, headers: dict[str, str], target: Path) -> None:
    partial = target.with_suffix(target.suffix + ".part")
    received = 0
    with client.stream("GET", url, headers=headers) as response:
        response.raise_for_status()
        with partial.open("wb") as handle:
            for chunk in response.iter_bytes():
                received += len(chunk)
                if received > MAX_BYTES:
                    raise RuntimeError(f"{url} is larger than {MAX_BYTES} bytes")
                handle.write(chunk)
    partial.replace(target)


def fetch_all(settings: Settings, only: set[str] | None = None, record: bool = False) -> list[str]:
    """Download missing documents and verify hashes. Returns a list of problems."""
    manifest = load_manifest()
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    changed = False
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        for doc_id, doc in manifest["documents"].items():
            if only and doc_id not in only:
                continue
            target = doc_path(settings, doc_id, doc)
            if not target.exists():
                is_sec = urlparse(doc["url"]).hostname == SEC_HOST
                try:
                    _download(client, doc["url"], _headers_for(doc["url"], settings), target)
                except (httpx.HTTPError, RuntimeError) as exc:
                    problems.append(f"{doc_id}: download failed: {exc}")
                    continue
                finally:
                    if is_sec:
                        time.sleep(SEC_DELAY_SECONDS)
            with target.open("rb") as handle:
                head = handle.read(4096)
            if not _looks_like(doc["format"], head):
                problems.append(f"{doc_id}: content does not look like {doc['format']}")
                continue
            digest = sha256_file(target)
            expected = doc.get("sha256")
            if expected and expected != digest:
                problems.append(f"{doc_id}: sha256 mismatch, expected {expected}, got {digest}")
            elif not expected:
                if record:
                    doc["sha256"] = digest
                    doc["bytes"] = target.stat().st_size
                    changed = True
                else:
                    problems.append(f"{doc_id}: no sha256 in manifest (run with --record)")
    if changed:
        save_manifest(manifest)
    return problems
