"""F03 佐證缺漏規則檢查（確定性、無網路）。

與 checks/llm 的判讀分離：此處只做「勾完成卻佐證空白」「標不適用卻沒寫理由」這類
可機械判定的缺漏，即使 LLM 關閉也能把基本把關做掉。
判讀性的差異/草率交給 checks/llm/f03_evidence。
"""

from __future__ import annotations

from govcheck.models import F03Checklist, Finding, Severity, WorkflowStage


def run_all(
    form: F03Checklist,
    cfg: dict | None = None,
    workflow_stage: WorkflowStage = WorkflowStage.PROPOSAL,
) -> list[Finding]:  # noqa: ARG001 - 對齊 Check 介面
    findings: list[Finding] = []
    if form is None or not form.sheet_present:
        return findings

    for item in form.items:
        if item.check_state == "不適用":
            # 不適用的項次不進語意判讀，人工是唯一把關；主張沒附理由就無從確認是否成立。
            if not item.evidence_proposal:
                findings.append(Finding(
                    severity=Severity.WARN,
                    code="F03.NOT_APPLICABLE_NO_REASON",
                    title="標示「不適用」但未說明理由",
                    message="此檢核項標示「不適用」且佐證欄空白，無從確認不適用主張是否成立；"
                            "請於佐證欄補述理由（例如本系統不涉及該情境），或改為進行檢核。",
                    location=item.loc,
                    expected="不適用理由",
                    actual="空白",
                ))
            continue
        if not item.is_done:
            continue  # 僅就「已完成檢核（是）」要求佐證；否不強制
        loc = item.loc
        if not item.evidence_proposal:
            findings.append(Finding(
                severity=Severity.WARN,
                code="F03.EVIDENCE_MISSING_PROPOSAL",
                title="已勾選完成，但提案規劃階段佐證空白",
                message="此檢核項勾選「是（已完成檢核）」，卻未填『前項佐證說明(提案規劃階段)』，請補充佐證或確認勾選。",
                location=loc,
                expected="提案規劃階段佐證",
                actual="空白",
            ))
        if workflow_stage is not WorkflowStage.PROPOSAL and not item.evidence_golive:
            findings.append(Finding(
                severity=Severity.INFO,
                code="F03.EVIDENCE_MISSING_GOLIVE",
                title="上線階段佐證空白",
                message="此檢核項『前項佐證說明(上線階段;列出與提案差異)』空白；上線階段佐證可能於上線前補填，請留意。",
                location=loc,
                expected="上線階段佐證（列出與提案差異）",
                actual="空白",
            ))
    return findings
