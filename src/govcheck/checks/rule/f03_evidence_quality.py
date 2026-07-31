"""F03 佐證品質規則檢查（確定性、無網路）。

與 f03_evidence_presence 的分工：那邊管「有沒有填」，這邊管「填的東西明顯不足以檢核」。
兩者都是機械可判定的事實（字數、逐字相同），不做任何語意判讀——
「18 字」是治理人員可以自己核對的事實，「過於草率」不是。

這一層的價值在於：語意判讀要跑好幾分鐘，而這些缺陷 8 秒內就能掃出來，
讓治理人員在等待模型的同時就能先發補件通知。
"""

from __future__ import annotations

from govcheck.models import F03Checklist, Finding, Severity, WorkflowStage

# config 缺 f03.quality 區段時的門檻後備（與 review_config.yaml 的預設值一致）。
_DEFAULT_MIN_CHARS = 40


def _min_chars(cfg: dict | None) -> int:
    quality = ((cfg or {}).get("f03") or {}).get("quality") or {}
    return int(quality.get("min_evidence_chars", _DEFAULT_MIN_CHARS))


def _char_count(text: str | None) -> int:
    """去掉所有空白後的字數：排版空白不算內容，避免用換行灌字數。"""
    return len("".join((text or "").split()))


def run_all(
    form: F03Checklist,
    cfg: dict | None = None,
    workflow_stage: WorkflowStage = WorkflowStage.PROPOSAL,
) -> list[Finding]:
    findings: list[Finding] = []
    if form is None or not form.sheet_present:
        return findings

    threshold = _min_chars(cfg)
    is_golive = workflow_stage is not WorkflowStage.PROPOSAL

    for item in form.items:
        # 不適用的項次填的是「不適用理由」，不是佐證，不該用佐證長度門檻要求它。
        if item.check_state == "不適用":
            continue

        # 品質門檻檢查的是「本階段自己的欄位」：提案看 I、上線看 J。不能沿用
        # evidence_for_stage() 的 LLM fallback（上線時允許以提案內容作判讀上下文），否則
        # I 欄很完整、J 欄只寫幾個字時會被 I 欄遮住而漏報。
        text = item.evidence_golive if is_golive else item.evidence_proposal
        count = _char_count(text)
        # count == 0（完全沒填）由 f03_evidence_presence 的「佐證空白」負責，不重複警示。
        if 0 < count < threshold:
            findings.append(Finding(
                severity=Severity.WARN,
                code="F03.EVIDENCE_TOO_SHORT",
                title="佐證說明過短，不足以檢核",
                message=(
                    f"此檢核項的佐證說明僅 {count} 字（門檻 {threshold} 字），"
                    "無法據以判斷做法、負責單位與執行頻率；請補述具體做法或指明佐證文件。"
                ),
                location=item.loc,
                expected=f"至少 {threshold} 字的具體說明",
                actual=f"{count} 字",
            ))

        if not is_golive:
            continue
        # 上線佐證逐字照抄提案 = 沒有交代上線後的實況。這是規則式文字比對（見
        # F03ChecklistItem.golive_diff_for_stage），不是語意判讀。
        if item.golive_diff_for_stage(workflow_stage) == "same":
            findings.append(Finding(
                severity=Severity.WARN,
                code="F03.EVIDENCE_COPIED",
                title="上線階段佐證與提案階段完全相同",
                message=(
                    "『前項佐證說明(上線階段;列出與提案差異)』與提案階段內容逐字相同，"
                    "等於未交代上線後的實際做法與差異；請改填上線後的實況，"
                    "若確實無差異請明確敘明並附上線後的佐證。"
                ),
                location=item.loc,
                expected="上線階段實況與差異說明",
                actual="與提案階段完全相同",
            ))
    return findings
