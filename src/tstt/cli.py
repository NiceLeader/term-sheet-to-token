"""Command line entry point: `tstt <command>`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tstt.config import REPO_ROOT, load_settings

GROUND_TRUTH = REPO_ROOT / "eval" / "groundtruth"


def ground_truth_bonds(held_out: bool = False) -> list[str]:
    """Bonds with verified terms: those labelled after the field rules were frozen when
    `held_out` is set, otherwise the rest."""
    from tstt.fetch import load_manifest

    flags = {bond["id"]: bool(bond.get("held_out")) for bond in load_manifest()["bonds"]}
    return [path.stem for path in sorted(GROUND_TRUTH.glob("*.json"))
            if flags.get(path.stem, False) is held_out]


def run_folder_name(mode: str, model: str, held_out: bool = False) -> str:
    return f"{mode}-{model}" + ("-unseen" if held_out else "")


def _cmd_fetch(args: argparse.Namespace) -> int:
    from tstt.fetch import fetch_all

    problems = fetch_all(load_settings(), only=set(args.only) if args.only else None,
                         record=args.record)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


def _cmd_parse(args: argparse.Namespace) -> int:
    from tstt.docs import cache_path, load_document
    from tstt.fetch import load_manifest

    settings = load_settings()
    doc_ids = args.only or list(load_manifest()["documents"])
    for doc_id in doc_ids:
        if args.force:
            cache_path(settings, doc_id).unlink(missing_ok=True)
        document = load_document(settings, doc_id)
        print(f"{doc_id:40s} pages={len(document.pages):4d} chars={document.char_count:8d}")
    return 0


def _print_report(report) -> None:
    print(f"{report.bond_id} ({report.source}), {report.currency}")
    print(f"{'#':>2} {'kind':10} {'scheduled':10} {'record':10} {'engine':>14} "
          f"{'QuantLib':>14} {'token':>14}  printed")
    for r in report.rows:
        print(f"{r.index:>2} {r.kind:10} {r.scheduled:10} {r.record_date:10} {r.engine:>14} "
              f"{r.quantlib or '-':>14} {r.chain:>14}  {r.stated or ''}"
              + ("" if r.status == "ok" else f"  MISMATCH: {'; '.join(r.problems)}"))
    for p in report.probes:
        print(f"probe: {p['name']}: {p['actual']} ({'pass' if p['passed'] else 'FAIL'})")
    for f in report.flags:
        print(f"{f.severity}: {f.message}")
    print(json.dumps(report.summary()))


def _cmd_verify(args: argparse.Namespace) -> int:
    from tstt.cite import Citations, check_fields
    from tstt.extract import Extraction, bond_source
    from tstt.terms import TermsError, build_terms
    from tstt.verify import ground_truth_fields, verify

    settings = load_settings()
    bonds = args.bond or [p.stem for p in sorted(GROUND_TRUTH.glob("*.json"))]
    failed = 0
    for bond_id in bonds:
        if args.extraction:
            extraction = Extraction.load(Path(args.extraction) / f"{bond_id}.json")
            fields, source = extraction.fields, f"{extraction.mode}:{extraction.model}"
            documents = [doc for doc, _ in bond_source(settings, bond_id).documents]
            checked = check_fields(fields, Citations(documents))
        else:
            fields = ground_truth_fields(GROUND_TRUTH / f"{bond_id}.json")
            source, checked = "ground truth", None
        try:
            terms = build_terms({name: f.get("value") for name, f in fields.items()})
        except TermsError as exc:
            print(f"{bond_id}: cannot build a schedule: {exc}")
            failed += 1
            continue
        report = verify(bond_id, terms, source, fields if args.extraction else None, checked)
        name = args.extraction and Path(args.extraction).name or "groundtruth"
        report.save(settings.runs_dir / "reports" / name / f"{bond_id}.json")
        _print_report(report) if not args.quiet else print(json.dumps(report.summary()))
        failed += not (report.payments_ok and report.probes_ok)
    return 1 if failed else 0


def _cmd_extract(args: argparse.Namespace) -> int:
    from tstt.extract import agent, bond_source, one_pass
    from tstt.llm import ModelError, SpendLimitReached, make_client
    from tstt.review import RUN_NAME, discard

    if args.run is not None and not RUN_NAME.fullmatch(args.run):
        print(f"run name {args.run!r} must be lower-case letters, digits, '.', '_' or '-'",
              file=sys.stderr)
        return 2
    settings = load_settings()
    client = make_client(args.model, settings)
    if args.effort and hasattr(client, "effort"):
        client.effort = args.effort
    run_name = args.run or run_folder_name(args.mode, client.model, args.held_out)
    folder = settings.runs_dir / "extractions" / run_name
    bonds = args.bond or ground_truth_bonds(args.held_out)
    failed = 0
    for bond_id in bonds:
        source = bond_source(settings, bond_id)
        try:
            run = one_pass if args.mode == "one_pass" else agent
            extraction = run(client, source)
        except SpendLimitReached as exc:
            print(f"{bond_id}: {exc}", file=sys.stderr)
            return 1
        except ModelError as exc:
            print(f"{bond_id}: {exc}", file=sys.stderr)
            failed += 1
            continue
        path = extraction.save(folder)
        discard(settings, folder.name, bond_id)
        if extraction.error:
            print(f"{bond_id}: {extraction.error} -> {path}", file=sys.stderr)
            failed += 1
            continue
        cost = "" if extraction.cost_usd is None else f" ${extraction.cost_usd:.3f}"
        print(f"{bond_id}: {extraction.usage.input_tokens} in, "
              f"{extraction.usage.output_tokens} out, {extraction.seconds:.0f}s{cost}, "
              f"{extraction.steps} steps -> {path}")
    spend = getattr(client, "spend", None)
    if spend:
        limit = "" if spend.limit_usd is None else f" of ${spend.limit_usd:.2f}"
        print(f"hosted spend so far: ${spend.spent():.2f}{limit}")
    return 1 if failed else 0


def _cmd_mutate(args: argparse.Namespace) -> int:
    from tstt.mutate import run_mutants, run_term_errors, save, summarise_terms

    settings = load_settings()
    ground_truth = sorted(GROUND_TRUTH.glob("*.json"))
    folder = settings.runs_dir / "mutation"
    if args.what in ("terms", "all"):
        cases = run_term_errors(settings, ground_truth)
        rows = summarise_terms(cases)
        for r in rows:
            print(f"{r['error']:34} with effect {r['with_effect']:3}  caught {r['caught']:3}"
                  f"  missed {', '.join(r['missed']) or '-'}")
        effective = sum(r["with_effect"] for r in rows)
        caught = sum(r["caught"] for r in rows)
        print(f"term errors: {caught}/{effective} with an economic effect caught, "
              f"{len(cases) - effective} without effect")
        save(folder / "terms.json", {"summary": rows, "cases": cases})
    if args.what in ("contracts", "all"):
        results = run_mutants(settings, ground_truth, workers=args.workers,
                              only=set(args.only) if args.only else None)
        for m in results:
            status = "caught" if m.caught else ("BUILD FAILED" if m.build_error else "SURVIVED")
            first = next(iter(m.reasons.values()), m.build_error or "")
            print(f"{m.name:28} {status:8} {len(m.bonds_caught):2} bonds "
                  f"{len(m.failed_tests):2} tests  {first[:80]}")
        print(f"contract mutants: {sum(m.caught for m in results)}/{len(results)} caught")
        name = "contracts-partial.json" if args.only else "contracts.json"
        save(folder / name, {"mutants": results})
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    from tstt.eval import score_run

    settings = load_settings()
    folders = ([Path(f) for f in args.run] if args.run
               else sorted(p for p in (settings.runs_dir / "extractions").iterdir()
                           if p.is_dir()))
    for folder in folders:
        run = score_run(settings, folder, GROUND_TRUTH)
        run.save(settings.runs_dir / "eval" / f"{folder.name}.json")
        for b in run.bonds:
            if b.error:
                print(f"  {b.bond_id:24} no submission: {b.error[:70]}")
                continue
            wrong = ", ".join(b.wrong) or "-"
            print(f"  {b.bond_id:24} fields {b.fields_correct:2}/{b.fields_total}  "
                  f"flows {'match' if b.flows_match else 'DIFFER'}  "
                  f"quotes {b.quotes_on_page}/{b.quotes}  wrong: {wrong}")
        print(json.dumps(run.summary()))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from tstt.web import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tstt")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="download public bond documents from the manifest")
    fetch.add_argument("--only", nargs="*", help="document ids to fetch")
    fetch.add_argument("--record", action="store_true",
                       help="write sha256 and size of first downloads into the manifest")
    fetch.set_defaults(func=_cmd_fetch)

    parse = sub.add_parser("parse", help="split fetched documents into numbered text pages")
    parse.add_argument("--only", nargs="*", help="document ids to parse")
    parse.add_argument("--force", action="store_true", help="ignore the parsed cache")
    parse.set_defaults(func=_cmd_parse)

    check = sub.add_parser("verify", help="deploy each bond as a token and check every payment")
    check.add_argument("bond", nargs="*", help="bond ids (default: all with ground truth)")
    check.add_argument("--extraction", help="folder of extractions to use instead of ground truth")
    check.add_argument("--quiet", action="store_true", help="print one summary line per bond")
    check.set_defaults(func=_cmd_verify)

    extract = sub.add_parser("extract", help="extract bond terms with a Meta model")
    extract.add_argument("bond", nargs="*",
                         help="bond ids (default: all with verified terms but the held-out ones)")
    extract.add_argument("--model", choices=["hosted", "local"], default="hosted",
                         help="hosted Muse Spark or local Muse Glimmer")
    extract.add_argument("--mode", choices=["one_pass", "agent"], default="one_pass")
    extract.add_argument("--effort", help="effort level for the hosted model (low, medium, high)")
    extract.add_argument("--held-out", action="store_true",
                         help="read the bonds labelled after the field rules were frozen "
                              "(default folder: <mode>-<model>-unseen)")
    extract.add_argument("--run", help="name of the run folder: lower-case letters, digits, "
                                       "'.', '_' and '-' (default: <mode>-<model>)")
    extract.set_defaults(func=_cmd_extract)

    mutate = sub.add_parser("mutate", help="inject known errors and count how many are caught")
    mutate.add_argument("what", choices=["terms", "contracts", "all"], nargs="?", default="all")
    mutate.add_argument("--only", nargs="*", help="contract mutant names")
    mutate.add_argument("--workers", type=int, default=6)
    mutate.set_defaults(func=_cmd_mutate)

    evaluate = sub.add_parser("eval", help="score extraction runs against the ground truth")
    evaluate.add_argument("run", nargs="*", help="extraction folders (default: all runs)")
    evaluate.set_defaults(func=_cmd_eval)

    serve = sub.add_parser("serve", help="open the review screen on localhost")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=_cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
