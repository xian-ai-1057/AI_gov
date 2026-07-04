"""F02 觸發題規則查表（f02_reg_refs.run_all）測試。

全部離線、零 LLM。

AC coverage（p3-03 spec §6）：
- AC-10 : F02 查表正反例（觸發/不觸發/無sections）+ REG_REF_SUMMARY 恆出
- AC-16 : format_section_ref（透過 F02.REG_REF_NOTE.message 驗）
"""

from __future__ import annotations

from govcheck.checks.rule import f02_reg_refs
from govcheck.models import F02Form
from govcheck.rag.models import F02QuestionRetrieval, RetrievalMap, RetrievedSection


# ─────────────────────────────────────────────────────────────────────────────
# 測試建構 helper
# ─────────────────────────────────────────────────────────────────────────────


def _section(reg_code: str, section_path: str, title: str = "示例條（synthetic）") -> RetrievedSection:
    return RetrievedSection(
        reg_code=reg_code,
        section_path=section_path,
        title=title,
        excerpt="（示例摘錄，非真實法規內容）",
        score=0.65,
        origin="semantic",
    )


def _retrieval_map(
    qid_sections: dict[str, list[RetrievedSection]] | None = None,
) -> RetrievalMap:
    """建構測試用 RetrievalMap，只含 f02_questions。"""
    questions: dict[str, F02QuestionRetrieval] = {}
    for qid, secs in (qid_sections or {}).items():
        questions[qid] = F02QuestionRetrieval(qid=qid, sections=secs)
    return RetrievalMap(
        schema_version=1,
        built_at="2026-01-01T00:00:00Z",
        embedding_model="fake-embedding-model",
        f03_items={},
        f02_questions=questions,
    )


def _scoring_cfg(qids_score_on: dict[str, str]) -> dict:
    """快速組 scoring_cfg，只含 questions 子集。"""
    return {
        "questions": {
            qid: {"score_on": score_on}
            for qid, score_on in qids_score_on.items()
        }
    }


# ─────────────────────────────────────────────────────────────────────────────
# AC-10：觸發題有對應 sections → F02.REG_REF_NOTE（INFO/source=rule）
# ─────────────────────────────────────────────────────────────────────────────


def test_triggered_with_sections_produces_note():
    """AC-10：answers[qid]==score_on 且 map 有 sections → F02.REG_REF_NOTE（INFO，source=rule）。"""
    mapping = _retrieval_map({
        "UC-01": [_section("R02", "四/(一)", title="示例F02對應條（synthetic）")],
    })
    scoring = _scoring_cfg({"UC-01": "Y", "UC-02": "Y"})
    f02 = F02Form(answers={"UC-01": "Y", "UC-02": "N"})  # UC-01 觸發，UC-02 不觸發

    findings = f02_reg_refs.run_all(f02, mapping, scoring)
    codes = [f.code for f in findings]

    assert "F02.REG_REF_NOTE" in codes
    note = next(f for f in findings if f.code == "F02.REG_REF_NOTE")
    assert note.severity.value == "info"
    assert note.source == "rule"
    # message 應含條號引用（format_section_ref 輸出）
    assert "R02 四、(一)" in note.message
    # message 應含標題
    assert "示例F02對應條（synthetic）" in note.message
    # UC-02 不觸發 → 不產 note（多於 1 個 REG_REF_NOTE 才會包含 UC-02）
    notes = [f for f in findings if f.code == "F02.REG_REF_NOTE"]
    assert len(notes) == 1  # 只有 UC-01 觸發


def test_triggered_note_contains_qid():
    """AC-10 補充：REG_REF_NOTE.location 含 qid。"""
    mapping = _retrieval_map({
        "UC-03": [_section("R04", "三/(一)")],
    })
    scoring = _scoring_cfg({"UC-03": "Y"})
    f02 = F02Form(answers={"UC-03": "Y"})

    findings = f02_reg_refs.run_all(f02, mapping, scoring)
    note = next(f for f in findings if f.code == "F02.REG_REF_NOTE")
    assert note.location == "UC-03"


# ─────────────────────────────────────────────────────────────────────────────
# AC-10：不觸發 → 不產 REG_REF_NOTE
# ─────────────────────────────────────────────────────────────────────────────


