"""F03 parser L 欄（規範參考）擴充測試（p3-02 §4.5 / AC-15、AC-16）。

驗證：
- L 欄讀出 → F03ChecklistItem.regulation_ref_raw（寫入值覆蓋官方模板預填）；
- L 欄清空（空字串 → clean() → None）→ regulation_ref_raw 為 None；
- 既有欄位（item_id/lifecycle/topic/description/check_state/evidence_*）解析回歸不變。

fixture 一律以 tests/fixture_builder.build_f03_fixture 由官方模板程式化生成，寫到暫存路徑，
絕不寫回 data/original。
"""

from __future__ import annotations

from pathlib import Path

from govcheck.models.f03 import F03ChecklistItem
from govcheck.parsers.f03_parser import parse_f03_checklist

from tests.fixture_builder import build_f03_fixture


def test_l_col_read_written_value(tmp_path: Path) -> None:
    """寫入 L 欄 → regulation_ref_raw 讀出對應值（覆蓋模板預填）。"""
    f03 = tmp_path / "f03.xlsx"
    build_f03_fixture(f03, items=[
        {"row": 5, "state": "是", "proposal": "P5", "golive": "G5", "regulation_ref": "R03 五/(二)/2"},
    ])
    checklist = parse_f03_checklist(f03)
    by_id = {it.item_id: it for it in checklist.items}
    assert by_id["1-1"].regulation_ref_raw == "R03 五/(二)/2"


def test_l_col_empty_is_none(tmp_path: Path) -> None:
    """L 欄清空（空字串 → clean → None）→ regulation_ref_raw 為 None（AC-15 未填分支）。"""
    f03 = tmp_path / "f03.xlsx"
    build_f03_fixture(f03, items=[{"row": 5, "regulation_ref": ""}])  # 空字串清除模板預填值
    checklist = parse_f03_checklist(f03)
    by_id = {it.item_id: it for it in checklist.items}
    assert by_id["1-1"].regulation_ref_raw is None


def test_model_default_regulation_ref_is_none() -> None:
    """未提供 regulation_ref_raw 時 model 預設為 None（AC-15 型別預設）。"""
    item = F03ChecklistItem(item_id="9-9")
    assert item.regulation_ref_raw is None


def test_existing_columns_regression_unchanged(tmp_path: Path) -> None:
    """新增 L 欄不影響既有欄位解析（AC-16 回歸）。"""
    f03 = tmp_path / "f03.xlsx"
    build_f03_fixture(f03, items=[
        {"row": 5, "state": "是", "proposal": "PROP_A", "golive": "GOL_A", "regulation_ref": "R03"},
        {"row": 6, "state": "否", "proposal": "PROP_B", "golive": None},
        {"row": 7, "state": "不適用"},
    ])
    checklist = parse_f03_checklist(f03)
    by_id = {it.item_id: it for it in checklist.items}

    # item_id / topic / description / lifecycle 為模板固定值；只斷言可解析且型別正確
    assert set(by_id) >= {"1-1", "1-2", "1-3"}
    assert all(isinstance(it.item_id, str) for it in checklist.items)

    # check_state 由 F/G/H 推得
    assert by_id["1-1"].check_state == "是"
    assert by_id["1-2"].check_state == "否"
    assert by_id["1-3"].check_state == "不適用"

    # 兩段佐證欄不受 L 欄擴充影響
    assert by_id["1-1"].evidence_proposal == "PROP_A"
    assert by_id["1-1"].evidence_golive == "GOL_A"
    assert by_id["1-2"].evidence_proposal == "PROP_B"
    assert by_id["1-2"].evidence_golive is None

    # lifecycle / topic / description 為官方模板文字，存在且為 str|None（不硬編模板內容）
    assert by_id["1-1"].topic is not None
    assert by_id["1-1"].description is not None
    assert by_id["1-1"].lifecycle is not None
