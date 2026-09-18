import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factor_forge import (Factor, FactorVariable, extract, generate,
                          generate_to_file, validate_code, make_sample_data)

HERE = os.path.dirname(os.path.abspath(__file__))
INPUT = os.path.join(HERE, "..", "examples", "input")


def _read(name):
    with open(os.path.join(INPUT, name), encoding="utf-8") as f:
        return f.read()


# ---- schema ---------------------------------------------------------------
def test_schema_roundtrip():
    fac = Factor(name="X", name_cn="测试", category="cat", formula="X = a/b",
                 variables=[FactorVariable("a", "收盘价", "close")],
                 direction=-1, params={"lookback": 5})
    fac2 = Factor.from_json(fac.to_json())
    assert fac2.name == "X" and fac2.direction == -1
    assert fac2.variables[0].data_field == "close"
    assert fac2.params["lookback"] == 5
    assert fac.required_fields() == ["close"]


# ---- extractor ------------------------------------------------------------
def test_extract_cgo():
    fac = extract(_read("cgo.txt"), source="cgo.txt")
    assert fac.name == "CGO"
    assert fac.name_cn == "资本利得突出量"
    assert fac.operator == "turnover_weighted_price"
    assert fac.direction == -1                      # 与未来收益负相关
    fields = {v.symbol: v.data_field for v in fac.variables}
    assert fields.get("V_t") == "turnover"
    assert fields.get("Close") == "close"
    assert fields.get("P_t") == "vwap"


def test_extract_momentum_vs_reversal():
    mom = extract(_read("momentum.txt"))
    rev = extract(_read("reversal.txt"))
    assert mom.operator == "momentum"
    assert rev.operator == "reversal"
    assert mom.direction == 1 and rev.direction == 1


def test_extract_volatility_direction():
    vol = extract(_read("volatility.txt"))
    assert vol.operator == "volatility"
    assert vol.direction == -1


def test_extract_subscript_symbols_preserved():
    fac = extract(_read("amihud.txt"))
    syms = [v.symbol for v in fac.variables]
    assert "r_{i,s}" in syms                        # comma inside subscript kept
    assert "Amount_{i,s}" in syms


# ---- codegen --------------------------------------------------------------
def test_generate_cgo_contains_impl():
    fac = extract(_read("cgo.txt"))
    code = generate(fac)
    assert "def CGO(" in code
    assert "survival" in code                        # turnover-weighted loop present
    assert "资本利得突出量" in code


def test_generate_generic_skeleton():
    fac = extract(_read("amihud.txt"))
    assert fac.operator == "generic"
    code = generate(fac)
    assert "NotImplementedError" in code
    assert "ILLIQ" in code


# ---- validator (end-to-end) ----------------------------------------------
def test_cgo_end_to_end(tmp_path):
    fac = extract(_read("cgo.txt"))
    path = generate_to_file(fac, str(tmp_path / "cgo.py"))
    ok, msg, out = validate_code(path, fac)
    assert ok, msg
    assert out.notna().sum() > 100                   # finite after lookback warmup


def test_momentum_end_to_end(tmp_path):
    fac = extract(_read("momentum.txt"))
    path = generate_to_file(fac, str(tmp_path / "mom.py"))
    ok, msg, out = validate_code(path, fac)
    assert ok, msg


def test_sample_data_fields():
    data = make_sample_data(["close", "turnover", "amount"], n=50)
    assert set(data) == {"close", "turnover", "amount"}
    assert len(data["close"]) == 50
    assert (data["turnover"] > 0).all()
