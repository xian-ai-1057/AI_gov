"""缺陷彙整（報告頁預設視圖）的正/反例。

核心不變式：
  1. 同一原因的 N 條 finding 收成 1 列，count 等於 N。
  2. 一條 finding 只歸一種缺陷（不得被數兩次）。
  3. count == 0 的類型仍然列出且為 "ok"——「查過了、沒有」與「沒查」必須看得出差別。
  4. 判定句以 decision_status 為準，不會出現「規則通過」卻同頁全是待確認的矛盾。
"""

from __future__ import annotations

import pytest

from govcheck.models import (
    Citation,
    DecisionStatus,
    F03ItemAssessment,
    F03ItemCoverage,
    Finding,
    LayerStatus,
    ReviewReport,
    ReviewRunMeta,
    RubricLine,
    Severity,
    WorkflowStage,
)
from govcheck.report import defects as D

_CFG = {
    "f03": {
        "sheet": "檢核表",
        "checklist": {
            "evidence_proposal_col": "I",
            "evidence_golive_col": "J",
            "regulation_ref_col": "L",
        },
    }
}


def _roster(n: int, **kw) -> list[F03ItemCoverage]:
    return [
        F03ItemCoverage(
            item_id=f"1-{i}", topic="公平性", coverage="assessable",
            short_name=f"檢核項{i}", row_index=4 + i, **kw,
        )
        for i in range(1, n + 1)
    ]


def _report(
    findings,
    roster=None,
    status=DecisionStatus.CHANGES_REQUIRED,
    stage="proposal",
    assessments=None,
    layers=None,
):
    return ReviewReport(
        form_type="送件包", subject="測試案", findings=findings,
        checklist_roster=roster or [], decision_status=status,
        review_run_meta=ReviewRunMeta(workflow_stage=WorkflowStage(stage)),
        item_assessments=assessments or [], layers=layers or [],
    )


def _blank_golive(item_id: str) -> Finding:
    return Finding(
        severity=Severity.INFO, code="F03.EVIDENCE_MISSING_GOLIVE",
        title="上線階段佐證空白", message="…", location=f"項次 {item_id}（公平性）",
        expected="上線階段佐證", actual="空白",
    )


def _by_code(defects) -> dict:
    return {d.code: d for d in defects}


# ── 收斂：N 條同因 finding → 1 列 ────────────────────────────────────────


def test_fifteen_same_findings_collapse_to_one_row():
    roster = _roster(15)
    report = _report([_blank_golive(f"1-{i}") for i in range(1, 16)], roster)
    result = D.build_defects(report, _CFG)

    blank = _by_code(result)["BLANK_GOLIVE"]
    assert blank.count == 15
    assert len(blank.hits) == 15
    # 全部 15 個檢核項都命中 → 提升成共通事實帶，不再逐列重複
    assert blank.is_common is True
    facts = D.build_common_facts(result, len(roster))
    assert [f.code for f in facts] == ["BLANK_GOLIVE"]
    assert facts[0].text == "15/15 項上線佐證空白"


def test_partial_hit_is_not_a_common_fact():
    """只有部分項次命中時不能收進共通事實帶——那會謊報規模。"""
    report = _report([_blank_golive("1-1"), _blank_golive("1-2")], _roster(15))
    blank = _by_code(D.build_defects(report, _CFG))["BLANK_GOLIVE"]
    assert blank.count == 2
    assert blank.is_common is False
    assert D.build_common_facts(D.build_defects(report, _CFG), 15) == []


def test_same_hit_count_with_different_item_ids_is_not_common():
    """筆數剛好相同仍不夠；命中項次集合必須與名冊完全一致。"""
    report = _report([_blank_golive("1-1"), _blank_golive("9-9")], _roster(2))
    blank = _by_code(D.build_defects(report, _CFG))["BLANK_GOLIVE"]
    assert blank.count == 2
    assert blank.is_common is False


def test_each_finding_counts_once_only():
    """一條 finding 只能歸一種缺陷，否則同一件事會被數兩次。"""
    findings = [
        _blank_golive("1-1"),
        Finding(severity=Severity.WARN, code="F03.EVIDENCE_TOO_SHORT",
                title="佐證過短", message="…", location="項次 1-2", actual="18 字"),
    ]
    result = D.build_defects(_report(findings, _roster(3)), _CFG)
    assert sum(d.count for d in result) == 2


def test_duplicate_findings_for_one_item_count_as_one_hit():
    """同一項的多條同類 finding 是一個缺陷命中，不可把項次數灌大。"""
    report = _report([_blank_golive("1-1"), _blank_golive("1-1")], _roster(3))
    blank = _by_code(D.build_defects(report, _CFG))["BLANK_GOLIVE"]
    assert blank.count == 1
    assert len(blank.hits) == 1


