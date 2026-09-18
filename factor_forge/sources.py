"""Fixed research-report sources: registry, fetchers and incremental state.

Factor research arrives from a *fixed set of sources* — e.g. a broker's
report center that keeps publishing factor PDFs. This module models those
sources declaratively so the whole pipeline (fetch -> parse -> extract ->
core.py) runs consistently and idempotently:

    sources.json (registry)
        │  forge.py sources sync
        ▼
    fetch_entries(source)          # per-kind fetcher (eastmoney API, local
        │                          # folder of PDFs, static PDF URLs)
    download_entry(entry, cache)   # PDF/HTML into the cache dir
        │
    pdfinput / webinput            # -> factor brief text
        │
    extract -> codegen/corepy      # the existing pipeline
        │
    state.json                     # processed entries, never re-processed

Registry format (JSON):

    {"sources": [
      {"id": "eastmoney_industry", "name": "...", "kind": "eastmoney",
       "params": {"endpoint": "list", "qtype": 1, "days": 14,
                  "keywords": ["因子", "量化", ...]},
       "enabled": true, "group": "momentum_factor", "notes": "..."}
    ]}
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .webinput import fetch_url, fetch_bytes

VALID_KINDS = ("eastmoney", "folder", "pdf_urls")

EASTMONEY_LIST_API = "https://reportapi.eastmoney.com/report/list"
EASTMONEY_JG_API = "https://reportapi.eastmoney.com/report/jg"
EASTMONEY_PDF = "https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf"
EASTMONEY_HEADERS = {"Referer": "https://data.eastmoney.com/"}


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------
@dataclass
class Source:
    id: str
    name: str
    kind: str
    params: Dict = field(default_factory=dict)
    enabled: bool = True
    group: str = ""          # signal-library group hint for company mode
    notes: str = ""

    def validate(self) -> List[str]:
        errs = []
        if not self.id or not re.fullmatch(r"[a-z0-9_]+", self.id):
            errs.append(f"{self.id!r}: id must match [a-z0-9_]+")
        if self.kind not in VALID_KINDS:
            errs.append(f"{self.id}: unknown kind {self.kind!r} "
                        f"(valid: {VALID_KINDS})")
        if self.kind == "folder" and not self.params.get("path"):
            errs.append(f"{self.id}: folder source needs params.path")
        if self.kind == "pdf_urls" and not self.params.get("urls"):
            errs.append(f"{self.id}: pdf_urls source needs params.urls")
        return errs


@dataclass
class ReportEntry:
    entry_id: str
    source_id: str
    title: str
    date: str                # YYYY-MM-DD
    org: str
    url: str                 # detail page (or local path for folder sources)
    pdf_url: str = ""        # direct PDF when known
    meta: Dict = field(default_factory=dict)


def load_registry(path: str) -> List[Source]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    registry_dir = os.path.dirname(os.path.abspath(path))
    sources = [Source(id=s.get("id", ""), name=s.get("name", s.get("id", "")),
                      kind=s.get("kind", ""), params=s.get("params") or {},
                      enabled=bool(s.get("enabled", True)),
                      group=s.get("group", ""), notes=s.get("notes", ""))
               for s in raw.get("sources", [])]
    # resolve relative folder-source paths against the registry location
    for s in sources:
        if s.kind == "folder":
            p = s.params.get("path", "")
            if p and not os.path.isabs(p):
                s.params["path"] = os.path.normpath(
                    os.path.join(registry_dir, p))
    errs = [e for s in sources for e in s.validate()]
    ids = [s.id for s in sources]
    if len(ids) != len(set(ids)):
        errs.append("duplicate source ids in registry")
    if errs:
        raise ValueError("invalid source registry: " + "; ".join(errs))
    return sources


# ---------------------------------------------------------------------------
# fetchers (one per kind)
# ---------------------------------------------------------------------------
def _date_range(days: int, today: Optional[dt.date] = None):
    end = today or dt.date.today()
    begin = end - dt.timedelta(days=days)
    return begin.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _fetch_eastmoney(source: Source, today: Optional[dt.date] = None,
                     fetch_json: Optional[Callable] = None
                     ) -> List[ReportEntry]:
    p = source.params
    endpoint = p.get("endpoint", "list")
    days = int(p.get("days", 14))
    page_size = int(p.get("page_size", 50))
    pages = int(p.get("pages", 1))
    qtype = p.get("qtype", 1)
    keywords = p.get("keywords") or []
    begin, end = _date_range(days, today)

    fetch_json = fetch_json or _default_fetch_json
    entries: List[ReportEntry] = []
    seen = set()
    for page_no in range(1, pages + 1):
        if endpoint == "jg":
            url = (f"{EASTMONEY_JG_API}?pageSize={page_size}"
                   f"&beginTime={begin}&endTime={end}&pageNo={page_no}"
                   f"&fields=&qType={qtype}")
        else:
            industry = p.get("industry_code", "*")
            url = (f"{EASTMONEY_LIST_API}?industryCode={industry}"
                   f"&pageSize={page_size}&industry=*&rating=&ratingChange="
                   f"&beginTime={begin}&endTime={end}&pageNo={page_no}"
                   f"&fields=&qType={qtype}")
        try:
            data = fetch_json(url)
        except (OSError, ValueError) as e:
            print(f"  [warn] fetch page {page_no} failed: "
                  f"{type(e).__name__}: {e}")
            break
        items = data.get("data") or []
        if not items:
            break
        for item in items:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            if endpoint == "jg":
                # jg items carry a numeric id + encodeUrl; the direct PDF url
                # is only exposed on the detail page (resolved at download)
                entry_id = str(item.get("id") or "")
                pdf_url = ""
                encode_url = item.get("encodeUrl") or ""
            else:
                entry_id = item.get("infoCode") or ""
                pdf_url = EASTMONEY_PDF.format(info_code=entry_id)
                encode_url = item.get("encodeUrl") or ""
            if not entry_id or entry_id in seen:
                continue
            seen.add(entry_id)
            if keywords and not any(k in title for k in keywords):
                continue
            entries.append(ReportEntry(
                entry_id=entry_id, source_id=source.id, title=title,
                date=(item.get("publishDate") or "")[:10],
                org=item.get("orgSName") or "",
                url=(f"https://data.eastmoney.com/report/zw/"
                     f"{'industry' if endpoint == 'list' else 'strategy'}"
                     f".jshtml?{'infocode' if endpoint == 'list' else 'encodeUrl'}"
                     f"={entry_id if endpoint == 'list' else encode_url}"),
                pdf_url=pdf_url,
                meta={"qtype": qtype, "endpoint": endpoint,
                      "encodeUrl": encode_url,
                      "industry": item.get("industryName", "")}))
    return entries


def _default_fetch_json(url: str) -> Dict:
    text = fetch_url(url, timeout=30, headers=EASTMONEY_HEADERS)
    return json.loads(text)


def _fetch_folder(source: Source) -> List[ReportEntry]:
    p = source.params
    root = p["path"]
    pattern = p.get("pattern", "*.pdf")
    if not os.path.isdir(root):
        return []
    import fnmatch
    entries: List[ReportEntry] = []
    for name in sorted(os.listdir(root)):
        if not fnmatch.fnmatch(name.lower(), pattern.lower()):
            continue
        path = os.path.join(root, name)
        st = os.stat(path)
        digest = hashlib.sha1(
            f"{path}|{int(st.st_mtime)}|{st.st_size}".encode()).hexdigest()
        entries.append(ReportEntry(
            entry_id=digest[:16], source_id=source.id,
            title=os.path.splitext(name)[0],
            date=dt.date.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d"),
            org=p.get("org", ""), url=path, pdf_url=path,
            meta={"file": name}))
    return entries


def _fetch_pdf_urls(source: Source) -> List[ReportEntry]:
    entries: List[ReportEntry] = []
    for item in source.params.get("urls") or []:
        if isinstance(item, str):
            item = {"url": item}
        url = item["url"]
        digest = hashlib.sha1(url.encode()).hexdigest()[:16]
        entries.append(ReportEntry(
            entry_id=item.get("id") or digest, source_id=source.id,
            title=item.get("title") or os.path.basename(url.split("?")[0]),
            date=item.get("date", ""), org=item.get("org", ""),
            url=url, pdf_url=url, meta={}))
    return entries


def fetch_entries(source: Source, today: Optional[dt.date] = None,
                  fetch_json: Optional[Callable] = None) -> List[ReportEntry]:
    if source.kind == "eastmoney":
        return _fetch_eastmoney(source, today=today, fetch_json=fetch_json)
    if source.kind == "folder":
        return _fetch_folder(source)
    if source.kind == "pdf_urls":
        return _fetch_pdf_urls(source)
    raise ValueError(f"unsupported source kind: {source.kind}")


def resolve_eastmoney_pdf_url(encode_url: str, endpoint: str = "strategy"
                              ) -> str:
    """Resolve the direct PDF url from an eastmoney report detail page.

    The jg (strategy) list API only exposes ``encodeUrl``; the detail page
    embeds the real ``pdf.dfcfw.com/pdf/H3_<infoCode>_1.pdf`` link.
    """
    page = "zw_strategy.jshtml" if endpoint == "jg" else "zw_industry.jshtml"
    import urllib.parse
    url = (f"https://data.eastmoney.com/report/{page}"
           f"?encodeUrl={urllib.parse.quote(encode_url, safe='')}")
    html = fetch_url(url, timeout=30, headers=EASTMONEY_HEADERS)
    m = re.search(r"pdf\.dfcfw\.com/pdf/(H[0-9]_[A-Za-z0-9]+_1\.pdf)", html)
    if not m:
        return ""
    return f"https://pdf.dfcfw.com/pdf/{m.group(1)}"


def download_entry(entry: ReportEntry, cache_dir: str,
                   timeout: int = 120) -> str:
    """Materialize the entry document (PDF) into cache_dir; returns path."""
    os.makedirs(cache_dir, exist_ok=True)
    if entry.pdf_url and not entry.pdf_url.startswith(("http://", "https://")):
        return entry.pdf_url                       # local file (folder source)
    if not entry.pdf_url and entry.meta.get("encodeUrl"):
        resolved = resolve_eastmoney_pdf_url(
            entry.meta["encodeUrl"], entry.meta.get("endpoint", "jg"))
        if resolved:
            entry.pdf_url = resolved
    if not entry.pdf_url:
        raise ValueError(f"entry {entry.entry_id} has no downloadable url")
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", entry.title)[:60] or "report"
    ext = ".pdf" if ".pdf" in entry.pdf_url.split("?")[0].lower() else ".html"
    path = os.path.join(cache_dir, f"{entry.entry_id}_{safe}{ext}")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    body = fetch_bytes(entry.pdf_url, timeout=timeout,
                       headers=EASTMONEY_HEADERS)
    if not body:
        raise IOError(f"empty response for {entry.pdf_url} "
                      f"(CDN may be region-blocked)")
    if ext == ".pdf" and body[:4] != b"%PDF":
        raise IOError(f"response is not a PDF for {entry.pdf_url}")
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(body)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# incremental state
# ---------------------------------------------------------------------------
def load_state(path: str) -> Dict:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {"version": 1, "sources": {}}


def save_state(path: str, state: Dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def is_processed(state: Dict, source_id: str, entry_id: str) -> bool:
    return entry_id in state.get("sources", {}).get(source_id, {})


def mark_processed(state: Dict, source_id: str, entry: ReportEntry,
                   status: str, factor_name: str = "",
                   outputs: Optional[List[str]] = None, note: str = "") -> None:
    state.setdefault("sources", {}).setdefault(source_id, {})[entry.entry_id] = {
        "title": entry.title, "date": entry.date, "org": entry.org,
        "status": status, "factor": factor_name,
        "outputs": outputs or [], "note": note,
        "processed_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def digest_rows(state: Dict, source_id: Optional[str] = None) -> List[Dict]:
    rows = []
    for sid, entries in sorted(state.get("sources", {}).items()):
        if source_id and sid != source_id:
            continue
        for eid, rec in sorted(entries.items(),
                               key=lambda kv: kv[1].get("date", "")):
            rows.append({"source": sid, "entry_id": eid, **rec})
    return rows
