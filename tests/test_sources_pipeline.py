import datetime as dt
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factor_forge import sources as src            # noqa: E402
from factor_forge.pdfinput import (find_factor_briefs,  # noqa: E402
                                    FactorBriefCandidate)

HERE = os.path.dirname(os.path.abspath(__file__))

BRIEF_TEXT = """某证券 量化因子研究
二、因子定义与构造
资本利得突出量
行为金融因子-处置效应
RP_t = (1/k) * Σ V_{t-n} * P_{t-n}
CGO_{i,t} = (Close_{i,t} - RP_t) / Close_{i,t}
其中：
V_t、Close、P_t 分别为t日的换手率、收盘价、VWAP均价
—说明—
CGO因子对应反转效应，与未来收益负相关。
"""

NOISE_TEXT = """内容目录
一、背景 .................................... 2
二、因子定义与构造 ........................... 3
图表1：IC序列 ............................... 5
敬请参阅最后一页特别声明
"""


# ---- pdfinput ------------------------------------------------------------
def test_find_factor_briefs_scores_formula_section():
    text = NOISE_TEXT + "\n" + BRIEF_TEXT
    cands = find_factor_briefs(text)
    assert cands
    best = cands[0]
    assert "CGO_{i,t}" in best.text
    assert best.score >= 3.0
    # TOC leader lines are not anchors
    assert all("...." not in c.anchor for c in cands)


def test_find_factor_briefs_empty_on_pure_noise():
    assert find_factor_briefs("图表1 ......\n敬请参阅最后一页特别声明") == []


def test_pdf_roundtrip(tmp_path):
    fitz = pytest.importorskip("fitz")
    from factor_forge.pdfinput import pdf_to_briefs
    path = str(tmp_path / "report.pdf")
    doc = fitz.open()
    page = doc.new_page()
    tw = fitz.TextWriter(page.rect)
    y = 60
    for line in BRIEF_TEXT.splitlines():
        tw.append((60, y), line, fontsize=11)
        y += 16
    tw.write_text(page)
    doc.save(path)
    cands = pdf_to_briefs(path)
    assert cands and "CGO_{i,t}" in cands[0].text


# ---- registry ------------------------------------------------------------
def _registry(tmp_path, sources):
    p = tmp_path / "sources.json"
    p.write_text(json.dumps({"sources": sources}), encoding="utf-8")
    return str(p)


def test_load_registry_and_relative_folder(tmp_path):
    (tmp_path / "inbox").mkdir()
    reg = _registry(tmp_path, [
        {"id": "inb", "name": "inbox", "kind": "folder",
         "params": {"path": "inbox"}}])
    loaded = src.load_registry(reg)
    assert loaded[0].params["path"] == str(tmp_path / "inbox")


def test_registry_validation(tmp_path):
    reg = _registry(tmp_path, [
        {"id": "x", "name": "x", "kind": "nope"},
        {"id": "x", "name": "dup", "kind": "folder",
         "params": {"path": "/tmp"}},
    ])
    with pytest.raises(ValueError):
        src.load_registry(reg)


# ---- eastmoney fetcher (offline, injected fetch) ---------------------------
_LIST_ITEM = {
    "title": "金融工程：多因子选股月报",
    "infoCode": "AP202609020000000001",
    "publishDate": "2026-09-02 00:00:00.000",
    "orgSName": "测试证券",
    "industryName": "金融工程",
}


def test_eastmoney_list_fetch_offline():
    calls = []

    def fake_json(url):
        calls.append(url)
        return {"data": [dict(_LIST_ITEM),
                         dict(_LIST_ITEM, title="无关行业周报",
                              infoCode="AP0000000000000002")]}

    s = src.Source(id="em", name="em", kind="eastmoney",
                   params={"endpoint": "list", "qtype": 1, "days": 7,
                           "pages": 2,
                           "keywords": ["因子", "多因子"]})
    entries = src.fetch_entries(s, today=dt.date(2026, 9, 2),
                                fetch_json=fake_json)
    assert len(entries) == 1                       # keyword filter applied
    e = entries[0]
    assert e.entry_id == "AP202609020000000001"
    assert e.pdf_url.endswith("H3_AP202609020000000001_1.pdf")
    assert e.date == "2026-09-02" and e.org == "测试证券"
    assert len(calls) == 2 and "pageNo=2" in calls[1]   # paginated


