"""Factor schema: the structured JSON representation of a research factor.

This is the *middle* of the pipeline:

    research-report brief (text)  -->  Factor JSON (this module)  -->  Python code

A Factor captures everything needed to (a) understand the factor and (b)
regenerate executable code: its name, category, economic rationale, the formula,
the variables it consumes (each mapped to a concrete data field), the expected
direction of its relationship with future returns, and its parameters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


# Canonical data fields a factor variable can bind to. The extractor maps the
# report's prose (e.g. "换手率", "收盘价") onto one of these so codegen knows
# which input series to consume.
DATA_FIELDS = {
    "close":      "收盘价 (closing price)",
    "open":       "开盘价 (opening price)",
    "high":       "最高价 (high)",
    "low":        "最低价 (low)",
    "vwap":       "成交均价 (VWAP)",
    "volume":     "成交量 (volume)",
    "turnover":   "换手率 (turnover ratio)",
    "amount":     "成交额 (dollar volume)",
    "market_cap": "总市值 (market cap)",
    "float_share": "流通股本 (float shares)",
    "returns":    "收益率 (returns)",
    "pe":         "市盈率 (P/E)",
    "pb":         "市净率 (P/B)",
    "roe":        "净资产收益率 (ROE)",
    "index":      "基准指数 (benchmark index)",
}


# Canonical data fields each codegen operator consumes. Used by both code
# generators and by data-dictionary binding (a factor brief may declare no
# variables at all, e.g. when extracted from a noisy web page; the operator
# still implies the fields its implementation reads).
OPERATOR_FIELDS = {
    "turnover_weighted_price": ["close", "turnover", "vwap"],
    "momentum": ["close"],
    "reversal": ["close"],
    "volatility": ["close"],
    "generic": [],
}


@dataclass
class FactorVariable:
    """One input variable of a factor."""
    symbol: str                    # symbol used in the formula, e.g. "V_t"
    name_cn: str                   # Chinese name from the report, e.g. "换手率"
    data_field: str                # canonical data field (key of DATA_FIELDS)
    description: str = ""          # verbatim description from the report

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict) -> "FactorVariable":
        return cls(symbol=d.get("symbol", ""), name_cn=d.get("name_cn", ""),
                   data_field=d.get("data_field", ""),
                   description=d.get("description", ""))


@dataclass
class Factor:
    """Structured representation of a research factor."""
    name: str                                   # short code, e.g. "CGO"
    name_cn: str                                # Chinese name
    category: str = ""                          # e.g. "行为金融因子-处置效应"
    description: str = ""                       # economic rationale / notes
    formula: str = ""                           # formula string (LaTeX or plain)
    variables: List[FactorVariable] = field(default_factory=list)
    direction: int = 0                          # +1 / -1 / 0 corr. with future ret
    direction_note: str = ""                    # e.g. "与未来收益负相关"
    params: Dict = field(default_factory=dict)  # e.g. {"lookback": 120}
    source: str = ""                            # provenance (report / institution)
    # implementation hint: which codegen template to use
    operator: str = ""                          # e.g. "turnover_weighted_price"

    # ---- (de)serialization ------------------------------------------------
    def to_dict(self) -> Dict:
        d = asdict(self)
        d["variables"] = [v.to_dict() for v in self.variables]
        return d

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, **kw)

    @classmethod
    def from_dict(cls, d: Dict) -> "Factor":
        d = dict(d)
        d["variables"] = [FactorVariable.from_dict(v) for v in d.get("variables", [])]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    @classmethod
    def from_json(cls, s: str) -> "Factor":
        return cls.from_dict(json.loads(s))

    # ---- helpers ----------------------------------------------------------
    def required_fields(self) -> List[str]:
        """Unique data fields this factor needs."""
        seen, out = set(), []
        for v in self.variables:
            if v.data_field and v.data_field not in seen:
                seen.add(v.data_field)
                out.append(v.data_field)
        return out