def test_non_triggered_no_note():
    """AC-10：answers[qid] != score_on → 不產 F02.REG_REF_NOTE。"""
    mapping = _retrieval_map({
        "UC-01": [_section("R02", "四/(一)")],
    })
    scoring = _scoring_cfg({"UC-01": "Y"})
    f02 = F02Form(answers={"UC-01": "N"})  # N != score_on("Y")

    findings = f02_reg_refs.run_all(f02, mapping, scoring)
    codes = [f.code for f in findings]
    assert "F02.REG_REF_NOTE" not in codes


def test_unanswered_question_no_note():
    """AC-10：題目未填（不在 answers 或 None）→ 不觸發、不產 note。"""
    mapping = _retrieval_map({
        "UC-01": [_section("R02", "四/(一)")],
    })
    scoring = _scoring_cfg({"UC-01": "Y"})
    # 未填 UC-01
    f02 = F02Form(answers={})

    findings = f02_reg_refs.run_all(f02, mapping, scoring)
    codes = [f.code for f in findings]
    assert "F02.REG_REF_NOTE" not in codes


# ─────────────────────────────────────────────────────────────────────────────
# AC-10：觸發但無 sections → 靜默
# ─────────────────────────────────────────────────────────────────────────────


def test_triggered_no_sections_silent():
    """AC-10：觸發但 sections 為空 → 靜默不產 REG_REF_NOTE。"""
    mapping = _retrieval_map({
        "UC-01": [],  # 空 sections
    })
    scoring = _scoring_cfg({"UC-01": "Y"})
    f02 = F02Form(answers={"UC-01": "Y"})

    findings = f02_reg_refs.run_all(f02, mapping, scoring)
    codes = [f.code for f in findings]
    assert "F02.REG_REF_NOTE" not in codes


def test_triggered_qid_not_in_map_silent():
    """AC-10：觸發但 qid 不在 retrieval_map.f02_questions → 靜默。"""
    mapping = _retrieval_map({})  # 空 map
    scoring = _scoring_cfg({"UC-01": "Y"})
    f02 = F02Form(answers={"UC-01": "Y"})

    findings = f02_reg_refs.run_all(f02, mapping, scoring)
    codes = [f.code for f in findings]
    assert "F02.REG_REF_NOTE" not in codes


# ─────────────────────────────────────────────────────────────────────────────
# AC-10：REG_REF_SUMMARY 恆出
# ─────────────────────────────────────────────────────────────────────────────


def test_summary_always_produced():
    """AC-10：REG_REF_SUMMARY 恆出（無論有無觸發）。"""
    mapping = _retrieval_map({})
    scoring = _scoring_cfg({"UC-01": "Y"})
    f02 = F02Form(answers={"UC-01": "N"})  # 無觸發

    findings = f02_reg_refs.run_all(f02, mapping, scoring)
    codes = [f.code for f in findings]
    assert "F02.REG_REF_SUMMARY" in codes


def test_summary_always_produced_with_trigger():
    """AC-10：有觸發時 REG_REF_SUMMARY 仍恆出。"""
    mapping = _retrieval_map({
        "UC-01": [_section("R02", "四/(一)")],
    })
    scoring = _scoring_cfg({"UC-01": "Y"})
    f02 = F02Form(answers={"UC-01": "Y"})

    findings = f02_reg_refs.run_all(f02, mapping, scoring)
    codes = [f.code for f in findings]
    assert "F02.REG_REF_SUMMARY" in codes
    summary = next(f for f in findings if f.code == "F02.REG_REF_SUMMARY")
    assert summary.severity.value == "info"
    assert summary.source == "rule"


def test_summary_contains_counts():
    """AC-10 補充：REG_REF_SUMMARY.message 含觸發題數與引用條數。"""
    mapping = _retrieval_map({
        "UC-01": [
            _section("R02", "四/(一)"),
            _section("R03", "五/(二)/2"),
        ],
    })
    scoring = _scoring_cfg({"UC-01": "Y", "UC-02": "Y"})
    f02 = F02Form(answers={"UC-01": "Y", "UC-02": "N"})

    findings = f02_reg_refs.run_all(f02, mapping, scoring)
    summary = next(f for f in findings if f.code == "F02.REG_REF_SUMMARY")
    # 觸發 1 道，引用 2 條
    assert "1" in summary.message  # 觸發題數
    assert "2" in summary.message  # 引用條數


# ─────────────────────────────────────────────────────────────────────────────
# AC-10 / AC-16：format_section_ref 引用顯示在 note.message 中
# ─────────────────────────────────────────────────────────────────────────────


