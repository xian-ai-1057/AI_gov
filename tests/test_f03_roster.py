"""檢核項名冊：確保「沒被語意判讀的項次」仍然出現在報告裡。

背景：分項評估表只涵蓋送進判讀的項次。實測某份真實送件包 20 項中有 9 項標示不適用，
這 9 項既無 assessment 也無 finding，整列從總覽表消失（連「全部」濾鏡都看不到），
治理人員無從得知提案單位主張了哪些項目不適用。名冊就是補這個洞。
"""

from __future__ import annotations

import pytest

from govcheck.checks.llm import f03_assessment
from govcheck.models import (
    F03Checklist,
    F03ChecklistItem,
    ReviewReport,
    WorkflowStage,
)
from govcheck.report.builder import to_markdown


def _item(**kw) -> F03ChecklistItem:
    base = {"item_id": "1-1", "topic": "公平性"}
    base.update(kw)
    return F03ChecklistItem(**base)


_MIXED = F03Checklist(items=[
    _item(item_id="1-1", check_state="是", evidence_proposal="已執行偏誤測試"),
    _item(item_id="1-2", check_state="不適用", evidence_proposal="本系統不涉及該情境"),
    _item(item_id="1-3", check_state="不適用"),
    _item(item_id="1-4", check_state="否"),
    _item(item_id="1-5", check_state=None),
    _item(item_id="1-6", check_state="否", evidence_golive="上線後補的說明"),
])


@pytest.mark.parametrize("stage", list(WorkflowStage))
def test_roster_covers_every_checklist_item(stage) -> None:
    """名冊筆數必須等於檢核項總數 —— 這正是「全部 N」要能名符其實的前提。"""
    roster = _MIXED.roster(stage)
    assert [row.item_id for row in roster] == [item.item_id for item in _MIXED.items]


@pytest.mark.parametrize("stage", list(WorkflowStage))
def test_assessable_coverage_matches_llm_targets_exactly(stage) -> None:
    """名冊的 assessable 與 LLM 送審清單必須逐項一致。

    兩者若各自判斷，就會出現「名冊說可判讀、卻查無 assessment」的矛盾列。
    """
    assessable = {row.item_id for row in _MIXED.roster(stage) if row.coverage == "assessable"}
    targets = {item.item_id for item in f03_assessment._targets(_MIXED, stage)}
    assert assessable == targets


def test_proposal_stage_ignores_golive_only_evidence() -> None:
    """提案階段只看提案佐證：只填了上線佐證的項次不算可判讀。"""
    by_id = {row.item_id: row for row in _MIXED.roster(WorkflowStage.PROPOSAL)}
    assert by_id["1-6"].coverage == "no_evidence"
    assert by_id["1-1"].coverage == "assessable"


def test_later_stages_accept_golive_only_evidence() -> None:
    by_id = {row.item_id: row for row in _MIXED.roster(WorkflowStage.GOLIVE)}
    assert by_id["1-6"].coverage == "assessable"


def test_not_applicable_wins_over_evidence_presence() -> None:
    """標示不適用即不判讀，佐證欄寫了理由也一樣；理由本身要帶進報告供人工認定。"""
    by_id = {row.item_id: row for row in _MIXED.roster(WorkflowStage.PROPOSAL)}
    assert by_id["1-2"].coverage == "not_applicable"
    assert by_id["1-2"].reason == "本系統不涉及該情境"
    assert by_id["1-3"].coverage == "not_applicable"
    assert by_id["1-3"].reason is None


def test_reason_is_truncated() -> None:
    """不適用理由是提案單位自由填寫的欄位，不能讓整段長文無上限灌進 API payload。"""
    checklist = F03Checklist(items=[
        _item(check_state="不適用", evidence_proposal="理" * 500),
    ])
    assert len(checklist.roster(WorkflowStage.PROPOSAL)[0].reason) == 200


def test_roster_carries_source_text_for_manual_cross_check() -> None:
    """原文要進報告：治理人員必須能拿 AI 判讀去對照提案單位實際填的字。"""
    checklist = F03Checklist(items=[_item(
        description="是否已完成偏誤測試", check_state="是",
        evidence_proposal="已於 UAT 完成偏誤測試", evidence_golive="上線前重跑偏誤測試",
    )])
    row = checklist.roster(WorkflowStage.GOLIVE)[0]
    assert row.description == "是否已完成偏誤測試"
    assert row.evidence_proposal_text == "已於 UAT 完成偏誤測試"
    assert row.evidence_golive_text == "上線前重跑偏誤測試"


def test_proposal_stage_roster_hides_golive_text() -> None:
    """提案階段不看 J 欄：帶出上線佐證原文會讓人誤以為 AI 已審過那段。"""
    checklist = F03Checklist(items=[_item(
        check_state="是", evidence_proposal="提案說明", evidence_golive="上線說明",
    )])
    row = checklist.roster(WorkflowStage.PROPOSAL)[0]
    assert row.evidence_proposal_text == "提案說明"
    assert row.evidence_golive_text is None
    assert row.golive_diff is None


def test_source_text_is_truncated() -> None:
    """佐證說明是自由填寫欄位，與不適用理由一樣不能無上限灌進 API payload。"""
    checklist = F03Checklist(items=[_item(
        description="檢" * 900, check_state="是", evidence_proposal="佐" * 900,
    )])
    row = checklist.roster(WorkflowStage.PROPOSAL)[0]
    assert len(row.description) == 600
    assert len(row.evidence_proposal_text) == 600


@pytest.mark.parametrize(
    ("proposal", "golive", "expected"),
    [
        ("提案時的做法", None, "missing"),
        ("提案時的做法", "   ", "missing"),
        ("提案時的做法", "提案時的做法", "same"),
        ("提案時的做法", " 提案時 的做法 ", "same"),   # 只有排版差異不算內容差異
        ("提案時的做法", "上線後改用新做法", "changed"),
        (None, "上線後才補的做法", "changed"),          # 提案沒寫、上線才有＝差異
    ],
)
def test_golive_diff_is_deterministic(proposal, golive, expected) -> None:
    """上線階段的「與提案差異」是規則式文字比對，不經模型，可逐例驗證。"""
    checklist = F03Checklist(items=[_item(
        check_state="是", evidence_proposal=proposal, evidence_golive=golive,
    )])
    assert checklist.roster(WorkflowStage.GOLIVE)[0].golive_diff == expected


def test_markdown_lists_every_unassessed_item() -> None:
    report = ReviewReport(
        form_type="送件包",
        checklist_roster=_MIXED.roster(WorkflowStage.PROPOSAL),
    )
    md = to_markdown(report)
    assert "檢核項共 6 項，其中 1 項送語意判讀、5 項未送" in md
    for item_id in ("1-2", "1-3", "1-4", "1-5", "1-6"):
        assert f"| {item_id} |" in md
    assert "本系統不涉及該情境" in md
    assert "標示不適用" in md and "佐證欄空白" in md


def test_markdown_omits_coverage_section_without_f03() -> None:
    """無 F03 的送件包（例如 F02-only）不該憑空多出一個空的涵蓋範圍章節。"""
    assert "檢核表涵蓋範圍" not in to_markdown(ReviewReport(form_type="送件包"))
