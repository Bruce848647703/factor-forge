"""Validator: execute generated factor code on sample data and sanity-check it.

Loads a generated module, synthesizes plausible input series for the factor's
declared data fields, runs the factor function, and verifies the output is
finite, correctly shaped, and non-degenerate. This closes the loop:

    text -> JSON -> code -> EXECUTES CORRECTLY
"""

from __future__ import annotations

import contextlib
import importlib.util
import sys
import types
import numpy as np
import pandas as pd
from typing import Dict, Iterator, Tuple

from .schema import Factor
from .corepy import _OP_FIELDS

# default params for each data field's synthetic series
_FIELD_PARAMS = {
    "close": 100.0,
    "open": 100.0, "high": 100.0, "low": 100.0, "vwap": 100.0,
    "volume": 1e6, "amount": 1e8, "turnover": 0.01,
    "market_cap": 1e10, "float_share": 1e8,
    "pe": 20.0, "pb": 2.0, "roe": 0.15,
}


def make_sample_data(fields, n: int = 300, seed: int = 7) -> Dict[str, pd.Series]:
    """Synthesize plausible daily series for the requested data fields."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n)
    out = {}
    for f in fields:
        base = _FIELD_PARAMS.get(f, 1.0)
        if f in ("close", "open", "high", "low", "vwap"):
            # random-walk price
            rets = rng.normal(0.0004, 0.015, n)
            px = base * np.exp(np.cumsum(rets))
            out[f] = pd.Series(px, index=idx)
        elif f == "turnover":
            out[f] = pd.Series(np.clip(rng.gamma(2.0, base / 2, n), 1e-4, 0.9), index=idx)
        elif f in ("volume", "amount", "market_cap", "float_share"):
            out[f] = pd.Series(np.abs(rng.normal(base, base * 0.2, n)), index=idx)
        elif f == "returns":
            out[f] = pd.Series(rng.normal(0.0004, 0.015, n), index=idx)
        else:
            out[f] = pd.Series(np.abs(rng.normal(base, base * 0.1, n)), index=idx)
    return out


def _load_module(path: str, mod_name: str):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def validate_code(code_path: str, factor: Factor, n: int = 300
                  ) -> Tuple[bool, str, pd.Series]:
    """Run the generated factor on sample data. Returns (ok, message, output)."""
    fields = factor.required_fields() or ["close"]
    data = make_sample_data(fields, n=n)

    mod = _load_module(code_path, f"gen_{factor.name}")
    fn = getattr(mod, factor.name, None)
    if fn is None:
        return False, f"function '{factor.name}' not found in generated module", pd.Series()

    # map data fields -> function kwargs. CGO-style uses close/turnover/price.
    kwargs = {}
    if "close" in data:
        kwargs["close"] = data["close"]
    if "turnover" in data:
        kwargs["turnover"] = data["turnover"]
    if "vwap" in data:
        kwargs["price"] = data["vwap"]
    # generic fallback: pass all fields
    if factor.operator == "generic":
        kwargs = data

    try:
        out = fn(**kwargs)
    except NotImplementedError as e:
        return False, f"generic skeleton (expected): {e}", pd.Series()
    except Exception as e:  # noqa: BLE001
        return False, f"runtime error: {type(e).__name__}: {e}", pd.Series()

    out = pd.Series(out)
    finite = out.replace([np.inf, -np.inf], np.nan).dropna()
    if len(finite) == 0:
        return False, "output is all-NaN/empty", out
    if finite.std() < 1e-12:
        return False, "output is degenerate (zero variance)", out
    coverage = len(finite) / len(out)
    return True, f"OK — {len(finite)}/{len(out)} finite " \
                 f"(mean {finite.mean():.4f}, std {finite.std():.4f})", out


# ---------------------------------------------------------------------------
# company core.py validation
# ---------------------------------------------------------------------------
def make_company_sample_data(fields, n_stocks: int = 8, n_days: int = 300,
                             seed: int = 11) -> pd.DataFrame:
    """Synthesize a daily panel with the aliased columns core.py consumes.

    Columns: InnerID, DataDate + one column per bound data field
    (close/turnover/vwap/...), mimicking read_daily_data's SELECT output.
    """
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n_days)
    frames = []
    for i in range(n_stocks):
        rets = rng.normal(0.0004, 0.015, n_days)
        close = 20.0 * np.exp(np.cumsum(rets))
        df = pd.DataFrame({
            "InnerID": 1000 + i,
            "DataDate": idx,
            "close": close,
        })
        if "turnover" in fields:
            df["turnover"] = np.clip(rng.gamma(2.0, 0.005, n_days), 1e-4, 0.9)
        if "vwap" in fields:
            df["vwap"] = close * (1.0 + rng.normal(0.0, 0.002, n_days))
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


class _DuckDBStub(types.ModuleType):
    """Minimal duckdb stand-in for the build_signal_data join query."""

    def __init__(self) -> None:
        super().__init__("duckdb")
        self.frames: Dict[str, pd.DataFrame] = {}

    def sql(self, query: str):
        frames = self.frames

        class _Result:
            def df(self) -> pd.DataFrame:
                out = frames["daily_security_info"].merge(
                    frames["factor_data"], on="InnerID", how="left")
                return out.rename(columns={"SecCode": "runner_code"}) \
                          .filter(items=["runner_code", "runner_value"])
        return _Result()


@contextlib.contextmanager
def _stub_company_modules() -> Iterator[Tuple[types.ModuleType, _DuckDBStub]]:
    """Install stub `function`/`duckdb` modules; restore afterwards."""
    func_stub = types.ModuleType("function")
    func_stub.RAWDATA = None

    def _unavailable(*a, **kw):
        raise RuntimeError("company data layer unavailable in validation")

    for fn in ("read_sql", "read_ob", "read_pg", "read_trading_date",
               "read_tradingdays", "read_daily_security_info",
               "read_daily_runnercode", "read_untradable", "save_signal"):
        setattr(func_stub, fn, _unavailable)

    duck_stub = _DuckDBStub()
    saved = {name: sys.modules.get(name) for name in ("function", "duckdb")}
    sys.modules["function"] = func_stub
    sys.modules["duckdb"] = duck_stub
    try:
        yield func_stub, duck_stub
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


def validate_corepy(code_path: str, factor: Factor,
                    runner_id: int = None,
                    n_stocks: int = 8, n_days: int = 300
                    ) -> Tuple[bool, str, pd.DataFrame]:
    """Validate a generated company core.py on synthetic daily data.

    Checks: (1) module compiles and exposes the required house structure,
    (2) SIGNAL_FILE/SIGNAL_CHECK_FILE carry the runner id, (3) for implemented
    operators calc_factor_data + build_signal_data run end-to-end on a
    synthetic panel and produce a non-degenerate runner_value.
    """
    required = ("MIN_VALID_VALUE", "SIGNAL_FILE", "SIGNAL_CHECK_FILE",
                "get_signal", "main")
    with open(code_path, encoding="utf-8") as f:
        source = f.read()
    try:
        compile(source, code_path, "exec")
    except SyntaxError as e:
        return False, f"syntax error: {e}", pd.DataFrame()

    with _stub_company_modules() as (_func, duck):
        mod = _load_module(code_path, f"core_{factor.name}")
        missing = [n for n in required if not hasattr(mod, n)]
        if missing:
            return False, f"missing company structure: {missing}", pd.DataFrame()
        if runner_id is not None:
            if f"runner_value_{runner_id}.json" != mod.SIGNAL_FILE:
                return False, (f"SIGNAL_FILE {mod.SIGNAL_FILE!r} does not "
                               f"carry runner_id {runner_id}"), pd.DataFrame()

        op = factor.operator or "generic"
        fields = _OP_FIELDS.get(op) or factor.required_fields()
        daily_data = make_company_sample_data(fields, n_stocks=n_stocks,
                                              n_days=n_days)
        try:
            factor_data = mod.calc_factor_data(daily_data)
        except NotImplementedError as e:
            return False, f"generic skeleton (expected): {e}", pd.DataFrame()
        except Exception as e:  # noqa: BLE001
            return False, f"calc error: {type(e).__name__}: {e}", pd.DataFrame()

        if not {"InnerID", "DataDate", "runner_value"} <= set(factor_data.columns):
            return False, ("calc output must have InnerID/DataDate/"
                           "runner_value"), factor_data
        finite = factor_data["runner_value"].replace(
            [np.inf, -np.inf], np.nan).dropna()
        if len(finite) == 0:
            return False, "runner_value all-NaN", factor_data
        if finite.std() < 1e-12:
            return False, "runner_value degenerate (zero variance)", factor_data

        duck.frames["factor_data"] = factor_data
        duck.frames["daily_security_info"] = pd.DataFrame({
            "InnerID": factor_data["InnerID"],
            "SecCode": [f"6{i:05d}" for i in factor_data["InnerID"]],
        })
        signal = mod.build_signal_data(factor_data,
                                       duck.frames["daily_security_info"])
        if list(signal.columns) != ["runner_code", "runner_value"]:
            return False, f"signal columns {list(signal.columns)}", signal
        if signal["runner_value"].isna().all():
            return False, "signal runner_value all-NaN after join", signal

        msg = (f"OK — {len(finite)}/{len(factor_data)} stocks "
               f"(mean {finite.mean():.4f}, std {finite.std():.4f})")
        return True, msg, signal
