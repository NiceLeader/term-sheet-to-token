"""Review screen: terms with their sources, the token's payments, and the red flags.

`tstt serve` starts this app on localhost. It reads the saved extractions, reports, evaluation
and mutation results under the data folder, renders cited pages on demand, can verify an
extraction it has not verified yet, and records the reviewer's approval or correction of each
term before the token is issued.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from tstt.cite import Citations, check_fields
from tstt.config import REPO_ROOT, Settings, load_settings
from tstt.docs import load_document
from tstt.evidence import value_in_text
from tstt.extract import Extraction, agent, bond_source, one_pass
from tstt.fetch import load_manifest
from tstt.pages import page_image, page_view, search_pages
from tstt.review import (
    GROUND_TRUTH,
    RUN_NAME,
    corrected,
    discard,
    load_review,
    mark_issued,
    record,
    reviewed_report_path,
    reviewed_values,
    unreviewed,
)
from tstt.second import differing, pick_second, reading_label, readings
from tstt.terms import FIELD_BY_NAME, FIELDS, TermsError, build_terms

WEB_DIR = REPO_ROOT / "web"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})

FIELD_GROUPS: dict[str, list[tuple[str, str]]] = {
    "Instrument": [("isin", "ISIN"), ("issuer", "Issuer"), ("currency", "Currency"),
                   ("aggregate_nominal_amount", "Amount issued"),
                   ("specified_denomination", "Denomination"),
                   ("calculation_amount", "Calculation amount")],
    "Dates": [("issue_date", "Issue date"),
              ("interest_commencement_date", "Interest from"),
              ("first_interest_payment_date", "First interest payment"),
              ("maturity_date", "Maturity")],
    "Interest": [("interest_rate", "Rate (% p.a.)"), ("coupon_frequency", "Payments a year"),
                 ("interest_payment_dates", "Payment dates"),
                 ("day_count_fraction", "Day count fraction"),
                 ("rounding_rule", "Rounding"),
                 ("interest_on_holding", "Interest on each holding"),
                 ("stated_coupon_amounts", "Printed coupon amounts")],
    "Payment days": [("business_day_convention", "Business day convention"),
                     ("accrual_adjusted", "Accrual adjusted"),
                     ("business_day", "Business day centres"),
                     ("record_date", "Record date")],
    "Redemption": [("final_redemption_amount", "Redemption (% of nominal)")],
}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _runs(settings: Settings) -> list[dict[str, Any]]:
    runs = [{"id": "groundtruth", "label": "Verified terms (ground truth)", "kind": "truth"}]
    folder = settings.runs_dir / "extractions"
    for path in sorted(folder.iterdir()) if folder.exists() else []:
        if path.is_dir():
            runs.append(_run_entry(path))
    return runs


def _run_entry(path: Path) -> dict[str, Any]:
    """Mode and model come from a saved extraction; the folder name only names the run."""
    mode, _, model = path.name.partition("-")
    first = next(iter(sorted(path.glob("*.json"))), None)
    saved = _read_json(first) if first else None
    if isinstance(saved, dict):
        mode, model = saved.get("mode") or mode, saved.get("model") or model
    default = f"{mode}-{model}"
    tag = path.name.removeprefix(default).lstrip("-") if path.name.startswith(default) \
        else path.name
    label = f"{model} ({mode.replace('_', '-')}{', ' + tag if tag else ''})"
    return {"id": path.name, "kind": "extraction", "mode": mode, "model": model, "label": label,
            "tag": tag or None}


def _known_run(settings: Settings, run: str) -> str:
    """A run id from the URL, accepted only if it names a saved run, so it is safe in a path."""
    if run != "groundtruth" and not (RUN_NAME.fullmatch(run)
                                     and (settings.runs_dir / "extractions" / run).is_dir()):
        raise HTTPException(404, f"unknown run {run}")
    return run


def _known_bond(bond_id: str) -> dict[str, Any]:
    entry = next((b for b in load_manifest()["bonds"] if b["id"] == bond_id), None)
    if entry is None:
        raise HTTPException(404, f"unknown bond {bond_id}")
    return entry


def _known_doc(doc_id: str) -> str:
    if doc_id not in load_manifest()["documents"]:
        raise HTTPException(404, f"unknown document {doc_id}")
    return doc_id


def _source_fields(settings: Settings, run: str, bond_id: str) -> tuple[dict, dict | None]:
    """Fields in the extractor's shape, and the extraction record if there is one."""
    if run == "groundtruth":
        truth = _read_json(GROUND_TRUTH / f"{bond_id}.json")
        if truth is None:
            raise HTTPException(404, f"no ground truth for {bond_id}")
        fields = {}
        for name, f in truth["fields"].items():
            cites = []
            if f.get("doc"):
                cites.append({"doc": f["doc"], "page": f.get("page"), "quote": None})
            defined = [{"doc": f["definition"]["doc"], "page": f["definition"].get("page"),
                        "quote": None}] if f.get("definition") else []
            fields[name] = {"value": f.get("value"), "stated_in": cites,
                            "defined_in": defined, "note": f.get("check")}
        return fields, None
    path = settings.runs_dir / "extractions" / run / f"{bond_id}.json"
    if not path.exists():
        raise HTTPException(404, f"no extraction {run}/{bond_id}")
    extraction = Extraction.load(path)
    return extraction.fields, {
        "model": extraction.model, "mode": extraction.mode, "seconds": extraction.seconds,
        "steps": extraction.steps, "usage": extraction.usage.__dict__,
        "cost_usd": extraction.cost_usd, "error": extraction.error,
        "tool_log": extraction.tool_log,
    }


