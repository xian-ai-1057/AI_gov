"""F03 佐證缺漏規則檢查（確定性、無網路）。

與 checks/llm 的判讀分離：此處只做「勾完成卻佐證空白」這種可機械判定的缺漏，
即使 LLM 關閉也能把基本把關做掉。判讀性的差異/草率交給 checks/llm/f03_evidence。
"""

from __future__ import annotations

from govcheck.models import F03Checklist, Finding, Severity


def run_all(form: F03Checklist, cfg: dict | None = None) -> list[Finding]:  # noqa: ARG001 - 對齊 Check 介面
    findings: list[Finding] = []
    if form is None or not form.sheet_present:
        return findings

    for item in form.items:
        if not item.is_done:
            continue  # 僅就「已完成檢核（是）」要求佐證；否/不適用不強制
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
        if not item.evidence_golive:
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
