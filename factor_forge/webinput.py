"""Web input: fetch factor briefs from URLs (e.g. fund.eastmoney.com pages).

Extends the pipeline's input paths beyond local .txt files:

    local file / folder          -> read as before
    http(s)://... or file://...  -> fetch, HTML -> plain text, then extract

Only the standard library is used (urllib + html.parser), so no new
dependencies are introduced. The HTML-to-text conversion keeps block-level
structure (title, headings, paragraphs, table rows) so the extractor's
line-based heuristics (formula lines with '=', "其中" sections, direction
sentences) still work on web text.
"""

from __future__ import annotations

import os
import re
import urllib.request
from html.parser import HTMLParser
from typing import Optional, Tuple

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_URL_SCHEMES = ("http://", "https://", "file://")

# tags whose whole subtree is noise for factor-brief extraction
_SKIP_TAGS = {"script", "style", "noscript", "iframe", "svg",
              "nav", "footer", "form", "button", "select", "option"}
# block-level tags that end a text line
_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "table", "tr", "h1",
               "h2", "h3", "h4", "h5", "h6", "section", "article",
               "blockquote", "pre", "dt", "dd", "title"}
# cell separator when joining one table row into a line
_CELL_SEP = " | "


def is_url(path: str) -> bool:
    return any(path.startswith(s) for s in _URL_SCHEMES)


def fetch_url(url: str, timeout: int = 30,
              headers: Optional[dict] = None) -> str:
    """Fetch a URL and return the decoded body text (best-effort charset)."""
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset()
    for enc in filter(None, [charset, "utf-8", "gb18030"]):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_bytes(url: str, timeout: int = 60,
                headers: Optional[dict] = None) -> bytes:
    """Fetch a URL and return the raw body bytes (e.g. for PDF downloads)."""
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class _TextExtractor(HTMLParser):
    """HTML -> line-oriented plain text (title first, then body blocks)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.lines: list = []
        self._buf: list = []
        self._skip_depth = 0
        self._in_title = False
        self._row_cells: Optional[list] = None
        self._cell_buf: Optional[list] = None

    # -- structure ----------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag == "tr":
            self._flush_line()
            self._row_cells = []
            self._cell_buf = None
        elif tag in ("td", "th") and self._row_cells is not None:
            self._flush_cell()
            self._cell_buf = []

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        elif tag in ("td", "th") and self._cell_buf is not None:
            self._flush_cell()
        elif tag == "tr" and self._row_cells is not None:
            self._flush_cell()
            row = [c for c in self._row_cells if c]
            if row:
                self.lines.append(_CELL_SEP.join(row))
            self._row_cells = None
            self._cell_buf = None
        elif tag in _BLOCK_TAGS:
            self._flush_line()

    # -- data ---------------------------------------------------------------
    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        if not data.strip():
            return
        if self._cell_buf is not None:
            self._cell_buf.append(" ".join(data.split()))
        elif self._row_cells is None:
            self._buf.append(" ".join(data.split()))

    # -- helpers ------------------------------------------------------------
    def _flush_cell(self):
        if self._cell_buf is None or self._row_cells is None:
            return
        text = " ".join(self._cell_buf).strip()
        if text:
            self._row_cells.append(text)
        self._cell_buf = None

    def _flush_line(self):
        text = " ".join(self._buf).strip()
        self._buf = []
        if text:
            self.lines.append(text)

    def get_text(self) -> str:
        self._flush_line()
        parts = []
        title = " ".join(self.title.split())
        if title:
            parts.append(title)
        parts.extend(self.lines)
        return "\n".join(parts)


def html_to_text(html: str) -> str:
    """Convert an HTML document to line-oriented plain text."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - lenient on malformed HTML
        pass
    text = parser.get_text()
    # collapse repeated blank-ish lines and obvious boilerplate duplicates
    out, seen = [], set()
    for ln in text.splitlines():
        ln = ln.strip().strip("\ufeff\u200b\u200c\u200d\u00a0").strip()
        if not ln:
            continue
        if ln in seen and len(ln) > 10:
            continue
        seen.add(ln)
        out.append(ln)
    return "\n".join(out)


def url_basename(url: str) -> str:
    """Derive an output basename from a URL: last non-empty path segment."""
    path = re.sub(r"^[a-z]+://", "", url, flags=re.I).split("?")[0].split("#")[0]
    seg = [s for s in path.split("/") if s]
    if not seg:
        return "page"
    name = seg[-1]
    name = re.sub(r"\.(html?|php|aspx?|jsp)$", "", name, flags=re.I)
    name = re.sub(r"[^A-Za-z0-9_\-.]", "_", name)
    return name or "page"


def load_brief_from_path(path: str, timeout: int = 30) -> Tuple[str, str]:
    """Load factor-brief text from a local file or a URL.

    Returns (brief_text, source_label).
    """
    if is_url(path):
        html = fetch_url(path, timeout=timeout)
        return html_to_text(html), path
    with open(path, encoding="utf-8") as f:
        return f.read(), os.path.basename(path)
