"""F03 兩段佐證的 LLM 判讀檢查（Phase 3）。

針對「有填佐證」的檢核項，請 LLM：
1) 比較提案規劃階段（I 欄）與上線階段（J 欄）佐證的差異，提醒對應檢核項；
2) 標示說明過於草率/不明確者。
任何端點/解析失敗一律降級為 Finding（絕不 raise），確保介面與規則檢查不中斷。
判讀結果全程標「需人工覆核」，最終判定權不在 AI。
"""

from __future__ import annotations

from govcheck.llm.client import ChatClient, LLMError, parse_json_object
from govcheck.models import F03Checklist, F03ChecklistItem, Finding, Severity

_SYSTEM = (
    "你是台新銀行 AI 治理審查助理，協助治理人員做初步審查。"
    "請嚴格只輸出符合指定格式的 JSON，不要輸出任何多餘文字或說明。所有判讀僅供人工初步參考。"
)

_USER_TEMPLATE = """以下是 AI 系統上線檢核表中的一個檢核項，附兩段佐證說明。請依準則判讀並「只輸出 JSON」。

判讀準則：
1) difference：比較「提案規劃階段佐證」與「上線階段佐證」是否存在實質差異；上線階段（依表格要求）應列出與提案的差異。若上線階段為空、或僅複述提案而未說明差異、或內容與提案有實質出入卻未交代，flag 設 true，summary 用一句繁體中文說明。
2) proposal：判斷「提案規劃階段佐證」是否過於草率或不明確（例如僅「已完成」「OK」「無」「同上」等空泛字眼、或與檢查項目無實質對應）。vague 為 true 時，reason 用一句繁體中文說明。
3) golive：以同樣準則判斷「上線階段佐證」。

輸出格式（僅此一個 JSON 物件）：
{{"difference":{{"flag":false,"summary":""}},"proposal":{{"vague":false,"reason":""}},"golive":{{"vague":false,"reason":""}}}}

【檢查項目】{description}
【提案規劃階段佐證】{proposal}
【上線階段佐證】{golive}"""


# 連續這麼多項呼叫失敗 → 研判端點系統性異常，中止後續以免拖長（最壞耗時約 N×timeout）
_MAX_CONSECUTIVE_ERRORS = 3


def run_all(form: F03Checklist, client: ChatClient | None, max_items: int = 30) -> list[Finding]:
    """對有填佐證的檢核項做 LLM 判讀，回傳 Finding 清單（source=llm）。"""
    if client is None or form is None or not form.sheet_present:
        return []

    targets = [it for it in form.items if it.evidence_proposal or it.evidence_golive]
    if not targets:
        return []  # 無佐證可審 → 不產生摘要噪音；空白/缺漏由 f03_evidence_presence 規則檢查負責

    truncated = max(0, len(targets) - max_items)
    targets = targets[:max_items]

    findings: list[Finding] = []
    reviewed = 0
    item_errors = 0
    consecutive_errors = 0
    for item in targets:
        try:
            verdict = _judge(client, item)
        except LLMError as exc:
            item_errors += 1
            consecutive_errors += 1
            if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                findings.append(Finding(
                    severity=Severity.WARN,
                    code="F03.LLM_ERROR",
                    title="LLM 佐證審查中止",
                    message=(f"連續 {consecutive_errors} 項呼叫 LLM 端點失敗，研判端點異常，已中止；"
                             f"已審 {reviewed} 項，尚有 {len(targets) - reviewed} 項未完成判讀：{exc}"),
                    location=item.loc,
                    source="llm",
                ))
                break
            # 單項失敗（如逾時/單項過長）→ 記一筆並續審其餘項，避免一項拖垮整體召回
            findings.append(Finding(
                severity=Severity.INFO,
                code="F03.LLM_ITEM_ERROR",
                title="單項 LLM 判讀失敗（已略過）",
                message=f"此項 LLM 判讀失敗，已略過續審其餘項目：{exc}",
                location=item.loc,
                source="llm",
            ))
            continue
        consecutive_errors = 0
        reviewed += 1
        findings.extend(_map_verdict(item, verdict))

    flagged = sum(1 for f in findings if f.severity is Severity.WARN and f.code != "F03.LLM_ERROR")
    summary = f"LLM 佐證審查：審閱 {reviewed} 項，標示 {flagged} 項待人工覆核。"
    notes: list[str] = []
    if item_errors:
        notes.append(f"{item_errors} 項判讀失敗")
    if truncated:
        notes.append(f"超過單次上限 {max_items}，未送審 {truncated} 項")
    if notes:
        summary += "（" + "；".join(notes) + "）"
    findings.append(Finding(
        severity=Severity.INFO,
        code="F03.LLM_SUMMARY",
        title="LLM 佐證審查摘要",
        message=summary,
        source="llm",
    ))
    return findings


def _judge(client: ChatClient, item: F03ChecklistItem) -> dict:
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _USER_TEMPLATE.format(
            description=item.description or "（未提供）",
            proposal=item.evidence_proposal or "（空白）",
            golive=item.evidence_golive or "（空白）",
        )},
    ]
    return parse_json_object(client.chat(messages))


def _map_verdict(item: F03ChecklistItem, verdict: dict) -> list[Finding]:
    loc = item.loc
    out: list[Finding] = []

    diff = verdict.get("difference") or {}
    if isinstance(diff, dict) and diff.get("flag"):
        out.append(Finding(
            severity=Severity.WARN,
            code="F03.LLM_DIFF",
            title="提案與上線階段佐證可能存在差異",
            message=str(diff.get("summary") or "上線階段佐證未清楚列出與提案的差異，請確認。"),
            location=loc,
            source="llm",
        ))

    prop = verdict.get("proposal") or {}
    if isinstance(prop, dict) and prop.get("vague"):
        out.append(Finding(
            severity=Severity.WARN,
            code="F03.LLM_VAGUE_PROPOSAL",
            title="提案規劃階段佐證過於草率/不明確",
            message=str(prop.get("reason") or "說明過於空泛或與檢查項目無實質對應，請補充具體佐證。"),
            location=loc,
            actual=_excerpt(item.evidence_proposal),
            source="llm",
        ))

    gol = verdict.get("golive") or {}
    if isinstance(gol, dict) and gol.get("vague"):
        out.append(Finding(
            severity=Severity.WARN,
            code="F03.LLM_VAGUE_GOLIVE",
            title="上線階段佐證過於草率/不明確",
            message=str(gol.get("reason") or "說明過於空泛或與檢查項目無實質對應，請補充具體佐證。"),
            location=loc,
            actual=_excerpt(item.evidence_golive),
            source="llm",
        ))
    return out


def _excerpt(text: str | None, limit: int = 80) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"
