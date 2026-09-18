"""Company data dictionary (datadict.example.com) client and field bindings.

The extractor maps report prose ("换手率", "收盘价", ...) onto canonical local
data fields (close/turnover/...). Before generating company code those fields
must be bound to *company* data names. The authoritative source for company
data names is the RAG data dictionary at datadict.example.com; this module queries
it and parses the returned field tables into bindings.

Offline fallback: BUILTIN_BINDINGS below were themselves verified against the
dictionary (LegacyDB DailyQuote 日行情表) and are used when the dictionary
is unreachable or disabled.

API contract (verified):
    POST {base_url}/query
    body: {"query": str, "scope": str, "method": str, "k": int}
      scope : merged | unified_all | wind | xy_jy | tl | highfreq | dfcf |
              yc | zyyx | xy | math | sxz | ricequant | tushare | kuanrui
      method: bm25 | vector | ensemble
    resp : {"metadata": {...}, "results": [
              {"pdf_name", "source_type", "table_content"(HTML), ...}]}
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field, asdict
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional

from .schema import OPERATOR_FIELDS

DEFAULT_BASE_URL = "http://datadict.example.com"
DEFAULT_SCOPE = "unified_all"
DEFAULT_METHOD = "ensemble"

# daily-quote table the signal library's core.py reads from
PREFERRED_TABLE = "DailyQuote"


@dataclass
class DictBinding:
    """Company data name bound to one canonical factor data field."""
    data_field: str              # canonical local field, e.g. "turnover"
    cn_name: str                 # Chinese name, e.g. "换手率"
    table: str                   # company table, e.g. "DailyQuote"
    column: str = ""             # company column ("" when expr is used)
    expr: str = ""               # derived expression, e.g. "A / NULLIF(B, 0)"
    columns: List[str] = field(default_factory=list)  # raw columns consumed
    data_type: str = ""
    origin: str = "builtin"      # builtin | dict
    evidence: str = ""           # dictionary hit the binding came from

    @property
    def select_expr(self) -> str:
        """SQL expression usable in a SELECT list (alias = data_field)."""
        if self.expr:
            return f"{self.expr} AS {self.data_field}"
        return self.column

    def to_dict(self) -> Dict:
        return asdict(self)


# Canonical field -> verified company binding (LegacyDB.dbo.DailyQuote).
# 换手率/成交均价 follow company convention: derived from raw columns, as in
# the dictionary's own math example (TradeVolume / FloatShares).
BUILTIN_BINDINGS: Dict[str, DictBinding] = {
    "close": DictBinding("close", "收盘价", "DailyQuote", "ClosePx",
                         data_type="smallmoney", evidence="dict: DailyQuote"),
    "open": DictBinding("open", "开盘价", "DailyQuote", "OpenPx",
                        data_type="smallmoney", evidence="dict: DailyQuote"),
    "high": DictBinding("high", "最高价", "DailyQuote", "HighPx",
                        data_type="smallmoney", evidence="dict: DailyQuote"),
    "low": DictBinding("low", "最低价", "DailyQuote", "LowPx",
                       data_type="smallmoney", evidence="dict: DailyQuote"),
    "vwap": DictBinding("vwap", "成交均价", "DailyQuote",
                        expr="TradeValue / NULLIF(TradeVolume, 0)",
                        columns=["TradeValue", "TradeVolume"],
                        evidence="dict: DailyQuote (成交金额/成交量)"),
    "volume": DictBinding("volume", "成交量", "DailyQuote", "TradeVolume",
                          data_type="decimal", evidence="dict: DailyQuote"),
    "amount": DictBinding("amount", "成交额", "DailyQuote", "TradeValue",
                          data_type="money", evidence="dict: DailyQuote"),
    "turnover": DictBinding("turnover", "换手率", "DailyQuote",
                            expr="TradeVolume / NULLIF(FloatShares, 0)",
                            columns=["TradeVolume", "FloatShares"],
                            evidence="dict: math 20日平均换手率 "
                                     "(TradeVolume / FloatShares)"),
    "market_cap": DictBinding("market_cap", "总市值", "DailyQuote",
                              "MarketCap", data_type="decimal",
                              evidence="dict: DailyQuote"),
    "float_share": DictBinding("float_share", "流通股本", "DailyQuote",
                               "FloatShares", data_type="decimal",
                               evidence="dict: DailyQuote"),
    "returns": DictBinding("returns", "日度收益", "DailyQuote", "DailyReturn",
                           data_type="decimal", evidence="dict: DailyQuote"),
}

# fields with no daily-quote binding: left as TODO in generated code
UNBOUND_FIELDS = {"pe", "pb", "roe", "index"}

# strip unit suffixes when comparing Chinese names, e.g. 收盘价(元) -> 收盘价
_UNIT_TAIL = ("(元)", "（元）", "(股)", "（股）", "(笔)", "（笔）", "(%)", "（%）")


class _TableParser(HTMLParser):
    """Parse dictionary table_content HTML into plain cell rows."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[str]] = []
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(c for c in self._row):
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_table_content(html: str) -> List[Dict]:
    """Parse one dictionary result's table_content into field rows.

    Handles the two header layouts the dictionary returns:
      xy  : table_code | table_name | column_rank | column_name | chi_name | ...
      tl  : 序号       | 字段名     | 中文名称     | FULL_NAME_EN | 数据类型 | ...
    Returns rows: {"table", "column", "cn_name", "data_type"}.
    """
    parser = _TableParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - lenient
        return []
    if len(parser.rows) < 2:
        return []
    header = [h.strip() for h in parser.rows[0]]

    def col_idx(*names) -> int:
        for i, h in enumerate(header):
            if h in names:
                return i
        return -1

    if "column_name" in header:                      # xy layout
        i_table, i_col = col_idx("table_name"), col_idx("column_name")
        i_cn, i_type = col_idx("chi_name"), col_idx("data_type")
    else:                                            # tl/processed layout
        i_table, i_col = -1, col_idx("字段名", "name")
        i_cn, i_type = col_idx("中文名称", "chinese_name"), col_idx("数据类型", "type")
    if i_col < 0 or i_cn < 0:
        return []

    out = []
    for row in parser.rows[1:]:
        if len(row) <= max(i_col, i_cn):
            continue
        column, cn = row[i_col].strip(), row[i_cn].strip()
        if not column or not cn or column.isdigit():
            continue
        out.append({
            "table": row[i_table].strip() if 0 <= i_table < len(row) else "",
            "column": column,
            "cn_name": cn,
            "data_type": row[i_type].strip() if 0 <= i_type < len(row) else "",
        })
    return out


