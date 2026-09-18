"""factor-forge: research-report factor brief -> JSON -> executable code.

Pipeline:
    extract  : factor brief (text)  -> Factor (structured JSON)
    codegen  : Factor               -> Python source
    validator: generated code       -> runs on sample data (sanity check)

Company pipeline (signal library):
    webinput : URL (e.g. fund.eastmoney.com) -> factor brief text
    datadict : datadict.example.com      -> company data-name bindings
    corepy   : Factor               -> company core.py + repo scaffold
"""

from .schema import Factor, FactorVariable, DATA_FIELDS
from .extractor import extract
from .codegen import generate, generate_to_file
from .validator import validate_code, make_sample_data, validate_corepy
from .webinput import (is_url, fetch_url, fetch_bytes, html_to_text,
                       url_basename, load_brief_from_path)
from .datadict import (DataDictClient, DictBinding, BUILTIN_BINDINGS,
                       resolve_factor_fields)
from .corepy import generate_corepy, generate_repo, factor_to_company_spec
from .pdfinput import extract_pdf_text, find_factor_briefs, pdf_to_briefs
from .sources import (Source, ReportEntry, load_registry, fetch_entries,
                      download_entry, load_state, save_state)

__all__ = [
    "Factor", "FactorVariable", "DATA_FIELDS",
    "extract", "generate", "generate_to_file",
    "validate_code", "make_sample_data", "validate_corepy",
    "is_url", "fetch_url", "fetch_bytes", "html_to_text", "url_basename",
    "load_brief_from_path",
    "DataDictClient", "DictBinding", "BUILTIN_BINDINGS",
    "resolve_factor_fields",
    "generate_corepy", "generate_repo", "factor_to_company_spec",
    "extract_pdf_text", "find_factor_briefs", "pdf_to_briefs",
    "Source", "ReportEntry", "load_registry", "fetch_entries",
    "download_entry", "load_state", "save_state",
]

__version__ = "0.3.0"
