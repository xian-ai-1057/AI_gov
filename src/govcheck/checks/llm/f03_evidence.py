"""F03 兩段佐證的 LLM 判讀檢查（Phase 3）。

針對「有填佐證」的檢核項，分批請 LLM：
1) 比較提案規劃階段（I 欄）與上線階段（J 欄）佐證的差異，提醒對應檢核項；
2) 標示說明過於草率/不明確者。
為減少呼叫數，採「分塊批次」：一次送多項，回一個含 results 陣列的 JSON，再以 item_id 對回。
除逐項 Finding 外，另產出一張跨項「彙整表」（F03.LLM_TABLE）供快速總覽。
任何端點/解析失敗一律降級為 Finding（絕不 raise），確保介面與規則檢查不中斷。
判讀結果全程標「需人工覆核」，最終判定權不在 AI。
"""

from __future__ import annotations

import json

from govcheck.llm.client import ChatClient, LLMError, parse_json_object
from govcheck.models import F03Checklist, F03ChecklistItem, Finding, Severity

_SYSTEM = (
    "你是台新銀行 AI 治理審查助理，協助治理人員做初步審查。"
    "請嚴格只輸出符合指定格式的 JSON，不要輸出任何多餘文字或說明。所有判讀僅供人工初步參考。"
)

_USER_TEMPLATE = """以下是 AI 系統上線檢核表中的多個檢核項，每項附「提案規劃階段佐證」與「上線階段佐證」。請逐項依準則判讀並「只輸出一個 JSON 物件」。

判讀準則：
1) difference：比較該項「提案規劃階段佐證」與「上線階段佐證」是否存在實質差異；上線階段（依表格要求）應列出與提案的差異。若上線階段為空、或僅複述提案而未說明差異、或內容與提案有實質出入卻未交代，flag 設 true，summary 用一句繁體中文說明。
2) proposal：判斷該項「提案規劃階段佐證」是否過於草率或不明確（例如僅「已完成」「OK」「無」「同上」等空泛字眼、或與檢查項目無實質對應）。vague 為 true 時，reason 用一句繁體中文說明。
3) golive：以同樣準則判斷該項「上線階段佐證」。

每個檢核項都要在 results 陣列回一筆，並以 item_id 對應；除指定 JSON 外不要輸出任何文字。

輸出格式（僅此一個 JSON 物件）：
{{"results":[{{"item_id":"<項次>","difference":{{"flag":false,"summary":""}},"proposal":{{"vague":false,"reason":""}},"golive":{{"vague":false,"reason":""}}}}]}}

【待判讀檢核項（JSON）】
{items_json}"""


# 連續這麼多「批」呼叫失敗 → 研判端點系統性異常，中止後續以免拖長（最壞耗時約 N×timeout）
_MAX_CONSECUTIVE_ERRORS = 3


def run_all(
    form: F03Checklist,
    client: ChatClient | None,
    max_items: int = 30,
    batch_size: int = 8,
) -> list[Finding]:
    """對有填佐證的檢核項做分批 LLM 判讀，回傳 Finding 清單（source=llm）。

    max_items：送 LLM 的檢核項總上限（安全閥）。
    batch_size：單次 LLM 呼叫送審的檢核項數（批次以減少呼叫）。
    """
    if client is None or form is None or not form.sheet_present:
        return []

    targets = [it for it in form.items if it.evidence_proposal or it.evidence_golive]
    if not targets:
        return []  # 無佐證可審 → 不產生摘要噪音；空白/缺漏由 f03_evidence_presence 規則檢查負責

    truncated = max(0, len(targets) - max_items)
    targets = targets[:max_items]

    size = max(1, int(batch_size))
    chunks = [targets[i:i + size] for i in range(0, len(targets), size)]

    findings: list[Finding] = []
    table_rows: list[tuple[str, ...]] = []
    reviewed = 0
    item_errors = 0
    consecutive_errors = 0
    aborted = False

    for chunk in chunks:
        try:
            verdicts = _judge_batch(client, chunk)
        except LLMError as exc:
            item_errors += len(chunk)
            consecutive_errors += 1
            if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                findings.append(Finding(
                    severity=Severity.WARN,
                    code="F03.LLM_ERROR",
                    title="LLM 佐證審查中止",
                    message=(f"連續 {consecutive_errors} 批呼叫 LLM 端點失敗，研判端點異常，已中止；"
                             f"已審 {reviewed} 項，尚有未完成判讀項目：{exc}"),
                    location=chunk[0].loc,
                    source="llm",
                ))
                aborted = True
                break
            # 單批失敗（如逾時/批量過長）→ 記一筆並續審其餘批次，避免一批拖垮整體召回
            locs = "、".join(it.loc for it in chunk)
            findings.append(Finding(
                severity=Severity.INFO,
                code="F03.LLM_ITEM_ERROR",
                title="本批 LLM 判讀失敗（已略過）",
                message=f"本批 {len(chunk)} 項 LLM 判讀失敗，已略過續審其餘批次（{locs}）：{exc}",
                location=chunk[0].loc,
                source="llm",
            ))
            for it in chunk:
                table_rows.append(_row_cells(it, None, errored=True))
            continue

        consecutive_errors = 0
        for it in chunk:
            verdict = verdicts.get(it.item_id)
            if verdict is None:
                # 模型漏回此項 → 記一筆並在表中標示，其餘不受影響
                item_errors += 1
                findings.append(Finding(
                    severity=Severity.INFO,
                    code="F03.LLM_ITEM_ERROR",
                    title="單項 LLM 判讀缺漏（已略過）",
                    message="LLM 批次回應未涵蓋此項，已略過；其餘項目不受影響。",
                    location=it.loc,
                    source="llm",
                ))
                table_rows.append(_row_cells(it, None, errored=True))
                continue
            reviewed += 1
            findings.extend(_map_verdict(it, verdict))
            table_rows.append(_row_cells(it, verdict, errored=False))

    if table_rows:
        findings.append(Finding(
            severity=Severity.INFO,
            code="F03.LLM_TABLE",
            title="LLM 佐證審查彙整表",
            message=_summary_table(table_rows),
            source="llm",
        ))

    flagged = sum(1 for f in findings if f.severity is Severity.WARN and f.code != "F03.LLM_ERROR")
    summary = f"LLM 佐證審查：審閱 {reviewed} 項，標示 {flagged} 項待人工覆核。"
    notes: list[str] = []
    if item_errors:
        notes.append(f"{item_errors} 項判讀失敗")
    if truncated:
        notes.append(f"超過單次上限 {max_items}，未送審 {truncated} 項")
    if aborted:
        notes.append("端點異常已中止")
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