def test_eastmoney_jg_fetch_offline():
    def fake_json(url):
        return {"data": [{
            "id": 273000001433599252, "title": "Ox Alpha发布",
            "publishDate": "2026-09-02 00:00:00.000",
            "orgSName": "中信证券经纪(香港)", "encodeUrl": "abc+def/ghi="}]}

    s = src.Source(id="emj", name="emj", kind="eastmoney",
                   params={"endpoint": "jg", "qtype": 2, "days": 7})
    entries = src.fetch_entries(s, today=dt.date(2026, 9, 2),
                                fetch_json=fake_json)
    assert len(entries) == 1
    e = entries[0]
    assert e.entry_id == "273000001433599252"
    assert e.pdf_url == ""                        # resolved at download time
    assert e.meta["encodeUrl"] == "abc+def/ghi="


# ---- folder source + incremental state ------------------------------------
def _make_pdf(path, text):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    tw = fitz.TextWriter(page.rect)
    y = 60
    for line in text.splitlines():
        tw.append((60, y), line, fontsize=11)
        y += 16
    tw.write_text(page)
    doc.save(str(path))


def test_folder_source_incremental(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _make_pdf(inbox / "因子研报A.pdf", BRIEF_TEXT)
    s = src.Source(id="inb", name="inbox", kind="folder",
                   params={"path": str(inbox)})
    entries = src.fetch_entries(s)
    assert len(entries) == 1 and entries[0].pdf_url.endswith(".pdf")

    state = {}
    assert not src.is_processed(state, "inb", entries[0].entry_id)
    src.mark_processed(state, "inb", entries[0], "factor", factor_name="CGO")
    assert src.is_processed(state, "inb", entries[0].entry_id)

    # state roundtrip
    spath = str(tmp_path / "state.json")
    src.save_state(spath, state)
    reloaded = src.load_state(spath)
    assert src.is_processed(reloaded, "inb", entries[0].entry_id)
    rows = src.digest_rows(reloaded)
    assert rows[0]["factor"] == "CGO" and rows[0]["status"] == "factor"


def test_download_entry_local_passthrough(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    p = inbox / "x.pdf"
    p.write_bytes(b"%PDF-1.4 test")
    e = src.ReportEntry(entry_id="e1", source_id="s", title="t",
                        date="2026-01-01", org="", url=str(p), pdf_url=str(p))
    assert src.download_entry(e, str(tmp_path / "cache")) == str(p)


def test_download_entry_rejects_empty_body(tmp_path, monkeypatch):
    e = src.ReportEntry(entry_id="e1", source_id="s", title="t",
                        date="2026-01-01", org="",
                        url="https://x/y.pdf", pdf_url="https://x/y.pdf")
    monkeypatch.setattr(src, "fetch_bytes", lambda *a, **k: b"")
    with pytest.raises(IOError, match="empty response"):
        src.download_entry(e, str(tmp_path / "cache"))


# ---- live (skipped when offline) -------------------------------------------
def test_live_eastmoney_list_fetch():
    s = src.Source(id="em", name="em", kind="eastmoney",
                   params={"endpoint": "list", "qtype": 1, "days": 3,
                           "page_size": 10, "keywords": []})
    try:
        entries = src.fetch_entries(s)
    except (OSError, ValueError):
        pytest.skip("eastmoney report API unreachable")
    assert entries and entries[0].pdf_url.startswith("https://pdf.dfcfw.com/")
