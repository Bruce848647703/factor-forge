"""Extractor: research-report factor brief (text) -> structured Factor.

Handles the semi-structured brief format produced by report mining:

    资本利得突出量                          <- name
    行为金融因子-处置效应                   <- category
    RP_t = ...                             <- formula lines
    CGO_{i,t} = (Close - RP)/Close
    —其中—
    ① V_t、Close、P_t 分别为换手率、收盘价、VWAP均价   <- variables
    ② k 为权重系数 ...
    —说明— ... CGO 与未来收益负相关。        <- direction / rationale

The extractor is heuristic and lenient: it never raises on messy text, it just
fills in what it can. Downstream codegen decides what to do with the result.
"""

from __future__ import annotations

import re
from typing import List, Tuple

from .schema import Factor, FactorVariable

# ---- Chinese term -> canonical data field ----------------------------------
_FIELD_KEYWORDS = [
    ("换手率", "turnover"),
    ("成交额", "amount"),
    ("成交量", "volume"),
    ("VWAP", "vwap"), ("均价", "vwap"),
    ("收盘价", "close"), ("开盘价", "open"),
    ("最高价", "high"), ("最低价", "low"),
    ("总市值", "market_cap"), ("市值", "market_cap"),
    ("流通股本", "float_share"),
    ("收益率", "returns"),
    ("市盈率", "pe"), ("市净率", "pb"),
    ("净资产收益率", "roe"),
]

_POSITIVE = ["正相关", "正向", "正效应", "未来收益高", "正预测"]
_NEGATIVE = ["负相关", "负向", "负效应", "反转效应", "未来收益低", "负预测"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _map_field(text: str) -> str:
    """Map a Chinese variable description to a canonical data field."""
    for kw, field in _FIELD_KEYWORDS:
        if kw in text:
            return field
    return ""


def _detect_direction(text: str) -> Tuple[int, str]:
    for kw in _NEGATIVE:
        if kw in text:
            return -1, kw
    for kw in _POSITIVE:
        if kw in text:
            return 1, kw
    return 0, ""


def _clean_symbol(s: str) -> str:
    return s.strip().strip("，。；,;:：()（）")


def _parse_variable_line(line: str) -> List[FactorVariable]:
    """Parse one variable-definition line into FactorVariables.

    Supports:
      'V_t、Close、P_t 分别为 t日的换手率、收盘价、VWAP均价'
      'k 为 权重系数，用于保证...'
    """
    out: List[FactorVariable] = []
    # split on '分别为' or '为' / '是'
    m = re.split(r"分别为|为|是", line, maxsplit=1)
    if len(m) != 2:
        return out
    lhs, rhs = m[0].strip(), m[1].strip()
    # split on the Chinese enumeration comma only, so subscript commas inside
    # symbols like r_{i,s} are preserved
    symbols = [_clean_symbol(x) for x in re.split(r"[、]", lhs) if _clean_symbol(x)]
    descs = [_clean_symbol(x) for x in re.split(r"[、,，]", rhs)]
    descs = [d for d in descs if d]
    if not symbols:
        return out
    if len(symbols) == len(descs):
        for sym, desc in zip(symbols, descs):
            out.append(FactorVariable(symbol=sym, name_cn=desc,
                                      data_field=_map_field(desc), description=desc))
    else:
        # one description for all symbols, or mismatched -> attach whole rhs
        for sym in symbols:
            out.append(FactorVariable(symbol=sym, name_cn=rhs,
                                      data_field=_map_field(rhs), description=rhs))
    return out


def _infer_operator(formula: str, name: str, category: str, text: str) -> str:
    """Heuristically pick a codegen operator template.

    Classification priority: factor NAME / CATEGORY first (most descriptive),
    then the formula, then the body text. This avoids misclassifying e.g. a
    momentum factor whose body happens to mention '反转'.
    """
    f = formula
    head = f"{name} {category}"
    if ("换手" in head or "换手" in text or "turnover" in f.lower()
            or "V_" in f or "V_{t" in f) and (
            "Π" in f or "prod" in f.lower() or "参考" in text or "持仓成本" in text):
        return "turnover_weighted_price"
    if "反转" in head or "reversal" in f.lower():
        return "reversal"
    if "动量" in head or "momentum" in f.lower():
        return "momentum"
    if "波动" in head or "波动" in text or "std" in f.lower() or "σ" in f:
        return "volatility"
    return "generic"


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------
def extract(brief: str, source: str = "", name: str = "",
            operator_hint: str = "") -> Factor:
    """Parse a factor brief into a Factor. Never raises on messy input."""
    lines = [ln.strip() for ln in brief.splitlines()]
    lines = [ln for ln in lines if ln]

    fac = Factor(name=name, name_cn="", source=source)
    if not lines:
        return fac

    # name / category from leading short lines (before the first formula)
    formula_lines: List[str] = []
    var_lines: List[str] = []
    desc_lines: List[str] = []
    section = "head"          # head -> formula -> vars -> note
    name_candidates: List[str] = []

    for ln in lines:
        has_eq = "=" in ln and not ln.startswith("http")
        if has_eq:
            section = "formula"
        if section == "head" and len(ln) <= 40:
            name_candidates.append(ln)
        if "其中" in ln or ln.startswith("①") or ln.startswith("②"):
            section = "vars"
        if "说明" in ln or "—" in ln and "其中" not in ln:
            if section == "vars":
                section = "note"

        if section == "formula" and has_eq:
            formula_lines.append(ln)
        elif section == "vars" and not ln.startswith("—"):
            var_lines.append(ln.lstrip("①②③④⑤⑥⑦⑧⑨⑩ "))
        elif section == "note":
            desc_lines.append(ln.lstrip("—说明— "))

    # name: first short candidate without '=' ; category: candidate containing 因子
    for c in name_candidates:
        if not fac.name_cn and "=" not in c and "因子" not in c:
            fac.name_cn = c
        elif "因子" in c and not fac.category:
            fac.category = c
    if not fac.name_cn and name_candidates:
        fac.name_cn = name_candidates[0]
    if not fac.name:
        fac.name = _guess_code(fac.name_cn, formula_lines)

    fac.formula = "\n".join(formula_lines)
    fac.description = " ".join(desc_lines).strip()

    for vl in var_lines:
        fac.variables.extend(_parse_variable_line(vl))

    # direction & rationale from full text
    full_text = brief
    direction, note = _detect_direction(full_text)
    fac.direction = direction
    fac.direction_note = note

    fac.operator = operator_hint or _infer_operator(
        fac.formula, fac.name_cn, fac.category, full_text)
    return fac


def _guess_code(name_cn: str, formula_lines: List[str]) -> str:
    """Guess a short code: prefer the LHS of the last formula, else the name.

    Strips trailing underscores/subscripts so 'CGO_{i,t}' -> 'CGO'."""
    for fl in reversed(formula_lines):
        lhs = fl.split("=")[0].strip()
        m = re.match(r"([A-Za-z][A-Za-z0-9]*)", lhs)
        if m:
            return m.group(1)
    return re.sub(r"[^A-Za-z0-9]", "", name_cn) or "FACTOR"
