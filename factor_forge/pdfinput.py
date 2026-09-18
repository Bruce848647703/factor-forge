"""PDF input: research-report PDF -> text -> factor-brief candidates.

Broker factor reports are published as PDFs. This module turns one into the
plain-text briefs the extractor understands:

    PDF  --extract_pdf_text-->  full text  --find_factor_briefs-->  brief(s)

Backend is PyMuPDF (``fitz``). It is an optional dependency: importing this
module never fails, but calling extraction without fitz raises a clear error.

Section detection is heuristic and deterministic (no LLM): it scans for
factor-definition anchors (因子定义/因子构造/计算公式/指标说明...), scores the
following window by formula/variable markers ("=", 其中, 因子, 方向), and
returns the best candidates. The extractor stays responsible for parsing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

# headings/anchors that typically open a factor definition section
_ANCHOR_PATTERNS = [
    r"因子(?:的)?(?:定义|构造|构建|计算|公式|说明|设计|选取|逻辑)",
    r"(?:计算|构造|构建)(?:该)?因子",
    r"(?:核心|选股|因子)[^。；]{0,12}公式",
    r"(?:指标|因子)含义",
]
# lines that look like a formula
_FORMULA_RE = re.compile(r"[=＝]")
# boilerplate that should not count as content
_NOISE_RE = re.compile(
    r"(敬请参阅|特别声明|图表\s*\d|目录|^\.{3,}|—{2,}|第?\s*\d+\s*页|"
    r"风险提示|请务必阅读)")


@dataclass
class FactorBriefCandidate:
    score: float
    text: str
    anchor: str  # the line that opened the section


def extract_pdf_text(path: str) -> str:
    """Extract the full text of a PDF (pages joined by blank lines)."""
    try:
        import fitz  # PyMuPDF
    except ImportError as e:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "PDF support requires PyMuPDF: pip install pymupdf") from e
    pages: List[str] = []
    with fitz.open(path) as doc:
        for page in doc:
            pages.append(page.get_text())
    return "\n\n".join(pages)


def _score_window(lines: List[str]) -> float:
    score = 0.0
    for ln in lines:
        if _NOISE_RE.search(ln):
            score -= 0.5
            continue
        if _FORMULA_RE.search(ln):
            score += 3.0
        if "其中" in ln:
            score += 2.0
        if "因子" in ln:
            score += 0.5
        if re.search(r"(正相关|负相关|方向|收益)", ln):
            score += 0.5
    return score


def _clean_line(ln: str) -> str:
    # collapse the letter-spacing some PDFs insert between CJK/latin chars
    ln = re.sub(r"(?<=[\u4e00-\u9fff]) (?=[\u4e00-\u9fff])", "", ln)
    return ln.strip()


def find_factor_briefs(text: str, top_k: int = 3, window: int = 30,
                       min_score: float = 3.0) -> List[FactorBriefCandidate]:
    """Locate factor-definition sections in report text.

    Returns up to `top_k` candidates (score-descending), each a self-contained
    brief suitable for `factor_forge.extract`.
    """
    lines = [_clean_line(l) for l in text.splitlines()]
    lines = [l for l in lines if l]
    if not lines:
        return []

    anchors = [re.compile(p) for p in _ANCHOR_PATTERNS]
    candidates: List[FactorBriefCandidate] = []
    used = [False] * len(lines)
    for i, ln in enumerate(lines):
        if used[i] or len(ln) > 60:
            continue
        if re.search(r"\.{4,}|…{2,}", ln):
            continue                       # table-of-contents leader line
        hit = next((a for a in anchors if a.search(ln)), None)
        if hit is None:
            continue
        block = lines[i:i + window]
        score = _score_window(block)
        if score < min_score:
            continue
        for j in range(i, min(i + window, len(lines))):
            used[j] = True
        candidates.append(FactorBriefCandidate(
            score=score, text="\n".join(block), anchor=ln))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:top_k]


def pdf_to_briefs(path: str, top_k: int = 3) -> List[FactorBriefCandidate]:
    """Convenience: PDF file -> factor-brief candidates."""
    return find_factor_briefs(extract_pdf_text(path), top_k=top_k)


def best_brief(candidates: List[FactorBriefCandidate]
               ) -> Optional[FactorBriefCandidate]:
    return candidates[0] if candidates else None