# ── count == 0 仍要列出 ──────────────────────────────────────────────────


def test_zero_count_types_are_still_listed_as_ok():
    result = _by_code(D.build_defects(_report([], _roster(3)), _CFG))
    copied = result["COPIED_FROM_PROPOSAL"]
    assert copied.count == 0
    # 「查過了、沒有」→ ok 色；不可跟「沒查」長得一樣
    assert copied.severity == "ok"


def test_noisy_optional_types_are_hidden_when_absent():
    """PARSE_ERROR / UNDETERMINED 沒命中時不佔版面（always_show=False）。"""
    codes = {d.code for d in D.build_defects(_report([], _roster(3)), _CFG)}
    assert "PARSE_ERROR" not in codes
    assert "UNDETERMINED" not in codes


def test_severity_reflects_catalog_when_hit():
    findings = [Finding(severity=Severity.WARN, code="CROSS.OWNER_MISMATCH",
                        title="不一致", message="…", expected="甲", actual="乙")]
    cross = _by_code(D.build_defects(_report(findings), _CFG))["CROSS_INCONSISTENT"]
    assert cross.severity == "error"
    assert cross.count == 1
    # 跨表比對的命中值是「期望 ≠ 實際」，治理人員可直接核對
    assert cross.hits[0].value == "甲 ≠ 乙"


# ── 可稽核座標 ──────────────────────────────────────────────────────────


def test_hits_carry_item_id_and_cell_locator():
    report = _report([_blank_golive("1-2")], _roster(3))
    hit = _by_code(D.build_defects(report, _CFG))["BLANK_GOLIVE"].hits[0]
    assert hit.item_id == "1-2"
    # roster 的 row_index=6（4+2）＋ J 欄 → J6
    assert hit.locator.cell == "J6"
    assert hit.locator.label() == "F03·檢核表 J6"


def test_too_short_locator_points_at_proposal_column():
    finding = Finding(severity=Severity.WARN, code="F03.EVIDENCE_TOO_SHORT",
                      title="過短", message="…", location="項次 1-1", actual="18 字")
    hit = _by_code(D.build_defects(_report([finding], _roster(3)), _CFG))["TOO_SHORT"].hits[0]
    assert hit.locator.cell == "I5"
    assert hit.value == "18 字"


def test_too_short_basis_uses_configured_threshold_from_finding():
    finding = Finding(
        severity=Severity.WARN,
        code="F03.EVIDENCE_TOO_SHORT",
        title="過短",
        message="…",
        location="項次 1-1",
        expected="至少 55 字的具體說明",
        actual="18 字",
    )
    defect = _by_code(D.build_defects(_report([finding], _roster(3)), _CFG))["TOO_SHORT"]
    assert defect.basis == "佐證說明字數低於 55 字，不足以檢核"


def test_too_short_locator_points_at_golive_column_for_golive_review():
    finding = Finding(
        severity=Severity.WARN,
        code="F03.EVIDENCE_TOO_SHORT",
        title="過短",
        message="…",
        location="項次 1-1",
        actual="8 字",
    )
    defects = _by_code(D.build_defects(
        _report([finding], _roster(3), stage="golive"), _CFG
    ))
    assert defects["TOO_SHORT"].hits[0].locator.cell == "J5"
    assert defects["TOO_SHORT"].locator_label == "F03·檢核表 J 欄"


def test_missing_row_index_gives_no_cell_guess():
    """列號不明時只給檔名與工作表，不猜格號——猜錯的座標比沒有座標更糟。"""
    roster = [F03ItemCoverage(item_id="1-1", coverage="assessable", row_index=None)]
    hit = _by_code(D.build_defects(_report([_blank_golive("1-1")], roster), _CFG))
    locator = hit["BLANK_GOLIVE"].hits[0].locator
    assert locator.cell is None
    assert locator.label() == "F03·檢核表"


def test_regulation_gap_hit_uses_canonical_refs_and_regulation_locator():
    finding = Finding(
        severity=Severity.WARN,
        code="F03.REGULATION_GAP",
        title="法規缺口",
        message="…",
        location="項次 1-1",
    )
    assessment = F03ItemAssessment(
        item_id="1-1",
        profile="fairness",
        regulation_gap="gap",
        regulation_refs=["治理辦法 §3.2", "治理辦法 §3.4"],
    )
    hit = _by_code(D.build_defects(
        _report([finding], _roster(3), assessments=[assessment]), _CFG
    ))["REG_GAP"].hits[0]
    assert hit.value == "治理辦法 §3.2、治理辦法 §3.4"
    assert hit.locator.label() == "治理辦法 治理辦法 §3.2、治理辦法 §3.4"


