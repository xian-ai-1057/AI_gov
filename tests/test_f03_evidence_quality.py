"""F03 佐證品質規則檢查的正/反例（確定性、無網路）。

門檻邊界逐一釘住：字數門檻是治理人員會調的參數，邊界寫錯會讓整批送件包誤判。
"""

from __future__ import annotations

from govcheck.checks.rule import f03_evidence_quality
from govcheck.models import F03Checklist, F03ChecklistItem, Severity, WorkflowStage

# 門檻 40 字：39 應觸發、40 不應觸發（見 review_config.yaml f03.quality.min_evidence_chars）
_39 = "字" * 39
_40 = "字" * 40


def _item(**kw) -> F03ChecklistItem:
    base = {"item_id": "1-1", "topic": "公平性", "check_state": "是"}
    base.update(kw)
    return F03ChecklistItem(**base)


def _codes(findings) -> set[str]:
    return {f.code for f in findings}


def _run(items, stage=WorkflowStage.PROPOSAL):
    return f03_evidence_quality.run_all(F03Checklist(items=items), None, stage)


# ── 佐證過短 ───────────────────────────────────────────────────────────────


def test_below_threshold_warns_with_actual_char_count():
    fs = _run([_item(evidence_proposal=_39)])
    assert _codes(fs) == {"F03.EVIDENCE_TOO_SHORT"}
    assert fs[0].severity is Severity.WARN
    # 字數是可核對的事實，必須出現在 actual（缺陷清單靠它組出「39 字」的命中值）
    assert fs[0].actual == "39 字"
    assert "1-1" in fs[0].location


def test_exactly_at_threshold_is_clean():
    assert _run([_item(evidence_proposal=_40)]) == []


def test_one_over_threshold_is_clean():
    assert _run([_item(evidence_proposal=_40 + "字")]) == []


def test_golive_stage_checks_j_even_when_proposal_is_long():
    """I 欄夠長不能遮住 J 欄過短；上線階段品質門檻只看 J。"""
    fs = _run(
        [_item(evidence_proposal=_40, evidence_golive="上線太短")],
        WorkflowStage.GOLIVE,
    )
    short = next(f for f in fs if f.code == "F03.EVIDENCE_TOO_SHORT")
    assert short.actual == "4 字"


def test_golive_stage_does_not_recheck_short_proposal_when_j_is_long():
    """提案欄的品質已在提案階段處理，上線階段不應拿 I 欄重複警示。"""
    fs = _run(
        [_item(evidence_proposal="提案短", evidence_golive=_40 + "上線")],
        WorkflowStage.GOLIVE,
    )
    assert "F03.EVIDENCE_TOO_SHORT" not in _codes(fs)


def test_blank_evidence_is_not_too_short():
    """完全沒填由 f03_evidence_presence 的「佐證空白」負責，這裡不重複警示。"""
    assert _run([_item(evidence_proposal=None)]) == []


def test_whitespace_does_not_count_towards_length():
    """用換行與空白灌字數不算內容——否則門檻可以被排版繞過。"""
    padded = "字" * 10 + "\n" * 50 + " " * 50
    fs = _run([_item(evidence_proposal=padded)])
    assert fs[0].actual == "10 字"


def test_not_applicable_item_is_exempt():
    """不適用項次填的是理由不是佐證，不該套用佐證長度門檻。"""
    assert _run([_item(check_state="不適用", evidence_proposal="不涉及")]) == []


def test_threshold_comes_from_config_not_hardcoded():
    cfg = {"f03": {"quality": {"min_evidence_chars": 5}}}
    items = [_item(evidence_proposal="字" * 10)]
    assert f03_evidence_quality.run_all(F03Checklist(items=items), cfg) == []


# ── 照抄提案 ───────────────────────────────────────────────────────────────


def test_golive_identical_to_proposal_warns():
    fs = _run([_item(evidence_proposal=_40, evidence_golive=_40)], WorkflowStage.GOLIVE)
    assert "F03.EVIDENCE_COPIED" in _codes(fs)
    copied = next(f for f in fs if f.code == "F03.EVIDENCE_COPIED")
    assert copied.severity is Severity.WARN


def test_golive_differing_from_proposal_is_clean():
    fs = _run(
        [_item(evidence_proposal=_40, evidence_golive=_40 + "上線後新增監控")],
        WorkflowStage.GOLIVE,
    )
    assert "F03.EVIDENCE_COPIED" not in _codes(fs)


def test_whitespace_only_difference_still_counts_as_copied():
    """排版差異不是內容差異（與 golive_diff_for_stage 同一套正規化）。"""
    fs = _run(
        [_item(evidence_proposal=_40, evidence_golive="  " + _40 + "\n")],
        WorkflowStage.GOLIVE,
    )
    assert "F03.EVIDENCE_COPIED" in _codes(fs)


def test_blank_golive_is_not_copied():
    """上線佐證空白由 F03.EVIDENCE_MISSING_GOLIVE 負責，不該同時報「照抄」。"""
    fs = _run([_item(evidence_proposal=_40, evidence_golive=None)], WorkflowStage.GOLIVE)
    assert "F03.EVIDENCE_COPIED" not in _codes(fs)


def test_proposal_stage_never_checks_copied():
    fs = _run([_item(evidence_proposal=_40, evidence_golive=_40)], WorkflowStage.PROPOSAL)
    assert "F03.EVIDENCE_COPIED" not in _codes(fs)


def test_absent_sheet_returns_empty():
    assert f03_evidence_quality.run_all(F03Checklist(sheet_present=False)) == []
