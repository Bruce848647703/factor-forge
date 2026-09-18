#!/usr/bin/env python3
"""End-to-end factor forge: research brief -> JSON -> executable code -> validate.

Input paths:
    python scripts/forge.py examples/input/cgo.txt              # text brief
    python scripts/forge.py examples/input/                     # folder
    python scripts/forge.py https://fund.eastmoney.com/006195.html   # web page
    python scripts/forge.py /path/to/report.pdf                 # research PDF

Company mode (signal-library repo, core.py centric):
    python scripts/forge.py examples/input/cgo.txt --company \
        --runner-id 30180 --runner-name Signal_CGO
    -> examples/company/Signal_CGO/{core.py, function.py, main.py, ...}

Fixed sources (consistent, incremental sync of broker report feeds):
    python scripts/forge.py sources list
    python scripts/forge.py sources sync [--source ID] [--company] [--limit N]
    python scripts/forge.py sources status
    registry: sources/sources.json   state: sources/state.json

Data names for company code are resolved via datadict.example.com (fallback:
verified builtin bindings; use --no-dict to stay offline).
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factor_forge import (extract, generate_to_file, validate_code,  # noqa: E402
                          validate_corepy, load_brief_from_path, is_url)
from factor_forge.webinput import url_basename  # noqa: E402
from factor_forge.datadict import DataDictClient, resolve_factor_fields  # noqa: E402
from factor_forge.corepy import generate_repo  # noqa: E402
from factor_forge.pdfinput import pdf_to_briefs, find_factor_briefs  # noqa: E402
from factor_forge.webinput import html_to_text  # noqa: E402
from factor_forge import sources as src  # noqa: E402
from typing import Dict, List  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_REGISTRY = os.path.join(REPO_ROOT, "sources", "sources.json")
DEFAULT_STATE = os.path.join(REPO_ROOT, "sources", "state.json")


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------
def _dict_client(args):
    if args.no_dict:
        return None
    cache_path = args.dict_cache or os.path.join(REPO_ROOT,
                                                 ".datadict_cache.json")
    return DataDictClient(base_url=args.dict_base, scope=args.dict_scope,
                          method=args.dict_method, cache_path=cache_path)


def _slug(title: str, date: str = "") -> str:
    t = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", title).strip("_")[:50]
    return f"{date}_{t}" if date else t


def _emit_factor(factor, name, out_root, args, bindings_client=None):
    """JSON + standalone code + validate (+ company repo). Returns outputs."""
    outputs = []
    json_dir = os.path.join(out_root, "json")
    os.makedirs(json_dir, exist_ok=True)
    json_path = os.path.join(json_dir, f"{name}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        f.write(factor.to_json())
    outputs.append(json_path)

    print(f"  name      : {factor.name}  ({factor.name_cn})")
    print(f"  category  : {factor.category}")
    print(f"  operator  : {factor.operator}")
    print(f"  direction : {factor.direction:+d}  ({factor.direction_note})")
    print(f"  variables : {[(v.symbol, v.data_field) for v in factor.variables]}")
    print(f"  JSON      : {json_path}")
    if args.json_only:
        return outputs, "json_only"

    code_dir = os.path.join(out_root, "code")
    os.makedirs(code_dir, exist_ok=True)
    code_path = os.path.join(code_dir, f"{name}.py")
    generate_to_file(factor, code_path)
    outputs.append(code_path)
    print(f"  code      : {code_path}")
    ok, msg, _out = validate_code(code_path, factor)
    print(f"  validate  : {'PASS' if ok else 'WARN/FAIL'} — {msg}")

    if args.company:
        runner_name = args.runner_name or factor.name
        if not runner_name.startswith("Signal_"):
            runner_name = f"Signal_{runner_name}"
        bindings = None
        if bindings_client is not None:
            bindings = resolve_factor_fields(factor, client=bindings_client)
            for fld, b in bindings.items():
                where = (f"{b.table}.{b.column}" if b.column
                         else f"{b.table}: {b.expr}")
                print(f"    dict {fld:<12} -> {where}  [{b.origin}]")
        repo_dir = os.path.join(out_root, "company", runner_name)
        written = generate_repo(factor, repo_dir, runner_id=args.runner_id,
                                runner_name=runner_name, bindings=bindings,
                                group=args.group)
        outputs.append(repo_dir)
        print(f"  repo      : {repo_dir}")
        core_ok, core_msg, _sig = validate_corepy(written["core.py"], factor,
                                                  runner_id=args.runner_id)
        print(f"  core.py   : {'PASS' if core_ok else 'WARN/FAIL'} — {core_msg}")
    return outputs, "ok"


# ---------------------------------------------------------------------------
# single-input modes: txt / url / pdf
# ---------------------------------------------------------------------------
def process(path: str, out_root: str, args) -> None:
    if is_url(path):
        brief, source = load_brief_from_path(path, timeout=args.timeout)
        name = args.name or url_basename(path)
    elif path.lower().endswith(".pdf"):
        process_pdf(path, out_root, args)
        return
    else:
        name = args.name or os.path.splitext(os.path.basename(path))[0]
        with open(path, encoding="utf-8") as f:
            brief = f.read()
        source = os.path.basename(path)

    print(f"\n=== {name} ===")
    print(f"  source    : {source}")
    factor = extract(brief, source=source)
    _emit_factor(factor, name, out_root, args, _dict_client(args))


def process_pdf(path: str, out_root: str, args) -> None:
    name = args.name or os.path.splitext(os.path.basename(path))[0]
    print(f"\n=== {name} (pdf) ===")
    print(f"  source    : {path}")
    candidates = pdf_to_briefs(path, top_k=args.top_briefs)
    if not candidates:
        print("  brief     : no factor-definition section detected")
        return
    for i, cand in enumerate(candidates):
        print(f"  brief[{i}] : score {cand.score:.1f} — {cand.anchor[:50]}")
    best = candidates[0]
    factor = extract(best.text, source=os.path.basename(path))
    _emit_factor(factor, name, out_root, args, _dict_client(args))


def collect_files(path: str):
    if os.path.isdir(path):
        return sorted(os.path.join(path, f) for f in os.listdir(path)
                      if f.endswith((".txt", ".pdf")))
    return [path]


# ---------------------------------------------------------------------------
# fixed sources sync
# ---------------------------------------------------------------------------
def _print_registry(registry: List[src.Source]) -> None:
    for s in registry:
        mark = "on " if s.enabled else "off"
        print(f"  [{mark}] {s.id:<24} {s.kind:<10} {s.name}")
        if s.notes:
            print(f"        {s.notes}")


def _sync_one(source: src.Source, state: Dict, args) -> None:
    print(f"\n=== source: {source.id} ({source.name}) ===")
    try:
        entries = src.fetch_entries(source)
    except Exception as e:  # noqa: BLE001
        print(f"  fetch ERROR: {type(e).__name__}: {e}")
        return
    print(f"  fetched   : {len(entries)} entries")

    state_path = args.state
    cache_dir = os.path.join(os.path.dirname(state_path), "cache")
    out_root = os.path.join(args.out, "from_sources", source.id)
    dict_client = _dict_client(args)

    processed = 0
    for entry in entries:
        if args.limit and processed >= args.limit:
            print(f"  (--limit {args.limit} reached)")
            break
        if src.is_processed(state, source.id, entry.entry_id) and not args.force:
            continue
        processed += 1
        label = f"{entry.date} {entry.org} {entry.title[:40]}"
        print(f"\n  -- {label}")
        slug = _slug(entry.title, entry.date)
        try:
            doc_path = src.download_entry(entry, cache_dir,
                                          timeout=args.timeout)
        except Exception as e:  # noqa: BLE001
            print(f"     download ERROR: {type(e).__name__}: {e}")
            src.mark_processed(state, source.id, entry, "download_error",
                               note=str(e))
            continue

        try:
            if doc_path.lower().endswith(".pdf"):
                candidates = pdf_to_briefs(doc_path, top_k=args.top_briefs)
            else:
                with open(doc_path, encoding="utf-8") as f:
                    candidates = find_factor_briefs(html_to_text(f.read()),
                                                    top_k=args.top_briefs)
        except Exception as e:  # noqa: BLE001
            print(f"     parse ERROR: {type(e).__name__}: {e}")
            src.mark_processed(state, source.id, entry, "parse_error",
                               note=str(e))
            continue

        if not candidates:
            print("     no factor section found (recorded)")
            src.mark_processed(state, source.id, entry, "no_factor")
            continue

        best = candidates[0]
        print(f"     brief score {best.score:.1f} — {best.anchor[:50]}")
        factor = extract(best.text,
                         source=f"{source.id}/{entry.entry_id}")
        entry_out = os.path.join(out_root, slug)
        os.makedirs(entry_out, exist_ok=True)
        with open(os.path.join(entry_out, "brief.txt"), "w",
                  encoding="utf-8") as f:
            f.write(best.text)
        with open(os.path.join(entry_out, "entry.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"entry_id": entry.entry_id, "title": entry.title,
                       "date": entry.date, "org": entry.org,
                       "url": entry.url, "pdf_url": entry.pdf_url,
                       "meta": entry.meta}, f, ensure_ascii=False, indent=2)

        sub_args = argparse.Namespace(**vars(args))
        sub_args.runner_name = None            # derive per factor
        outputs, _ = _emit_factor(factor, factor.name or slug,
                                  entry_out, sub_args, dict_client)
        src.mark_processed(state, source.id, entry, "factor",
                           factor_name=factor.name, outputs=outputs)
        src.save_state(state_path, state)
    src.save_state(state_path, state)


def _write_digest(state: Dict, args) -> None:
    rows = src.digest_rows(state)
    out_dir = os.path.join(args.out, "from_sources")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "digest.json"), "w",
              encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    lines = ["# 来源同步摘要", "",
             "| 来源 | 日期 | 机构 | 报告 | 状态 | 因子 |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['source']} | {r.get('date','')} | {r.get('org','')} "
                     f"| {r.get('title','')[:40]} | {r.get('status','')} "
                     f"| {r.get('factor','')} |")
    with open(os.path.join(out_dir, "digest.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n  digest    : {os.path.join(out_dir, 'digest.md')}")


def cmd_sources(args) -> int:
    registry = src.load_registry(args.registry)
    action = args.sources_args[0] if args.sources_args else "list"

    if action == "list":
        _print_registry(registry)
        return 0

    state = src.load_state(args.state)
    if action == "status":
        rows = src.digest_rows(state, args.source)
        if not rows:
            print("  (no entries processed yet)")
        for r in rows:
            print(f"  {r['source']:<22} {r.get('date',''):<11} "
                  f"{r.get('status',''):<14} {r.get('factor',''):<8} "
                  f"{r.get('title','')[:44]}")
        return 0

    if action != "sync":
        print(f"unknown sources action: {action}")
        return 2

    selected = [s for s in registry if s.enabled
                and (not args.source or s.id == args.source)]
    if args.source and not selected:
        print(f"no enabled source with id {args.source!r}")
        return 2
    for source in selected:
        _sync_one(source, state, args)
    _write_digest(state, args)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*",
                    help="brief file(s)/folder, URL(s), PDF(s), "
                         "or 'sources <list|sync|status>'")
    ap.add_argument("--json-only", action="store_true", help="stop after JSON")
    ap.add_argument("--out", default=os.path.join(REPO_ROOT, "examples"))
    ap.add_argument("--name", help="output basename (default: input name)")
    ap.add_argument("--timeout", type=int, default=120, help="fetch timeout")
    ap.add_argument("--top-briefs", type=int, default=3,
                    help="max factor-brief candidates per PDF/HTML")
    # company repo options
    ap.add_argument("--company", action="store_true",
                    help="also generate the company signal-library repo")
    ap.add_argument("--runner-id", type=int, default=0,
                    help="runner_id for SIGNAL_FILE / manifest.json")
    ap.add_argument("--runner-name", help="e.g. Signal_CGO")
    ap.add_argument("--group", default="momentum_factor",
                    help="signal library group for manifest git_repo")
    # data dictionary options
    ap.add_argument("--no-dict", action="store_true",
                    help="skip datadict.example.com, use builtin bindings")
    ap.add_argument("--dict-base", default="http://datadict.example.com")
    ap.add_argument("--dict-scope", default="unified_all")
    ap.add_argument("--dict-method", default="ensemble")
    ap.add_argument("--dict-cache", help="dictionary response cache file")
    # fixed sources options
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--source", help="sync only this source id")
    ap.add_argument("--limit", type=int, default=0,
                    help="max new entries to process per source")
    ap.add_argument("--force", action="store_true",
                    help="re-process entries already in state")
    args = ap.parse_args()

    if args.paths and args.paths[0] == "sources":
        args.sources_args = args.paths[1:]
        return cmd_sources(args)

    if not args.paths:
        ap.error("missing input path (or 'sources <list|sync|status>')")

    for path in args.paths:
        files = [path] if is_url(path) else collect_files(path)
        for fp in files:
            try:
                process(fp, args.out, args)
            except Exception as e:  # noqa: BLE001
                print(f"\n=== {fp} ===\n  ERROR: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