def test_semantic_defect_prefers_validated_citation_locator():
    finding = Finding(
        severity=Severity.WARN,
        code="F03.EVIDENCE_SOURCE_CONFLICT",
        title="佐證衝突",
        message="…",
        location="項次 1-1",
    )
    assessment = F03ItemAssessment(
        item_id="1-1",
        profile="fairness",
        citations=[Citation(
            document_id="doc-safe-id",
            unit_id="doc-safe-id:9",
            locator_type="page",
            locator_value="9",
        )],
    )
    hit = _by_code(D.build_defects(
        _report([finding], _roster(3), assessments=[assessment]), _CFG
    ))["EVIDENCE_CONFLICT"].hits[0]
    assert hit.locator.label() == "doc-safe-id p.9"


def test_non_item_findings_have_no_item_id():
    findings = [Finding(severity=Severity.ERROR, code="DOC.MISSING_F01",
                        title="缺件", message="…", location="送件包")]
    hit = _by_code(D.build_defects(_report(findings), _CFG))["DOC_MISSING"].hits[0]
    assert hit.item_id is None
    assert hit.locator is None


def test_hits_are_capped_and_remainder_summarised():
    roster = _roster(60)
    report = _report([_blank_golive(f"1-{i}") for i in range(1, 61)], roster)
    blank = _by_code(D.build_defects(report, _CFG, max_hits=40))["BLANK_GOLIVE"]
    assert blank.count == 60          # 筆數仍是真實總數
    assert len(blank.hits) == 41      # 40 筆 + 一筆「另 20 項」
    assert blank.hits[-1].value == "另 20 項"


# ── 降級訊號不是缺陷 ────────────────────────────────────────────────────


def test_notice_codes_are_excluded_from_defects():
    findings = [
        Finding(severity=Severity.INFO, code="F03.LLM_SKIPPED", title="略過", message="…"),
        Finding(severity=Severity.INFO, code="REGULATION.MAP_UNAVAILABLE",
                title="對照表未就緒", message="…"),
    ]
    result = D.build_defects(_report(findings), _CFG)
    assert sum(d.count for d in result) == 0
    notices = D.build_notices(findings)
    assert {f.code for f in notices} == {"F03.LLM_SKIPPED", "REGULATION.MAP_UNAVAILABLE"}


# ── 判定句 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (DecisionStatus.CHANGES_REQUIRED, "blocked"),
        (DecisionStatus.MANUAL_REVIEW, "review"),
        (DecisionStatus.INCOMPLETE, "review"),
        (DecisionStatus.APPROVED, "passed"),
    ],
)
def test_verdict_code_follows_decision_status(status, expected):
    report = _report([], _roster(3), status=status)
    assert D.build_verdict(report, D.build_defects(report, _CFG)).code == expected


def test_error_defect_blocks_even_if_status_says_otherwise():
    """有 error 級缺陷就不可能是「可進入覆核」——兩者矛盾時以事實為準。"""
    findings = [Finding(severity=Severity.WARN, code="CROSS.OWNER_MISMATCH",
                        title="不一致", message="…")]
    report = _report(findings, _roster(3), status=DecisionStatus.APPROVED)
    assert D.build_verdict(report, D.build_defects(report, _CFG)).code == "blocked"


def test_because_states_scale_and_says_rule_layer_is_clean():
    roster = _roster(15)
    findings = [_blank_golive(f"1-{i}") for i in range(1, 16)]
    report = _report(findings, roster)
    because = D.build_verdict(report, D.build_defects(report, _CFG)).because
    assert "15 個檢核項全部上線佐證空白" in because


def test_because_is_explicit_when_nothing_found():
    report = _report([], _roster(3), status=DecisionStatus.APPROVED)
    because = D.build_verdict(report, D.build_defects(report, _CFG)).because
    assert "未發現缺陷" in because
    assert "仍須人工覆核" in because


# ── 待覆核項次：試辦層不計入 ─────────────────────────────────────────────


def test_trial_layer_defects_do_not_inflate_attention_count():
    findings = [
        _blank_golive("1-1"),
        Finding(severity=Severity.WARN, code="F03.ASSESSMENT_VAGUE",
                title="敘述籠統", message="…", location="項次 1-3"),
    ]
    assessment = F03ItemAssessment(
        item_id="1-3",
        profile="fairness",
        rubric=[
            RubricLine(text="有具體做法", met=True),
            RubricLine(text="有負責單位", met=False),
        ],
    )
    report = _report(findings, _roster(3), assessments=[assessment])
    result = D.build_defects(report, _CFG)
    assert _by_code(result)["RUBRIC_UNMET"].trial is True
    # 1-3 只被試辦層命中 → 不進待覆核清單
    assert D.attention_item_ids(report, result) == ["1-1"]