def _reviewed(settings: Settings, run: str, bond_id: str) -> tuple[dict, dict | None, dict]:
    """Source fields with the reviewer's corrections applied, the extraction, the review."""
    fields, extraction = _source_fields(settings, run, bond_id)
    review = load_review(settings, run, bond_id)
    return reviewed_values(fields, review), extraction, review


def _report_path(settings: Settings, run: str, bond_id: str, review: dict) -> Path:
    if corrected(review):
        return reviewed_report_path(settings, run, bond_id)
    return settings.runs_dir / "reports" / run / f"{bond_id}.json"


def _review_summary(review: dict) -> dict[str, Any]:
    return {"reviewed": len(FIELDS) - len(unreviewed(review)), "total": len(FIELDS),
            "corrected": sum(e["status"] == "corrected" for e in review["fields"].values()),
            "issued": review["issued"]}


def _field_rows(settings: Settings, bond_id: str, fields: dict, report: dict | None,
                quoted: bool, review: dict | None = None,
                second: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """One row per term; `second` holds the values of a second reading to compare with."""
    documents = [doc for doc, _ in bond_source(settings, bond_id).documents]
    checked = check_fields(fields, Citations(documents)) if quoted else None
    values = {n: f.get("value") for n, f in fields.items()}
    differ = set(differing(values, second)) if second is not None else set()
    flagged: dict[str, list[dict]] = {}
    for flag in (report or {}).get("flags", []):
        for name in flag.get("fields", []):
            flagged.setdefault(name, []).append(flag)
    rows = []
    for group, names in FIELD_GROUPS.items():
        for name, label in names:
            f = fields.get(name) or {}
            cites = []
            for role in ("stated_in", "defined_in"):
                results = (checked[name].stated if role == "stated_in" else
                           checked[name].defined) if checked and name in checked else []
                for i, c in enumerate(f.get(role) or []):
                    status = results[i].status if i < len(results) else "cited"
                    cites.append({"role": role, "doc": c.get("doc"), "page": c.get("page"),
                                  "quote": c.get("quote"), "status": status,
                                  "found_page": results[i].found_page
                                  if i < len(results) else None})
            on_page = None
            if checked and name in checked:
                on_page = value_in_text(name, values.get(name), checked[name].texts, values)
            spec = FIELD_BY_NAME[name]
            entry = (review or {"fields": {}})["fields"].get(name) or {}
            rows.append({"name": name, "label": label, "group": group,
                         "value": f.get("value"), "note": f.get("note"), "cites": cites,
                         "value_on_page": on_page, "flags": flagged.get(name, []),
                         "bdt": spec.bdt, "kind": spec.kind, "options": list(spec.enum),
                         "review": entry.get("status"), "original": entry.get("original"),
                         "second": None if second is None else
                         {"value": second.get(name), "agrees": name not in differ}})
    return rows


def _second_reading(settings: Settings, run: str, bond_id: str, extraction: dict | None,
                    wanted: str | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The second reading to show beside a model's reading: the one asked for, or by default
    another model's reading of the same bond. The verified terms have none."""
    choices = readings(settings, bond_id) if extraction else {}
    choices.pop(run, None)
    if wanted == "none":
        chosen = None
    elif wanted in choices:
        chosen = wanted
    else:
        chosen = pick_second(choices, extraction["model"] if extraction else None)
    other = choices[chosen] if chosen else None
    return ({"run": chosen, "label": reading_label(other) if other else None,
             "choices": [{"id": r, "label": reading_label(e)} for r, e in choices.items()]},
            other.values() if other else None)


class ReviewAction(BaseModel):
    action: Literal["approve", "correct", "reset"]
    value: Any = None


@dataclass
class Job:
    """One extraction started from the review screen, followed by its verification."""

    id: str
    bond_id: str
    kind: str  # hosted | local
    mode: str  # agent | one_pass
    run: str | None = None  # known once the model is
    events: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False


def _run_job(settings: Settings, job: Job) -> None:
    from tstt.llm import ModelError, make_client
    from tstt.verify import verify

    def emit(kind: str, **data: Any) -> None:
        job.events.append({"type": kind, **data})

    try:
        if job.kind == "hosted" and not settings.model_api_key:
            raise ModelError("MODEL_API_KEY is not set in .env")
        client = make_client(job.kind, settings)
        run = job.run = f"{job.mode}-{client.model}"
        emit("start", model=client.model, mode=job.mode, run=run)
        source = bond_source(settings, job.bond_id)
        if job.mode == "agent":
            extraction = agent(client, source, on_step=lambda e: emit("step", **e))
        else:
            extraction = one_pass(client, source)
        extraction.save(settings.runs_dir / "extractions" / run)
        discard(settings, run, job.bond_id)
        emit("saved", run=run)
        if extraction.error:
            raise ModelError(extraction.error)
        emit("extracted", seconds=round(extraction.seconds, 1), steps=extraction.steps,
             input_tokens=extraction.usage.input_tokens, cost_usd=extraction.cost_usd)
        terms = build_terms(extraction.values())
        documents = [doc for doc, _ in source.documents]
        checked = check_fields(extraction.fields, Citations(documents))
        report = verify(job.bond_id, terms, f"{job.mode}:{client.model}", extraction.fields,
                        checked)
        report.save(settings.runs_dir / "reports" / run / f"{job.bond_id}.json")
        emit("done", run=run, summary=report.summary())
    except (ModelError, TermsError, ValueError) as exc:
        emit("error", message=str(exc)[:500])
    except Exception as exc:  # the page must hear that the job ended
        emit("error", message=f"{type(exc).__name__}: {exc}"[:500])
        raise
    finally:
        job.done = True


def create_app(settings: Settings | None = None,
               hosts: frozenset[str] = LOCAL_HOSTS) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="Term Sheet to Token")
    jobs: dict[str, Job] = {}
    review_lock = threading.Lock()

    @app.middleware("http")
    async def same_machine_only(request: Request, call_next):
        """Answer only requests addressed to this machine, and accept a POST only from this
        server's own page: a page on another site must not start an extraction, which spends
        API credit, or read the documents through a rebound DNS name."""
        host = request.headers.get("host", "")
        if host.rsplit(":", 1)[0] not in hosts:
            return Response("unknown host", status_code=403)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD") and origin not in (None, f"http://{host}"):
            return Response("cross-site request", status_code=403)
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            # The browser asks again each time, so a restarted server never runs an old script.
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/api/runs")
    def runs() -> list[dict[str, Any]]:
        return _runs(settings)

    @app.get("/api/bonds")
    def bonds() -> list[dict[str, Any]]:
        manifest = load_manifest()
        out = []
        for bond in manifest["bonds"]:
            if not (GROUND_TRUTH / f"{bond['id']}.json").exists():
                continue
            reports = {}
            for run in _runs(settings):
                report = _read_json(settings.runs_dir / "reports" / run["id"]
                                    / f"{bond['id']}.json")
                if report:
                    reports[run["id"]] = report["summary"]
            truth = _read_json(GROUND_TRUTH / f"{bond['id']}.json")
            out.append({"id": bond["id"], "label": bond["label"],
                        "isin": bond.get("isin"), "cusip": bond.get("cusip"),
                        "dlt_native": bond.get("dlt_native", False), "reports": reports,
                        "verified": truth.get("status") == "verified"})
        return out

    @app.get("/api/documents/{bond_id}")
    def documents(bond_id: str) -> list[dict[str, Any]]:
        manifest = load_manifest()
        out = []
        for d in _known_bond(bond_id)["documents"]:
            meta = manifest["documents"][d["doc"]]
            out.append({"doc": d["doc"], "role": d["role"], "title": meta["title"],
                        "format": meta["format"], "url": meta["url"],
                        "pages": len(load_document(settings, d["doc"]).pages)})
        return out

    @app.get("/api/bond/{run}/{bond_id}")
    def bond(run: str, bond_id: str, second: str | None = None) -> dict[str, Any]:
        entry = _known_bond(bond_id)
        _known_run(settings, run)
        fields, extraction, review = _reviewed(settings, run, bond_id)
        chosen, other = _second_reading(settings, run, bond_id, extraction, second)
        report = _read_json(_report_path(settings, run, bond_id, review))
        scored = settings.runs_dir / "eval" / f"{run}.json"
        read = settings.runs_dir / "extractions" / run / f"{bond_id}.json"
        # A score older than the extraction belongs to an earlier reading.
        stale = (scored.exists() and read.exists()
                 and read.stat().st_mtime > scored.stat().st_mtime)
        scores = None if stale else _read_json(scored)
        score = next((b for b in (scores or {}).get("bonds", [])
                      if b["bond_id"] == bond_id), None)
        return {"bond": {k: entry.get(k) for k in ("id", "label", "isin", "cusip",
                                                    "dlt_native")},
                "run": run, "documents": documents(bond_id), "extraction": extraction,
                "fields": _field_rows(settings, bond_id, fields, report, extraction is not None,
                                      review, other),
                "second": chosen,
                "report": report, "score": score, "review": _review_summary(review)}

    def run_verify(run: str, bond_id: str) -> tuple[dict[str, Any], dict]:
        """Deploy and replay the bond from the reviewed terms and save the report."""
        from tstt.verify import verify

        fields, extraction, review = _reviewed(settings, run, bond_id)
        try:
            terms = build_terms({n: f.get("value") for n, f in fields.items()})
        except TermsError as exc:
            raise HTTPException(422, f"cannot build a schedule: {exc}") from exc
        if extraction is None:
            report = verify(bond_id, terms, "ground truth")
        else:
            documents = [doc for doc, _ in bond_source(settings, bond_id).documents]
            checked = check_fields(fields, Citations(documents))
            source = f"{extraction['mode']}:{extraction['model']}"
            report = verify(bond_id, terms, source + (", reviewed" if corrected(review) else ""),
                            fields, checked)
        report.save(_report_path(settings, run, bond_id, review))
        return report.summary(), review

    def not_being_read(run: str, bond_id: str) -> None:
        """Refuse to review or deploy a model's reading of a bond while that model reads the
        bond again: the page still shows the earlier reading, which the new one replaces."""
        if any(job.bond_id == bond_id and not job.done and job.run in (None, run)
               for job in jobs.values()):
            raise HTTPException(409, "this bond is being read again; wait for the new reading")

    @app.post("/api/verify/{run}/{bond_id}")
    def verify_now(run: str, bond_id: str) -> dict[str, Any]:
        _known_bond(bond_id)
        _known_run(settings, run)
        not_being_read(run, bond_id)
        return run_verify(run, bond_id)[0]

    @app.post("/api/review/{run}/{bond_id}/{name}")
    def review_field(run: str, bond_id: str, name: str, body: ReviewAction) -> dict[str, Any]:
        _known_bond(bond_id)
        _known_run(settings, run)
        if name not in FIELD_BY_NAME:
            raise HTTPException(404, f"unknown field {name}")
        not_being_read(run, bond_id)
        with review_lock:
            fields, _ = _source_fields(settings, run, bond_id)
            try:
                review = record(settings, run, bond_id, name, body.action,
                                (fields.get(name) or {}).get("value"), body.value)
            except TermsError as exc:
                raise HTTPException(422, str(exc)) from exc
        return _review_summary(review)

    @app.post("/api/issue/{run}/{bond_id}")
    def issue(run: str, bond_id: str) -> dict[str, Any]:
        """Issue the token once every term is reviewed and the replay raises no red flag."""
        _known_bond(bond_id)
        _known_run(settings, run)
        not_being_read(run, bond_id)
        with review_lock:
            missing = unreviewed(load_review(settings, run, bond_id))
            if missing:
                raise HTTPException(409, f"not reviewed yet: {', '.join(missing)}")
            summary, _ = run_verify(run, bond_id)
            passed, total = summary["probes"].split("/")
            ok = summary["payments_ok"] and not summary["errors"] and passed == total
            if ok:
                mark_issued(settings, run, bond_id, summary)
        return {"issued": ok, "summary": summary}

    @app.get("/api/view/{doc_id}/{number}")
    def view(doc_id: str, number: int,
             q: Annotated[list[str] | None, Query()] = None) -> dict[str, Any]:
        try:
            return page_view(settings, _known_doc(doc_id), number, q or [])
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/page/{doc_id}/{number}.png")
    def page(doc_id: str, number: int) -> FileResponse:
        try:
            return FileResponse(page_image(settings, _known_doc(doc_id), number),
                                media_type="image/png")
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/search/{doc_id}")
    def search(doc_id: str, q: str) -> list[int]:
        return search_pages(settings, _known_doc(doc_id), q)

    @app.get("/api/results")
    def results() -> JSONResponse:
        folder = settings.runs_dir
        return JSONResponse({
            "eval": [_read_json(p)["summary"] for p in sorted((folder / "eval").glob("*.json"))],
            "terms": (_read_json(folder / "mutation" / "terms.json") or {}).get("summary"),
            "contracts": (_read_json(folder / "mutation" / "contracts.json")
                          or {}).get("mutants"),
        })

    @app.post("/api/extract/{kind}/{bond_id}")
    def extract(kind: str, bond_id: str, mode: str = "agent") -> dict[str, str]:
        if kind not in ("hosted", "local") or mode not in ("agent", "one_pass"):
            raise HTTPException(400, "kind is hosted or local, mode is agent or one_pass")
        if any(not j.done for j in jobs.values()):
            raise HTTPException(409, "another extraction is running")
        _known_bond(bond_id)
        job = Job(uuid.uuid4().hex[:12], bond_id, kind, mode)
        jobs[job.id] = job
        threading.Thread(target=_run_job, args=(settings, job), daemon=True).start()
        return {"job": job.id}

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str) -> StreamingResponse:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "unknown job")

        async def stream():
            sent = 0
            while True:
                while sent < len(job.events):
                    yield f"data: {json.dumps(job.events[sent], default=str)}\n\n"
                    sent += 1
                if job.done and sent == len(job.events):
                    return
                await asyncio.sleep(0.3)

        return StreamingResponse(stream(), media_type="text/event-stream")

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
