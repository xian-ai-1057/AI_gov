"""F02 觸發題的法規條文引用（確定性、零 LLM）。

對 F02Form 每道觸發題（`answers[qid] == score_on`）查凍結對照表，把對應條號逐字列出，
讓治理人員知道「這題會加分，是因為牽動了哪幾條規範」。純查表、不做判讀，
所以即使 LLM 關閉也會出現在報告裡。

對照表缺檔時由呼叫端傳 `retrieval_map=None`，本檢查直接回空清單（不產生噪音）。
"""

from __future__ import annotations

from govcheck.logging_setup import get_logger
from govcheck.models import F02Form, Finding, Severity
from govcheck.regulations.mapping import format_section_ref
from govcheck.regulations.models import RetrievalMap

log = get_logger("f02_reg_refs")


def run_all(
    f02_form: F02Form,
    retrieval_map: RetrievalMap | None,
    scoring_cfg: dict,
    max_items: int = 30,
) -> list[Finding]:
    """F02 觸發題查表；回傳 Finding 清單（source="rule"）。

    觸發且有對應條文 → F02.REG_REF_NOTE（INFO）；觸發但無對應 → 靜默。
    收尾恆出 F02.REG_REF_SUMMARY（INFO）。log 只記計數，不記條文內容。
    """
    if retrieval_map is None:
        return []

    questions_cfg: dict = scoring_cfg.get("questions", {})
    findings: list[Finding] = []
    triggered_count = 0
    cited_count = 0

    for qid, qspec in questions_cfg.items():
        answer = f02_form.answers.get(qid)
        if answer is None or answer != qspec.get("score_on"):
            continue
        triggered_count += 1

        q_retrieval = retrieval_map.f02_questions.get(qid)
        if q_retrieval is None or not q_retrieval.sections:
            continue

        # 題目全觸發時引用會洗版報告，故設上限；超出者仍計入摘要的觸發題數。
        if len(findings) >= max_items:
            continue

        ref_parts = [
            f"{format_section_ref(sec.reg_code, sec.section_path)}（{sec.title}）"
            if sec.title
            else format_section_ref(sec.reg_code, sec.section_path)
            for sec in q_retrieval.sections
        ]
        cited_count += len(ref_parts)

        findings.append(Finding(
            severity=Severity.INFO,
            code="F02.REG_REF_NOTE",
            title=f"F02 觸發題對應法規條文（{qid}）",
            message=(f"F02 風險題 {qid} 答案為「{answer}」（觸發風險評分），"
                     f"對應法規條文引用：{'、'.join(ref_parts)}。"
                     "請確認相關法規要求已納入管控措施，送治理人員人工覆核。"),
            location=qid,
            source="rule",
        ))

    log.info("f02 reg_refs triggered=%d cited=%d", triggered_count, cited_count)

    findings.append(Finding(
        severity=Severity.INFO,
        code="F02.REG_REF_SUMMARY",
        title="F02 法規查表摘要",
        message=(f"F02 法規查表：觸發題 {triggered_count} 道，"
                 f"共引用 {cited_count} 條條文；僅供初步參考，請人工覆核。"),
        source="rule",
    ))
    return findings
