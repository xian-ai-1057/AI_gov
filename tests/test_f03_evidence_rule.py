"""F03 佐證缺漏規則檢查的正/反例（確定性、無網路）。"""

from __future__ import annotations

from govcheck.checks.rule import f03_evidence_presence
from govcheck.models import F03Checklist, F03ChecklistItem, Severity, WorkflowStage


def _item(**kw) -> F03ChecklistItem:
    base = {"item_id": "1-1", "topic": "公平性"}
    base.update(kw)
    return F03ChecklistItem(**base)


def _codes(findings) -> set[str]:
    return {f.code for f in findings}


def test_done_but_proposal_blank_warns():
    cl = F03Checklist(items=[_item(check_state="是", evidence_proposal=None, evidence_golive="已說明")])
    fs = f03_evidence_presence.run_all(cl)
    assert "F03.EVIDENCE_MISSING_PROPOSAL" in _codes(fs)
    warn = next(f for f in fs if f.code == "F03.EVIDENCE_MISSING_PROPOSAL")
    assert warn.severity is Severity.WARN
    assert "1-1" in warn.location


def test_done_but_golive_blank_is_info():
    cl = F03Checklist(items=[_item(check_state="是", evidence_proposal="具體佐證說明", evidence_golive=None)])
    fs = f03_evidence_presence.run_all(cl, workflow_stage=WorkflowStage.ACCEPTANCE)
    assert "F03.EVIDENCE_MISSING_GOLIVE" in _codes(fs)
    assert "F03.EVIDENCE_MISSING_PROPOSAL" not in _codes(fs)
    info = next(f for f in fs if f.code == "F03.EVIDENCE_MISSING_GOLIVE")
    assert info.severity is Severity.INFO


def test_proposal_stage_does_not_require_golive():
    cl = F03Checklist(items=[_item(
        check_state="是", evidence_proposal="具體佐證說明", evidence_golive=None
    )])
    fs = f03_evidence_presence.run_all(cl, workflow_stage=WorkflowStage.PROPOSAL)
    assert "F03.EVIDENCE_MISSING_GOLIVE" not in _codes(fs)


def test_done_with_both_filled_is_clean():
    cl = F03Checklist(items=[_item(check_state="是", evidence_proposal="A", evidence_golive="B")])
    assert f03_evidence_presence.run_all(cl) == []


def test_undone_states_do_not_require_evidence():
    """勾「否」或未勾選＝尚未檢核，不強制附佐證（不適用另有理由要求，見下）。"""
    cl = F03Checklist(items=[
        _item(item_id="2-2", check_state="否"),
        _item(item_id="2-3", check_state=None),
    ])
    assert f03_evidence_presence.run_all(cl) == []


def test_not_applicable_without_reason_warns():
    """不適用的項次不進語意判讀，人工是唯一把關；沒寫理由就無從確認主張成立。"""
    cl = F03Checklist(items=[_item(item_id="2-1", check_state="不適用")])
    fs = f03_evidence_presence.run_all(cl)
    assert _codes(fs) == {"F03.NOT_APPLICABLE_NO_REASON"}
    assert fs[0].severity is Severity.WARN
    assert "2-1" in fs[0].location


def test_not_applicable_with_reason_is_clean():
    """把不適用理由寫在佐證欄是正確做法，不可反過來當成缺失警示。"""
    cl = F03Checklist(items=[
        _item(item_id="2-1", check_state="不適用", evidence_proposal="本系統不涉及該情境"),
    ])
    assert f03_evidence_presence.run_all(cl) == []


def test_not_applicable_never_requires_golive_evidence():
    """不適用項次在上線階段也只要求理由，不得再要求上線佐證。"""
    cl = F03Checklist(items=[
        _item(item_id="2-1", check_state="不適用", evidence_proposal="無此議題"),
    ])
    fs = f03_evidence_presence.run_all(cl, workflow_stage=WorkflowStage.PRODUCTION)
    assert fs == []


def test_absent_sheet_returns_empty():
    assert f03_evidence_presence.run_all(F03Checklist(sheet_present=False)) == []
