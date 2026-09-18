import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factor_forge import (Factor, FactorVariable, extract,  # noqa: E402
                          validate_corepy, html_to_text, is_url,
                          load_brief_from_path, url_basename,
                          DataDictClient, BUILTIN_BINDINGS,
                          resolve_factor_fields, generate_corepy,
                          generate_repo, factor_to_company_spec)
from factor_forge.datadict import parse_table_content  # noqa: E402
from factor_forge.validator import make_company_sample_data  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
INPUT = os.path.join(HERE, "..", "examples", "input")


def _read(name):
    with open(os.path.join(INPUT, name), encoding="utf-8") as f:
        return f.read()


# ---- webinput --------------------------------------------------------------
def test_is_url_and_basename():
    assert is_url("https://fund.eastmoney.com/006195.html")
    assert is_url("http://datadict.example.com/")
    assert is_url("file:///tmp/x.html")
    assert not is_url("examples/input/cgo.txt")
    assert url_basename("https://fund.eastmoney.com/006195.html") == "006195"
    assert url_basename("https://x.com/a/b/?q=1") == "b"


def test_html_to_text_structure():
    html = """
    <html><head><title>因子研究</title>
    <script>var x = 1;</script>
    <style>.a{color:red}</style>
    </head><body>
    <h2>资本利得突出量</h2>
    <p>CGO = (Close - RP) / Close</p>
    <p>\ufeff其中换手率加权</p>
    <table><tr><th>变量</th><th>含义</th></tr>
    <tr><td>V_t</td><td>换手率</td></tr></table>
    </body></html>
    """
    text = html_to_text(html)
    lines = text.splitlines()
    assert lines[0] == "因子研究"                     # title first
    assert "资本利得突出量" in text
    assert "CGO = (Close - RP) / Close" in text
    assert "V_t | 换手率" in text                     # table row kept
    assert "var x" not in text                        # script stripped
    assert "color:red" not in text                    # style stripped
    assert not any(ln.startswith("\ufeff") for ln in lines)  # BOM stripped


def test_load_brief_from_file_url(tmp_path):
    page = tmp_path / "brief.html"
    page.write_text("<html><title>T</title><body><p>X = a/b</p></body></html>",
                    encoding="utf-8")
    brief, source = load_brief_from_path(page.as_uri())
    assert "X = a/b" in brief
    assert source.startswith("file://")


def test_eastmoney_like_page(tmp_path):
    html = """<html><title>国金量化多因子股票A(006195)—天天基金网</title><body>
    <p>换手率 = 对应期间持仓买卖金额 /(期初净资产+期末净资产）</p>
    </body></html>"""
    text = html_to_text(html)
    fac = extract(text, source="url")
    assert "换手率" in fac.formula


# ---- datadict ---------------------------------------------------------------
_XY_TABLE = """<table><tr><th>table_code</th><th>table_name</th>
<th>column_rank</th><th>column_name</th><th>chi_name</th><th>data_type</th></tr>
<tr><td>1</td><td>DailyQuote</td><td>11</td><td>ClosePx</td>
<td>收盘价(元)</td><td>smallmoney</td></tr>
<tr><td>1</td><td>DailyQuote</td><td>9</td><td>HighPx</td>
<td>最高价(元)</td><td>smallmoney</td></tr></table>"""

_TL_TABLE = """<table><tr><th>序号</th><th>字段名</th><th>中文名称</th>
<th>FULL_NAME_EN</th><th>数据类型</th><th>可空</th></tr>
<tr><td>5</td><td>TURNOVER_RATE</td><td>换手率</td><td></td>
<td>decimal(19,4)</td><td>是</td></tr></table>"""


def test_parse_table_content_both_layouts():
    xy = parse_table_content(_XY_TABLE)
    assert {"table": "DailyQuote", "column": "ClosePx",
            "cn_name": "收盘价(元)", "data_type": "smallmoney"} in xy
    tl = parse_table_content(_TL_TABLE)
    assert tl[0]["column"] == "TURNOVER_RATE" and tl[0]["cn_name"] == "换手率"


def test_builtin_bindings_complete():
    for f in ("close", "open", "high", "low", "vwap", "volume", "amount",
              "turnover", "market_cap", "float_share", "returns"):
        assert f in BUILTIN_BINDINGS
    assert BUILTIN_BINDINGS["turnover"].expr  # derived, not a raw column
    assert BUILTIN_BINDINGS["close"].table == "DailyQuote"


