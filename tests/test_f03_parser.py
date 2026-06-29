"""F03 檢核表完整解析的 ground-truth 測試（依官方範本實地結構）。"""

from __future__ import annotations

import pytest
from tests.fixture_builder import OFFICIAL_F03, build_f03_fixture

from govcheck.parsers.f03_parser import parse_f03_checklist

pytestmark = pytest.mark.skipif(
    not OFFICIAL_F03.exists(), reason="官方 F03 範本不存在（data/original 未連結）"
)


def test_parse_structure(tmp_path):
    f = build_f03_fixture(
        tmp_path / "f03.xlsx",
        system_owner="李大華",
        items=[
            {"row": 5, "state": "是", "proposal": "已完成適法性評估，附法遵意見書 #2026-001",
             "golive": "與提案一致，無差異"},
            {"row": 6, "state": "否"},
            {"row": 11, "state": "不適用"},
        ],
    )
    cl = parse_f03_checklist(f)

    assert cl.sheet_present is True
    assert cl.subject == "李大華"
    assert len(cl.items) == 20  # 資料列 5~24（列25空、列26備註被略過）

    by_id = {it.item_id: it for it in cl.items}
    assert {"1-1", "1-6", "2-1", "3-1", "4-5", "5-3"} <= set(by_id)

    # 生命週期合併儲存格 → forward-fill
    assert by_id["1-1"].lifecycle == "通用性原則"
    assert by_id["1-3"].lifecycle == "通用性原則"
    assert by_id["2-1"].lifecycle == "系統規劃及設計"
    assert by_id["4-3"].lifecycle == "模型建立及驗證"
    assert by_id["5-3"].lifecycle == "系統部署及監控"

    # 勾選態（F/G/H → 是/否/不適用）
    assert by_id["1-1"].check_state == "是" and by_id["1-1"].is_done
    assert by_id["1-2"].check_state == "否"
    assert by_id["2-1"].check_state == "不適用" and not by_id["2-1"].is_done

    # 兩段佐證
    assert by_id["1-1"].evidence_proposal.startswith("已完成適法性評估")
    assert by_id["1-1"].evidence_golive == "與提案一致，無差異"

    # 未填列：無勾選、無佐證、仍保留 topic/description 上下文
    assert by_id["3-1"].check_state is None
    assert by_id["3-1"].evidence_proposal is None
    assert by_id["3-1"].topic
    assert by_id["3-1"].description


def test_default_owner_only(tmp_path):
    """不帶 items 時仍可解析全部 20 項，且皆為未填。"""
    f = build_f03_fixture(tmp_path / "f03.xlsx")
    cl = parse_f03_checklist(f)
    assert len(cl.items) == 20
    assert all(it.check_state is None for it in cl.items)
    assert all(it.evidence_proposal is None and it.evidence_golive is None for it in cl.items)