def test_rubric_unmet_is_derived_once_per_assessment_item():
    """多條概括 finding 不得重複計數；hit 顯示逐句可核對的實際結果。"""
    findings = [
        Finding(
            severity=Severity.WARN,
            code="F03.ASSESSMENT_INCOMPLETE",
            title="內容不完整",
            message="…",
            location="項次 4-2",
        ),
        Finding(
            severity=Severity.WARN,
            code="F03.ASSESSMENT_VAGUE",
            title="內容空泛",
            message="…",
            location="項次 4-2",
        ),
    ]
    assessment = F03ItemAssessment(
        item_id="4-2",
        profile="security",
        rubric=[
            RubricLine(text="有指名監控指標", met=True),
            RubricLine(text="有寫檢視頻率", met=False),
            RubricLine(text="有指定負責單位", met=False),
            RubricLine(text="有異常門檻與通報流程", met=False),
        ],
    )
    rubric = _by_code(D.build_defects(
        _report(findings, _roster(3), assessments=[assessment]), _CFG
    ))["RUBRIC_UNMET"]
    assert rubric.count == 1
    assert len(rubric.hits) == 1
    assert rubric.hits[0].item_id == "4-2"
    assert rubric.hits[0].value == "4 句中 1 句滿足"
    assert rubric.codes == ["F03.ASSESSMENT_INCOMPLETE", "F03.ASSESSMENT_VAGUE"]


def test_zero_count_rows_only_show_for_completed_layers():
    layers = [
        LayerStatus(id="L1", label="規則檢查", state="done"),
        LayerStatus(id="L2", label="法規／佐證檢索", state="running"),
        LayerStatus(id="L3", label="敘述充分性判讀", state="todo"),
    ]
    codes = {d.code for d in D.build_defects(
        _report([], _roster(3), layers=layers), _CFG
    )}
    assert "COPIED_FROM_PROPOSAL" in codes
    assert "REG_GAP" not in codes
    assert "RUBRIC_UNMET" not in codes


def test_completed_layer_may_report_zero_as_checked_ok():
    layers = [
        LayerStatus(id="L1", label="規則檢查", state="done"),
        LayerStatus(id="L2", label="法規／佐證檢索", state="done"),
        LayerStatus(id="L3", label="敘述充分性判讀", state="done"),
    ]
    defects = _by_code(D.build_defects(
        _report([], _roster(3), layers=layers), _CFG
    ))
    assert defects["REG_GAP"].severity == "ok"
    assert defects["RUBRIC_UNMET"].severity == "ok"


# ── 同值欄收起 ──────────────────────────────────────────────────────────


def test_uniform_columns_detects_all_blank_golive():
    roster = _roster(5, evidence_proposal_text="已填寫的佐證", evidence_golive_text=None)
    report = _report([], roster, stage="golive")
    columns = D.build_uniform_columns(report)
    assert "提案佐證（全部已填寫）" in columns
    assert "上線佐證（全部空白）" in columns
    # 上線佐證整欄空白時「兩段差異」必然有差異，屬可推導欄位
    assert "兩段差異（可由上線佐證推導）" in columns


def test_uniform_columns_ignores_proposal_stage_golive():
    roster = _roster(5, evidence_proposal_text="已填寫的佐證")
    columns = D.build_uniform_columns(_report([], roster, stage="proposal"))
    assert not any("上線佐證" in c for c in columns)


def test_mixed_column_is_not_collapsed():
    """欄內有差異就不能收起——那一欄正是治理人員要看的。"""
    roster = _roster(3, evidence_proposal_text="有內容")
    roster[1].evidence_proposal_text = None
    assert D.build_uniform_columns(_report([], roster)) == []


def test_single_row_roster_collapses_nothing():
    assert D.build_uniform_columns(_report([], _roster(1))) == []


def test_uniform_assessment_column_requires_the_same_item_id_set():
    assessments = [
        F03ItemAssessment(item_id="1-1", profile="fairness", regulation_gap="covered"),
        F03ItemAssessment(item_id="9-9", profile="fairness", regulation_gap="covered"),
    ]
    columns = D.build_uniform_columns(_report([], _roster(2), assessments=assessments))
    assert not any("法規符合" in column for column in columns)


# ── 分層狀態 ────────────────────────────────────────────────────────────


def test_layers_marked_skipped_when_llm_did_not_run():
    layers = {layer.id: layer.state for layer in D.build_layers(_report([], _roster(3)))}
    assert layers == {"L1": "done", "L2": "skipped", "L3": "skipped"}