def test_note_message_uses_format_section_ref_chapter():
    """AC-16 via AC-10：章節式 section_path 在 message 中顯示為正確格式。"""
    mapping = _retrieval_map({
        "UC-04-01": [_section("R03", "五/(二)/2", title="示例章節條（synthetic）")],
    })
    scoring = _scoring_cfg({"UC-04-01": "Y"})
    f02 = F02Form(answers={"UC-04-01": "Y"})

    findings = f02_reg_refs.run_all(f02, mapping, scoring)
    note = next(f for f in findings if f.code == "F02.REG_REF_NOTE")
    assert "R03 五、(二) 2" in note.message


def test_note_message_uses_format_section_ref_article():
    """AC-16 via AC-10：條文式 section_path（第一條）在 message 中顯示正確。"""
    mapping = _retrieval_map({
        "D-03": [_section("R01", "第一條", title="示例條文式條（synthetic）")],
    })
    scoring = _scoring_cfg({"D-03": "Y"})
    f02 = F02Form(answers={"D-03": "Y"})

    findings = f02_reg_refs.run_all(f02, mapping, scoring)
    note = next(f for f in findings if f.code == "F02.REG_REF_NOTE")
    assert "R01 第一條" in note.message


# ─────────────────────────────────────────────────────────────────────────────
# score_on = N 的題目（UC-07、M-01、M-02 等）
# ─────────────────────────────────────────────────────────────────────────────


def test_score_on_n_triggers_when_answer_is_n():
    """AC-10：score_on=N 的題目，答案為 N 時觸發（反向觸發）。"""
    mapping = _retrieval_map({
        "UC-07": [_section("R05", "三/(一)")],
    })
    scoring = _scoring_cfg({"UC-07": "N"})  # score_on = "N"
    f02 = F02Form(answers={"UC-07": "N"})   # N == score_on → 觸發

    findings = f02_reg_refs.run_all(f02, mapping, scoring)
    codes = [f.code for f in findings]
    assert "F02.REG_REF_NOTE" in codes


def test_score_on_n_no_trigger_when_answer_is_y():
    """AC-10：score_on=N 的題目，答案為 Y 時不觸發。"""
    mapping = _retrieval_map({
        "UC-07": [_section("R05", "三/(一)")],
    })
    scoring = _scoring_cfg({"UC-07": "N"})  # score_on = "N"
    f02 = F02Form(answers={"UC-07": "Y"})   # Y != N → 不觸發

    findings = f02_reg_refs.run_all(f02, mapping, scoring)
    codes = [f.code for f in findings]
    assert "F02.REG_REF_NOTE" not in codes


# ─────────────────────────────────────────────────────────────────────────────
# 多個觸發題
# ─────────────────────────────────────────────────────────────────────────────


def test_multiple_triggered_produces_multiple_notes():
    """AC-10：多個觸發題各自產一筆 REG_REF_NOTE。"""
    mapping = _retrieval_map({
        "UC-01": [_section("R02", "四/(一)")],
        "UC-03": [_section("R04", "三/(一)")],
    })
    scoring = _scoring_cfg({"UC-01": "Y", "UC-02": "Y", "UC-03": "Y"})
    f02 = F02Form(answers={"UC-01": "Y", "UC-02": "N", "UC-03": "Y"})

    findings = f02_reg_refs.run_all(f02, mapping, scoring)
    notes = [f for f in findings if f.code == "F02.REG_REF_NOTE"]
    assert len(notes) == 2


# ─────────────────────────────────────────────────────────────────────────────
# source / needs_human 檢查
# ─────────────────────────────────────────────────────────────────────────────


def test_all_findings_source_rule():
    """f02_reg_refs 所有 Finding source='rule'（純規則、零 LLM）。"""
    mapping = _retrieval_map({
        "UC-01": [_section("R02", "四/(一)")],
    })
    scoring = _scoring_cfg({"UC-01": "Y"})
    f02 = F02Form(answers={"UC-01": "Y"})

    findings = f02_reg_refs.run_all(f02, mapping, scoring)
    assert all(f.source == "rule" for f in findings)


def test_all_findings_needs_human():
    """所有 Finding needs_human=True（Finding 預設值）。"""
    mapping = _retrieval_map({
        "UC-01": [_section("R02", "四/(一)")],
    })
    scoring = _scoring_cfg({"UC-01": "Y"})
    f02 = F02Form(answers={"UC-01": "Y"})

    findings = f02_reg_refs.run_all(f02, mapping, scoring)
    assert all(f.needs_human for f in findings)
