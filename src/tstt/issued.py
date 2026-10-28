"""The issuance record: what the token was issued from.

Once every term is reviewed and the replay raises no red flag, the issuance keeps in one file
the documents the terms were read from with their hashes, the reading, every decision of the
reviewer, the terms the token was configured with, where each was read, and the result of the
replay, under one hash. A later question about a payment goes back to the page, the reading
and the reviewer.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tstt.config import Settings
from tstt.fetch import load_manifest
from tstt.terms import FIELDS


def record_path(settings: Settings, run: str, bond_id: str) -> Path:
    return settings.runs_dir / "issued" / run / f"{bond_id}.json"


def digest(record: dict[str, Any]) -> str:
    """SHA-256 of the record without its own hash, in canonical JSON."""
    body = {k: v for k, v in record.items() if k != "sha256"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def issuance_record(run: str, bond_id: str, fields: dict[str, dict[str, Any]],
                    extraction: dict[str, Any] | None, review: dict[str, Any],
                    report: dict[str, Any], manifest: dict[str, Any] | None = None,
                    now: str | None = None) -> dict[str, Any]:
    """The record of one issuance, from the reviewed fields, the reading, the review and the
    saved report of the replay."""
    manifest = manifest or load_manifest()
    bond = next((b for b in manifest["bonds"] if b["id"] == bond_id), None)
    if bond is None:
        raise KeyError(f"unknown bond {bond_id}")
    documents = []
    for d in bond["documents"]:
        meta = manifest["documents"].get(d["doc"], {})
        documents.append({"doc": d["doc"], "role": d["role"], "title": meta.get("title"),
                          "url": meta.get("url"), "sha256": meta.get("sha256"),
                          "bytes": meta.get("bytes")})
    decisions = {}
    for spec in FIELDS:
        entry = review["fields"].get(spec.name) or {}
        decision = {"status": entry.get("status"), "at": entry.get("at")}
        if entry.get("status") == "corrected":
            decision |= {"read": entry.get("original"), "corrected_to": entry.get("value")}
        decisions[spec.name] = decision
    reading = {"run": run}
    if extraction:
        reading |= {k: extraction.get(k) for k in ("model", "mode", "seconds", "cost_usd")}
    else:
        reading["source"] = "verified terms"
    record = {
        "bond": {"id": bond_id, "label": bond.get("label"), "isin": bond.get("isin"),
                 "cusip": bond.get("cusip")},
        "issued_at": now or datetime.now(UTC).isoformat(timespec="seconds"),
        "documents": documents,
        "reading": reading,
        "terms": {spec.name: (fields.get(spec.name) or {}).get("value") for spec in FIELDS},
        "citations": {spec.name: _citations(fields.get(spec.name)) for spec in FIELDS},
        "review": decisions,
        "replay": {"summary": report.get("summary"), "token": report.get("token")},
    }
    record["sha256"] = digest(record)
    return record


def _citations(field: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Where the reading found the term: document, page and quote, stated or defined."""
    return [{"role": role, "doc": c.get("doc"), "page": c.get("page"), "quote": c.get("quote")}
            for role in ("stated_in", "defined_in") for c in (field or {}).get(role) or []]


def save_record(settings: Settings, record: dict[str, Any]) -> Path:
    path = record_path(settings, record["reading"]["run"], record["bond"]["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False, default=str),
                    encoding="utf-8")
    return path


def load_record(settings: Settings, run: str, bond_id: str) -> dict[str, Any] | None:
    path = record_path(settings, run, bond_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
