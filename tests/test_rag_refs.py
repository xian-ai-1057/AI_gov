"""rag/refs 測試（p3-02 spec §4.1；golden 直接消費 contracts/fixtures/）。

涵蓋 p3-02 AC-1..8 的 T1 責任範圍（refs 解析 + prefix 比對 + per-ref cap），
以及 RetrievalMap fixture 對 schema 的驗證（p3-02 AC-9/10/11 之 models 部分）。
"""

import json
import logging
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from govcheck.rag.models import RegulationRef, RetrievalMap
from govcheck.rag.refs import (
    filter_chunks_by_ref,
    normalize_section_path,
    parse_regulation_refs,
    ref_matches_chunk,
)

ROOT = Path(__file__).resolve().parents[1]
P302_FIXTURES = ROOT / "specs" / "p3-02-canonical-map" / "contracts" / "fixtures"

with (P302_FIXTURES / "refs_cases.yaml").open(encoding="utf-8") as _fh:
    _REFS_CASES = yaml.safe_load(_fh)["cases"]

with (P302_FIXTURES / "prefix_match_cases.yaml").open(encoding="utf-8") as _fh:
    _PREFIX_DOC = yaml.safe_load(_fh)


@pytest.fixture
def debug_caplog(caplog):
    """使 govcheck.* 的 DEBUG record 可被 caplog 捕捉（logging_setup 對 ops logger
    設 propagate=False；此處臨時開回 propagate 並降 level）。"""
    gov = logging.getLogger("govcheck")
    old_propagate = gov.propagate
    gov.propagate = True
    caplog.set_level(logging.DEBUG, logger="govcheck")
    yield caplog
    gov.propagate = old_propagate


@pytest.mark.parametrize("case", _REFS_CASES, ids=[c["name"] for c in _REFS_CASES])
def test_parse_refs_table_driven(case):
    """p3-02 AC-1（表驅動全 14 案）＋ AC-2（正規化）＋ AC-3（殘段退整部規範）
    ＋ AC-4（共用 path 展開）＋ AC-5（多 entry）：
    parse_regulation_refs(input) 的 [(reg_code, section_path_prefix)] == expected（保序）。"""
    result = parse_regulation_refs(case["input"])
    assert [(r.reg_code, r.section_path_prefix) for r in result] == [
        (e["reg_code"], e["section_path_prefix"]) for e in case["expected"]
    ]


@pytest.mark.parametrize(
    "name", ["garbage_no_reg_code_dropped", "mixed_valid_entry_and_garbage_entry"]
)
def test_parse_refs_dropped_count_and_privacy(name, debug_caplog):
    """p3-02 AC-6：丟棄殘段只 DEBUG 記計數（refs dropped n=N），且 caplog 不含 input 原文。"""
    case = next(c for c in _REFS_CASES if c["name"] == name)
    result = parse_regulation_refs(case["input"])
    assert len(result) == len(case["expected"])

    dropped_msgs = [r.getMessage() for r in debug_caplog.records if "refs dropped" in r.getMessage()]
    assert dropped_msgs == [f"refs dropped n={case['expect_dropped']}"]
    # 隱私：任何 log record 不含 input 原文（含正規化前的殘段文字）
    for record in debug_caplog.records:
        msg = record.getMessage()
        assert "請提案單位自行補充" not in msg
        assert "與本項無關的文字" not in msg


def test_parse_refs_no_drop_log_when_clean(debug_caplog):
    """p3-02 AC-6 邊界：無丟棄（expect_dropped=0）時不發 dropped log。"""
    parse_regulation_refs("R03/五/(二)/2")
    assert not any("refs dropped" in r.getMessage() for r in debug_caplog.records)


def test_normalize_section_path_fullwidth():
    """p3-02 AC-2：正規化統一入口——全形→半形、去空白、（二）→(二)。"""
    assert normalize_section_path("Ｒ０３／五／（二）／２") == "R03/五/(二)/2"
    assert normalize_section_path(" 五 / (二) ") == "五/(二)"
    assert normalize_section_path("（二）") == "(二)"


@pytest.mark.parametrize(
    "case",
    _PREFIX_DOC["match_cases"],
    ids=[c["name"] for c in _PREFIX_DOC["match_cases"]],
)
def test_ref_matches_chunk_table_driven(case):
    """p3-02 AC-7：prefix 雙向比對布林表驅動（含 partial_token_not_prefix 分段比對案）。"""
    ref = RegulationRef(**case["ref"])
    assert (
        ref_matches_chunk(ref, case["chunk"]["reg_code"], case["chunk"]["section_path"])
        is case["expected"]
    )


@pytest.mark.parametrize(
    "case", _PREFIX_DOC["cap_cases"], ids=[c["name"] for c in _PREFIX_DOC["cap_cases"]]
)
def test_ref_cap_table_driven(case):
    """p3-02 AC-8：展開命中依 order 升冪取前 3 的路徑集合 == expected_matched_paths。"""
    ref = RegulationRef(**case["ref"])
    rows = [dict(c) for c in case["chunks"]]
    matched = filter_chunks_by_ref(ref, rows, cap=3)
    assert [c["section_path"] for c in matched] == case["expected_matched_paths"]


def test_retrieval_map_fixture_round_trip():
    """p3-02 AC-9/AC-10（models 部分）：retrieval_map_example.json 通過 model_validate
    且 model_dump(mode="json") 往返一致；minimal（空 map）合法。"""
    example = json.loads((P302_FIXTURES / "retrieval_map_example.json").read_text(encoding="utf-8"))
    rm = RetrievalMap.model_validate(example)
    assert RetrievalMap.model_validate(rm.model_dump(mode="json")) == rm

    minimal = json.loads((P302_FIXTURES / "retrieval_map_minimal.json").read_text(encoding="utf-8"))
    assert RetrievalMap.model_validate(minimal).f03_items == {}


def test_retrieval_map_extra_forbid():
    """p3-02 AC-11（models 部分）：example 頂層注入未知鍵 → ValidationError（extra="forbid"）。"""
    example = json.loads((P302_FIXTURES / "retrieval_map_example.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError):
        RetrievalMap.model_validate({**example, "unknown_key": 1})