def _fake_client(rows_by_query):
    def fetch(url, body):
        q = body["query"]
        results = []
        for row_query, rows in rows_by_query.items():
            if row_query in q:
                trs = "".join(
                    f"<tr><td>1</td><td>DailyQuote</td><td>{i}</td>"
                    f"<td>{c}</td><td>{cn}</td><td>t</td></tr>"
                    for i, (c, cn) in enumerate(rows))
                results.append({"pdf_name": "DailyQuote",
                                "source_type": "xy",
                                "table_content": _XY_TABLE.split("</tr>")[0]
                                + "</tr>" + trs + "</table>"})
        return {"metadata": {}, "results": results}
    return DataDictClient(fetch_fn=fetch)


def test_dict_client_resolve_and_cache(tmp_path):
    calls = []

    def fetch(url, body):
        calls.append(body["query"])
        return {"metadata": {}, "results": [{
            "pdf_name": "DailyQuote", "source_type": "xy",
            "table_content": _XY_TABLE}]}

    cache = str(tmp_path / "cache.json")
    client = DataDictClient(fetch_fn=fetch, cache_path=cache)
    hit = client.resolve_column("收盘价")
    assert hit and hit["column"] == "ClosePx"
    client.resolve_column("收盘价")          # cached, no second HTTP call
    assert calls == ["收盘价 DailyQuote"]
    assert os.path.exists(cache)

    client2 = DataDictClient(fetch_fn=fetch, cache_path=cache)
    client2.resolve_column("收盘价")          # reloaded from disk cache
    assert calls == ["收盘价 DailyQuote"]


def test_resolve_factor_fields_dict_overrides_builtin():
    client = _fake_client({"收盘价": [("ClosePx", "收盘价(元)")]})
    fac = extract(_read("cgo.txt"))
    bindings = resolve_factor_fields(fac, client=client)
    assert bindings["close"].origin == "dict"
    assert bindings["close"].column == "ClosePx"
    assert bindings["turnover"].origin == "builtin"   # derived stays builtin
    assert "pe" not in bindings


def test_resolve_factor_fields_offline():
    fac = extract(_read("cgo.txt"))
    bindings = resolve_factor_fields(fac, client=None, use_dict=False)
    assert bindings["close"].column == "ClosePx"
    assert bindings["vwap"].expr


def test_resolve_binds_operator_implied_fields():
    # no variables declared (e.g. brief extracted from a noisy web page):
    # the operator still implies which fields must be bound
    fac = Factor(name="X", name_cn="测试", operator="turnover_weighted_price")
    bindings = resolve_factor_fields(fac, client=None, use_dict=False)
    assert set(bindings) == {"close", "turnover", "vwap"}
    code = generate_corepy(fac, runner_id=1)
    assert "-- TODO: bind" not in code
    assert "ClosePx" in code


# ---- corepy -----------------------------------------------------------------
def test_generate_corepy_cgo_house_format():
    fac = extract(_read("cgo.txt"), source="cgo.txt")
    code = generate_corepy(fac, runner_id=30180, runner_name="Signal_CGO")
    assert 'SIGNAL_FILE: Final = "runner_value_30180.json"' in code
    assert 'SIGNAL_CHECK_FILE: Final = "check_30180.json"' in code
    assert "FROM LegacyDB.dbo.DailyQuote" in code
    assert "TradeVolume / NULLIF(FloatShares, 0) AS turnover" in code
    assert "TradeValue / NULLIF(TradeVolume, 0) AS vwap" in code
    assert "ClosePx" in code
    assert "def get_signal(date):" in code
    assert "function.save_signal(" in code
    assert "function.read_tradingdays(trading_date, 121)" in code
    assert "import duckdb" in code
    assert "(parameter)" in code                  # k is a parameter, not data
    compile(code, "<gen>", "exec")


def test_generate_corepy_names_and_operators():
    fac = extract(_read("momentum.txt"))
    code = generate_corepy(fac, runner_id=1)
    assert "Signal_MOM" in code                   # Signal_ prefix auto-added
    assert "LAG: Final" in code and "SKIP: Final" in code

    gen = extract(_read("amihud.txt"))
    gcode = generate_corepy(gen, runner_id=2)
    assert "NotImplementedError" in gcode
    compile(gcode, "<gen>", "exec")