def _norm_cn(name: str) -> str:
    for tail in _UNIT_TAIL:
        if name.endswith(tail):
            name = name[: -len(tail)]
    return name.strip()


def _score_row(query: str, row: Dict) -> float:
    q, c = _norm_cn(query), _norm_cn(row["cn_name"])
    score = 0.0
    if q and c:
        if q == c:
            score += 10.0
        elif q in c or c in q:
            score += 5.0
    else:
        return 0.0
    # daily-quote table of the signal library wins over intraday/other tables
    if row.get("table") == PREFERRED_TABLE:
        score += 6.0
    if row.get("source_type") == "xy":
        score += 1.0
    return score


class DataDictClient:
    """Minimal client for datadict.example.com with a JSON file cache."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL,
                 scope: str = DEFAULT_SCOPE, method: str = DEFAULT_METHOD,
                 k: int = 5, timeout: int = 60,
                 cache_path: Optional[str] = None,
                 fetch_fn: Optional[Callable[[str, Dict], Dict]] = None):
        self.base_url = base_url.rstrip("/")
        self.scope = scope
        self.method = method
        self.k = k
        self.timeout = timeout
        self.cache_path = cache_path
        self._fetch_fn = fetch_fn or self._http_fetch
        self._cache: Optional[Dict] = None

    # -- transport ----------------------------------------------------------
    def _http_fetch(self, url: str, body: Dict) -> Dict:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": "factor-forge/0.2"},
            method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # -- cache --------------------------------------------------------------
    def _cache_key(self, body: Dict) -> str:
        return json.dumps([body.get("query"), body.get("scope"),
                           body.get("method"), body.get("k")],
                          ensure_ascii=False)

    def _load_cache(self) -> Dict:
        if self._cache is None:
            self._cache = {}
            if self.cache_path and os.path.exists(self.cache_path):
                try:
                    with open(self.cache_path, encoding="utf-8") as f:
                        self._cache = json.load(f)
                except (OSError, ValueError):
                    self._cache = {}
        return self._cache

    def _save_cache(self) -> None:
        if not self.cache_path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.cache_path)),
                        exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    # -- queries ------------------------------------------------------------
    def query(self, text: str) -> Dict:
        """Raw dictionary query response (cached)."""
        body = {"query": text, "scope": self.scope,
                "method": self.method, "k": self.k}
        cache = self._load_cache()
        key = self._cache_key(body)
        if key in cache:
            return cache[key]
        resp = self._fetch_fn(f"{self.base_url}/query", body)
        cache[key] = resp
        self._save_cache()
        return resp

    def search_fields(self, text: str,
                      score_query: Optional[str] = None) -> List[Dict]:
        """Query and flatten into candidate field rows (best first).

        `score_query` may differ from the retrieval text: retrieve with extra
        context (e.g. the preferred table name) but score against the bare
        Chinese term so exact-name matches still rank first.
        """
        try:
            resp = self.query(text)
        except (OSError, ValueError):
            return []
        rows: List[Dict] = []
        for result in resp.get("results") or []:
            source = result.get("source_type", "")
            pdf = result.get("pdf_name", "")
            for row in parse_table_content(result.get("table_content", "")):
                row = dict(row)
                row["source_type"] = source
                row["pdf_name"] = pdf
                if not row.get("table") and " " not in pdf:
                    row["table"] = pdf  # field docs named after their table
                row["score"] = _score_row(score_query or text, row)
                rows.append(row)
        rows.sort(key=lambda r: r["score"], reverse=True)
        return rows

    def resolve_column(self, cn_query: str) -> Optional[Dict]:
        """Best exact-ish dictionary hit for a Chinese field name, if any."""
        rows = self.search_fields(f"{cn_query} {PREFERRED_TABLE}",
                                  score_query=cn_query)
        for row in rows:
            if row["score"] >= 10.0:
                return row
        return None


def resolve_factor_fields(factor, client: Optional[DataDictClient] = None,
                          use_dict: bool = True) -> Dict[str, DictBinding]:
    """Bind every data field a Factor needs to a company data name.

    Preference: exact hit from the company dictionary, then the verified
    builtin binding. Fields with neither stay absent (codegen emits TODO).
    """
    bindings: Dict[str, DictBinding] = {}
    var_by_field: Dict[str, str] = {}
    for v in factor.variables:
        if v.data_field and v.data_field not in var_by_field:
            var_by_field[v.data_field] = v.name_cn
    # fields implied by the operator + fields declared by variables
    needed: List[str] = []
    for data_field in (OPERATOR_FIELDS.get(factor.operator or "generic", [])
                       + factor.required_fields()):
        if data_field and data_field not in needed:
            needed.append(data_field)
    for data_field in needed:
        if data_field in UNBOUND_FIELDS:
            continue
        cn_query = var_by_field.get(data_field) or ""
        hit = None
        if use_dict and client is not None and cn_query:
            try:
                hit = client.resolve_column(cn_query)
            except (OSError, ValueError):
                hit = None
        if hit is not None:
            bindings[data_field] = DictBinding(
                data_field=data_field, cn_name=_norm_cn(hit["cn_name"]),
                table=hit.get("table") or PREFERRED_TABLE,
                column=hit["column"], data_type=hit.get("data_type", ""),
                origin="dict",
                evidence=f"dict: {hit.get('pdf_name', '')}")
            continue
        builtin = BUILTIN_BINDINGS.get(data_field)
        if builtin is not None:
            bindings[data_field] = builtin
    return bindings