def _judge_batch(client: ChatClient, chunk: list[F03ChecklistItem]) -> dict[str, dict]:
    """送一批檢核項給 LLM，回傳 {item_id: verdict} 對應；失敗或格式異常丟 LLMError。"""
    payload_items = [
        {
            "item_id": it.item_id,
            "description": it.description or "（未提供）",
            "proposal": it.evidence_proposal or "（空白）",
            "golive": it.evidence_golive or "（空白）",
        }
        for it in chunk
    ]
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _USER_TEMPLATE.format(
            items_json=json.dumps(payload_items, ensure_ascii=False, indent=2),
        )},
    ]
    obj = parse_json_object(client.chat(messages))
    results = obj.get("results")
    if not isinstance(results, list):
        raise LLMError("LLM 批次回應缺少 results 陣列")
    by_id: dict[str, dict] = {}
    for r in results:
        if isinstance(r, dict) and r.get("item_id") is not None:
            by_id[str(r["item_id"]).strip()] = r
    return by_id


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


def _row_cells(item: F03ChecklistItem, verdict: dict | None, *, errored: bool) -> tuple[str, ...]:
    """組一列彙整表儲存格：項次｜管理議題｜提案↔上線差異｜提案佐證｜上線佐證。"""
    item_id = item.item_id
    topic = item.topic or "—"
    if errored or verdict is None:
        return (item_id, topic, "⚠️ 判讀失敗", "—", "—")

    diff = verdict.get("difference") or {}
    if isinstance(diff, dict) and diff.get("flag"):
        diff_cell = "🟡 " + (_excerpt(str(diff.get("summary") or "上線未說明差異"), 40) or "上線未說明差異")
    else:
        diff_cell = "—"
    prop_cell = _evidence_cell(item.evidence_proposal, verdict.get("proposal"))
    gol_cell = _evidence_cell(item.evidence_golive, verdict.get("golive"))
    return (item_id, topic, diff_cell, prop_cell, gol_cell)


def _evidence_cell(text: str | None, vinfo) -> str:
    info = vinfo if isinstance(vinfo, dict) else {}
    if info.get("vague"):
        return "🟡 " + (_excerpt(str(info.get("reason") or "過於空泛"), 40) or "過於空泛")
    return "✅" if text else "—"


def _summary_table(rows: list[tuple[str, ...]]) -> str:
    header = "| 項次 | 管理議題 | 提案↔上線差異 | 提案佐證 | 上線佐證 |"
    sep = "| --- | --- | --- | --- | --- |"
    body = [f"| {' | '.join(_cell(c) for c in r)} |" for r in rows]
    return "\n".join([header, sep, *body])


def _cell(text) -> str:
    """正規化表格儲存格：去除會破壞 Markdown 表格的 | 與換行。"""
    s = "" if text is None else str(text)
    s = s.replace("|", "／").replace("\n", " ").replace("\r", " ").strip()
    return s or "—"


def _excerpt(text: str | None, limit: int = 80) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"