def test_generate_repo_scaffold(tmp_path):
    fac = extract(_read("cgo.txt"), source="cgo.txt")
    out = str(tmp_path / "Signal_CGO")
    written = generate_repo(fac, out, runner_id=30180,
                            runner_name="Signal_CGO")
    for rel in ("core.py", "function.py", "main.py", "manifest.json",
                "rawdata.json", ".gitlab-ci.yml", "materials/spec.json",
                "README.md"):
        assert rel in written and os.path.exists(written[rel])

    manifest = json.load(open(written["manifest.json"]))
    assert manifest["runner_info"][0]["runner_id"] == 30180
    assert manifest["runner_info"][0]["runner_name"] == "Signal_CGO"
    assert manifest["project_info"][0]["project_type"] == "signal"
    assert "momentum_factor" in manifest["project_info"][0]["git_repo"]

    rawdata = json.load(open(written["rawdata.json"]))
    assert rawdata["legacy_db"]["host"] == "TODO"  # never real credentials

    spec = json.load(open(written["materials/spec.json"]))
    assert spec["signal_name"] == "Signal_CGO"
    assert spec["signal_name_raw"] == "资本利得突出量"
    assert "V_t" in spec["signal_variables_name"]
    assert "V_t: " in spec["signal_variables_description"]

    fn_src = open(written["function.py"]).read()
    assert "def save_signal(" in fn_src and "def read_tradingdays(" in fn_src


def test_factor_to_company_spec():
    fac = Factor(name="X", name_cn="测试", category="cat",
                 formula="X = a/b\nc = 1",
                 variables=[FactorVariable("a", "收盘价", "close",
                                           "t日收盘价")])
    spec = factor_to_company_spec(fac, "Signal_X")
    assert spec["signal_formula"] == ["X = a/b", "c = 1"]
    assert spec["signal_formula_raw"] == "X = a/b \\quad c = 1"
    assert spec["signal_variables_description"] == "a: t日收盘价"


# ---- core.py validation ------------------------------------------------------
def test_validate_corepy_cgo(tmp_path):
    fac = extract(_read("cgo.txt"))
    path = str(tmp_path / "core.py")
    open(path, "w", encoding="utf-8").write(
        generate_corepy(fac, runner_id=30180, runner_name="Signal_CGO"))
    ok, msg, signal = validate_corepy(path, fac, runner_id=30180,
                                      n_stocks=6, n_days=260)
    assert ok, msg
    assert list(signal.columns) == ["runner_code", "runner_value"]


def test_validate_corepy_wrong_runner_id(tmp_path):
    fac = extract(_read("cgo.txt"))
    path = str(tmp_path / "core.py")
    open(path, "w", encoding="utf-8").write(
        generate_corepy(fac, runner_id=30180))
    ok, msg, _ = validate_corepy(path, fac, runner_id=99999)
    assert not ok and "runner_id" in msg


def test_validate_corepy_generic_skeleton(tmp_path):
    fac = extract(_read("amihud.txt"))
    path = str(tmp_path / "core.py")
    open(path, "w", encoding="utf-8").write(generate_corepy(fac))
    ok, msg, _ = validate_corepy(path, fac)
    assert not ok and "generic skeleton" in msg


def test_validate_corepy_all_operators(tmp_path):
    for name, rid in (("momentum.txt", 11), ("reversal.txt", 12),
                      ("volatility.txt", 13)):
        fac = extract(_read(name))
        path = str(tmp_path / f"core_{rid}.py")
        open(path, "w", encoding="utf-8").write(
            generate_corepy(fac, runner_id=rid))
        ok, msg, _ = validate_corepy(path, fac, runner_id=rid)
        assert ok, f"{name}: {msg}"


def test_make_company_sample_data():
    df = make_company_sample_data(["close", "turnover", "vwap"],
                                  n_stocks=3, n_days=50)
    assert set(df.columns) == {"InnerID", "DataDate", "close",
                               "turnover", "vwap"}
    assert df["InnerID"].nunique() == 3 and len(df) == 150
    assert ((df["turnover"] > 0) & (df["turnover"] < 1)).all()


# ---- live dictionary (skipped when offline) ----------------------------------
def test_live_dict_close_price():
    client = DataDictClient(timeout=10)
    try:
        hit = client.resolve_column("收盘价")
    except (OSError, ValueError):
        pytest.skip("datadict.example.com unreachable")
    if hit is None:
        pytest.skip("dictionary returned no exact hit")
    assert hit["table"] == "DailyQuote" and hit["column"] == "ClosePx"
