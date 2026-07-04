"""rag/mapping.py — RetrievalMap loader + format_section_ref。

AC coverage（p3-02 spec §6 / p3-03 spec §4.5）：
- AC p3-02 AC-9   : RetrievalMap round-trip（example fixture）
- AC p3-02 AC-10  : minimal fixture 通過 model_validate
- AC p3-02 AC-11  : extra key → ValidationError（extra="forbid"）
- AC p3-02 AC-12  : loader 缺檔 → RetrievalMapError
- AC p3-02 AC-13  : loader 版本不符 → RetrievalMapError
- AC p3-03 AC-16  : format_section_ref 三形態（章節式 / 條文式 / 單段）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from govcheck.rag.mapping import RetrievalMapError, format_section_ref, load_retrieval_map
from govcheck.rag.models import RetrievalMap

# Fixture 目錄（p3-02 合約）
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "specs" / "p3-02-canonical-map" / "contracts" / "fixtures"

EXAMPLE_JSON = FIXTURE_DIR / "retrieval_map_example.json"
MINIMAL_JSON = FIXTURE_DIR / "retrieval_map_minimal.json"
BAD_VERSION_JSON = FIXTURE_DIR / "retrieval_map_bad_version.json"


# ─────────────────────────────────────────────────────────────────────────────
# format_section_ref（AC p3-03 AC-16）
# ─────────────────────────────────────────────────────────────────────────────


def test_format_section_ref_chapter_style():
    """AC p3-03 AC-16：章節式 五/(二)/2 → R03 五、(二) 2。"""
    assert format_section_ref("R03", "五/(二)/2") == "R03 五、(二) 2"


def test_format_section_ref_article_style():
    """AC p3-03 AC-16：條文式 第一條 → R01 第一條（單段直接接合）。"""
    assert format_section_ref("R01", "第一條") == "R01 第一條"


def test_format_section_ref_single_segment_chapter():
    """AC p3-03 AC-16：單段非條文式（如 五）→ R02 五（直接接合，無「、」）。"""
    assert format_section_ref("R02", "五") == "R02 五"


def test_format_section_ref_two_levels():
    """章節式兩層：四/(一) → R04 四、(一)（括號層直接接，無前置空格）。"""
    assert format_section_ref("R04", "四/(一)") == "R04 四、(一)"


def test_format_section_ref_empty_path():
    """空 section_path → 只回 reg_code（整部規範 fallback）。"""
    assert format_section_ref("R05", "") == "R05"


def test_format_section_ref_three_levels_arabic():
    """章節式三層含阿拉伯數字：三/(一)/1 → R06 三、(一) 1。"""
    assert format_section_ref("R06", "三/(一)/1") == "R06 三、(一) 1"


# ─────────────────────────────────────────────────────────────────────────────
# RetrievalMap round-trip（AC p3-02 AC-9）
# ─────────────────────────────────────────────────────────────────────────────


def test_retrieval_map_example_round_trip():
    """AC p3-02 AC-9：model_validate(example) 成功；re-dump 再 validate 不失真。"""
    data = json.loads(EXAMPLE_JSON.read_text(encoding="utf-8"))
    m = RetrievalMap.model_validate(data)
    assert "1-1" in m.f03_items
    assert "UC-01" in m.f02_questions
    # round-trip
    dumped = m.model_dump(mode="json")
    m2 = RetrievalMap.model_validate(dumped)
    assert m == m2


# ─────────────────────────────────────────────────────────────────────────────
# minimal fixture（AC p3-02 AC-10）
# ─────────────────────────────────────────────────────────────────────────────


def test_retrieval_map_minimal_is_valid():
    """AC p3-02 AC-10：空 map（f03_items={}, f02_questions={}）合法。"""
    data = json.loads(MINIMAL_JSON.read_text(encoding="utf-8"))
    m = RetrievalMap.model_validate(data)
    assert m.f03_items == {}
    assert m.f02_questions == {}


# ─────────────────────────────────────────────────────────────────────────────
# extra key 禁止（AC p3-02 AC-11）
# ─────────────────────────────────────────────────────────────────────────────


def test_retrieval_map_extra_key_raises():
    """AC p3-02 AC-11：頂層注入未知鍵 → ValidationError（extra="forbid"）。"""
    data = json.loads(EXAMPLE_JSON.read_text(encoding="utf-8"))
    data["unknown_field_xyz"] = "should_fail"
    with pytest.raises(ValidationError):
        RetrievalMap.model_validate(data)


# ─────────────────────────────────────────────────────────────────────────────
# loader 缺檔（AC p3-02 AC-12）
# ─────────────────────────────────────────────────────────────────────────────


def test_load_retrieval_map_missing_file():
    """AC p3-02 AC-12：缺檔 → RetrievalMapError（訊息含路徑，不含內容）。"""
    with pytest.raises(RetrievalMapError) as exc_info:
        load_retrieval_map("/nonexistent/path/retrieval_map.json")
    assert "/nonexistent/path/retrieval_map.json" in str(exc_info.value)


# ─────────────────────────────────────────────────────────────────────────────
# loader 版本不符（AC p3-02 AC-13）
# ─────────────────────────────────────────────────────────────────────────────


def test_load_retrieval_map_bad_version():
    """AC p3-02 AC-13：schema_version=999 → RetrievalMapError（版本不符）。"""
    with pytest.raises(RetrievalMapError) as exc_info:
        load_retrieval_map(BAD_VERSION_JSON)
    msg = str(exc_info.value)
    assert "版本不符" in msg or "999" in msg


# ─────────────────────────────────────────────────────────────────────────────
# loader 正常載入（整合驗證）
# ─────────────────────────────────────────────────────────────────────────────


def test_load_retrieval_map_example_ok():
    """loader 正常載入 example fixture，回傳 RetrievalMap 實例。"""
    mapping = load_retrieval_map(EXAMPLE_JSON)
    assert isinstance(mapping, RetrievalMap)
    assert "1-1" in mapping.f03_items


def test_load_retrieval_map_minimal_ok():
    """loader 正常載入 minimal fixture（空 map 合法）。"""
    mapping = load_retrieval_map(MINIMAL_JSON)
    assert mapping.f03_items == {}
    assert mapping.f02_questions == {}
