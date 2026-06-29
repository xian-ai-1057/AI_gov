"""F03 佐證缺漏規則檢查的正/反例（確定性、無網路）。"""

from __future__ import annotations

from govcheck.checks.rule import f03_evidence_presence
from govcheck.models import F03Checklist, F03ChecklistItem, Severity


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
    fs = f03_evidence_presence.run_all(cl)
    assert "F03.EVIDENCE_MISSING_GOLIVE" in _codes(fs)
    assert "F03.EVIDENCE_MISSING_PROPOSAL" not in _codes(fs)
    info = next(f for f in fs if f.code == "F03.EVIDENCE_MISSING_GOLIVE")
    assert info.severity is Severity.INFO


def test_done_with_both_filled_is_clean():
    cl = F03Checklist(items=[_item(check_state="是", evidence_proposal="A", evidence_golive="B")])
    assert f03_evidence_presence.run_all(cl) == []


def test_not_done_states_not_required():
    cl = F03Checklist(items=[
        _item(item_id="2-1", check_state="不適用"),
        _item(item_id="2-2", check_state="否"),
        _item(item_id="2-3", check_state=None),
    ])
    assert f03_evidence_presence.run_all(cl) == []


def test_absent_sheet_returns_empty():
    assert f03_evidence_presence.run_all(F03Checklist(sheet_present=False)) == []
