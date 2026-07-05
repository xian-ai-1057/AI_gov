"""F02 觸發題規則查表（Phase 3，p3-03 §4.3）。

對 F02Form 每道觸發題（answers[qid] == score_on）：
- 若 retrieval_map.f02_questions[qid].sections 非空 → 產 F02.REG_REF_NOTE（INFO，source="rule"），
  message 逐字引用條號+標題（使用 format_section_ref helper）；
- 觸發但無 sections → 靜默不產 Finding；
- 收尾一筆 F02.REG_REF_SUMMARY（INFO，source="rule"，觸發題數 / 引用條數計數）。

純規則、零 LLM（p3-03 spec §4.3 第 1 次重複提醒）。
"""

from __future__ import annotations

from govcheck.logging_setup import get_logger
from govcheck.models import F02Form, Finding, Severity
from govcheck.rag.mapping import format_section_ref
from govcheck.rag.models import RetrievalMap

log = get_logger("f02_reg_refs")


def run_all(
    f02_form: F02Form,
    retrieval_map: RetrievalMap,
    scoring_cfg: dict,
) -> list[Finding]:
    """F02 觸發題規則查表；回傳 Finding 清單（source="rule"）。

    f02_form.answers[qid] == scoring_cfg["questions"][qid]["score_on"] → 觸發。
    觸發且有對應 sections → F02.REG_REF_NOTE（INFO）。
    觸發但無 sections → 靜默。
    收尾恆出 F02.REG_REF_SUMMARY（INFO）。
    log 只記 triggered / cited 計數（不記條文內容）。
    """
    questions_cfg: dict = scoring_cfg.get("questions", {})
    findings: list[Finding] = []
    triggered_count = 0
    cited_count = 0

    for qid, qspec in questions_cfg.items():
        score_on = qspec.get("score_on")
        answer = f02_form.answers.get(qid)
        if answer is None or answer != score_on:
            continue
        # 此題觸發
        triggered_count += 1

        q_retrieval = retrieval_map.f02_questions.get(qid)
        if q_retrieval is None or not q_retrieval.sections:
            # 無對應 sections → 靜默
            continue

        # 逐字引用條號+標題（format_section_ref 產生顯示字串）
        ref_parts: list[str] = []
        for sec in q_retrieval.sections:
            ref_str = format_section_ref(sec.reg_code, sec.section_path)
            # 標題非空時附上
            if sec.title:
                ref_parts.append(f"{ref_str}（{sec.title}）")
            else:
                ref_parts.append(ref_str)
        cited_count += len(ref_parts)

        msg = (
            f"F02 風險題 {qid} 答案為「{answer}」（觸發風險評分），"
            f"對應法規條文引用：{'、'.join(ref_parts)}。"
            "請確認相關法規要求已納入管控措施，送治理人員人工覆核。"
        )
        findings.append(Finding(
            severity=Severity.INFO,
            code="F02.REG_REF_NOTE",
            title=f"F02 觸發題對應法規條文（{qid}）",
            message=msg,
            location=qid,
            source="rule",
        ))

    log.info("f02 reg_refs triggered=%d cited=%d", triggered_count, cited_count)

    findings.append(Finding(
        severity=Severity.INFO,
        code="F02.REG_REF_SUMMARY",
        title="F02 法規查表摘要",
        message=(
            f"F02 法規查表：觸發題 {triggered_count} 道，"
            f"共引用 {cited_count} 條條文；僅供初步參考，請人工覆核。"
        ),
        source="rule",
    ))
    return findings
