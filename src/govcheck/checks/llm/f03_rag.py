"""F03 檢核項的 RAG 符合性 LLM 判讀（Phase 3，p3-03 §4.2）。

對「有兩段佐證且在 retrieval_map 有 canonical 對應」的檢核項，分批請 LLM 判讀：
兩段佐證合計是否涵蓋 canonical 條文摘錄的要求 → verdict ∈ {covered, gap, undetermined}。

結構鏡射 checks/llm/f03_evidence.py（batch 迴圈、連錯 3 中止、彙整表+摘要、永不 raise），差異：
- 判讀對象改「canonical 摘錄（來自 RetrievalMap，build 自官方模板）+ 兩段佐證」；
- verdict 白名單容錯（白名單外一律降 undetermined，不丟整批）；
- prompt 預算防護（CJK 字數 ≈ token 估算 → 自動降批 / 砍摘錄，只 log 計數）；
- TEMPLATE_REF_MODIFIED 比對（上傳 L 欄/描述 vs canonical，判讀仍以 canonical 為準）。

引用顯示一律由 canonical section_path 決定性推導（LLM 回的 gap_refs 僅作交集過濾提示，
不作顯示來源），避免模型憑空造出不存在的條號。判讀結果全程標「需人工覆核」。
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Callable

from govcheck.llm.client import ChatClient, LLMError, parse_json_object
from govcheck.logging_setup import get_logger
from govcheck.models import F03Checklist, F03ChecklistItem, Finding, Severity
from govcheck.rag.mapping import format_section_ref
from govcheck.rag.models import F03ItemRetrieval, RagConfig, RetrievalMap

log = get_logger("f03_rag")

_SYSTEM = (
    "你是台新銀行 AI 治理審查助理，協助治理人員做初步 F03 上線檢核表符合性判讀。"
    "請嚴格只輸出符合指定格式的 JSON，不要輸出任何多餘文字或說明。所有判讀僅供人工初步參考。"
)

_USER_TEMPLATE = """以下是 AI 系統上線檢核表中的多個檢核項，每項附「canonical 法規條文摘錄」（審查基準）、「提案規劃階段佐證」及「上線階段佐證」（提案單位填寫）。

【判讀準則】
請逐項判斷：該項兩段佐證（提案規劃 + 上線）合計，是否已充分涵蓋 canonical 條文摘錄所要求的治理事項。

verdict 只能填下列三個值之一：
- "covered"：佐證已充分涵蓋條文要求，無明顯缺口。
- "gap"：佐證存在缺口，未涵蓋條文要求。gap_refs 請從 canonical 摘錄中識別並填寫缺口對應條號（如 "R03 五、(二) 2"）。
- "undetermined"：佐證資訊不足或語意模糊，無法判定。

reason 請用一句繁體中文說明判讀依據。
每個檢核項都要在 results 陣列回一筆，以 item_id 對應；除指定 JSON 外不要輸出任何文字。

輸出格式（僅此一個 JSON 物件）：
{{"results":[{{"item_id":"<項次>","verdict":"covered|gap|undetermined","gap_refs":[],"reason":""}}]}}

【待判讀檢核項（JSON）】
{items_json}"""

# 連續這麼多「批」呼叫失敗 → 研判端點系統性異常，中止後續（鏡射 f03_evidence）
_MAX_CONSECUTIVE_ERRORS = 3

# prompt 預算上限（CJK 字數 ≈ token 估算；保守固定值，e2e 校準後若需調整由 Lead 統一處理）
_MAX_PROMPT_TOKENS = 6000

_VERDICT_WHITELIST = frozenset({"covered", "gap", "undetermined"})


def run_all(
    checklist: F03Checklist,
    retrieval_map: RetrievalMap,
    client: ChatClient | None,
    cfg: RagConfig,
    progress: Callable[[dict], None] | None = None,
) -> list[Finding]:
    """RAG 符合性判讀入口；永不 raise（任何未預期例外收斂為 Finding）。"""
    try:
        return _run_all_inner(checklist, retrieval_map, client, cfg, progress)
    except Exception as exc:  # noqa: BLE001 - 判讀層絕不讓例外逸出到 engine
        log.warning("f03 rag unexpected failure: %s", type(exc).__name__)
        return [Finding(
            severity=Severity.WARN,
            code="F03.RAG_ERROR",
            title="RAG 符合性判讀異常中止",
            message="RAG 符合性判讀發生未預期錯誤，已中止；其餘規則檢查不受影響，請人工覆核。",
            source="llm",
        )]


def _run_all_inner(
    checklist: F03Checklist,
    retrieval_map: RetrievalMap,
    client: ChatClient | None,
    cfg: RagConfig,
    progress: Callable[[dict], None] | None,
) -> list[Finding]:
    if client is None or checklist is None or not checklist.sheet_present:
        return []

    # 目標項 = 有兩段佐證之一 且 retrieval_map 有 canonical 對應；無則回空（不產摘要噪音）
    targets = [
        it for it in checklist.items
        if (it.evidence_proposal or it.evidence_golive) and it.item_id in retrieval_map.f03_items
    ]
    if not targets:
        return []

    truncated = max(0, len(targets) - cfg.max_items)
    if truncated:
        log.info("f03 rag max_items total=%d limit=%d truncated=%d",
                 len(targets), cfg.max_items, truncated)
    targets = targets[:cfg.max_items]

    findings: list[Finding] = []

    # ── TEMPLATE_REF_MODIFIED 比對（判讀前，逐項；判讀摘錄仍用 canonical）──
    for it in targets:
        canonical = retrieval_map.f03_items[it.item_id]
        if _template_modified(it, canonical):
            findings.append(Finding(
                severity=Severity.WARN,
                code="F03.TEMPLATE_REF_MODIFIED",
                title="F03 模板欄位疑似被修改",
                message=(f"項次 {it.item_id} 的「規範參考」或「檢查項目描述」與官方模板不符，"
                         "疑似被提案單位修改；判讀仍以官方模板（canonical）為準，請人工核查。"),
                location=it.loc,
                source="llm",
            ))

    # ── prompt 預算防護 + 組批 ──
    prepared = [(it, _payload_item(it, retrieval_map.f03_items[it.item_id], cfg)) for it in targets]
    chunks = _build_chunks(prepared, cfg)

    def _llm_step(done: int) -> None:
        if progress is not None:
            progress({"stage": "llm", "label": "RAG 符合性判讀", "done": done, "total": len(chunks)})

    table_rows: list[tuple[str, ...]] = []
    reviewed = 0
    gap_count = 0
    undet_count = 0
    item_errors = 0
    consecutive_errors = 0
    aborted = False

    for batch_idx, chunk in enumerate(chunks):
        log.debug("f03 rag batch %d/%d items=%d", batch_idx + 1, len(chunks), len(chunk))
        try:
            verdicts = _judge_batch(client, chunk)
        except Exception as exc:  # noqa: BLE001 - LLMError 之外的例外同樣視為本批失敗，不逸出
            item_errors += len(chunk)
            consecutive_errors += 1
            if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                log.warning("f03 rag aborted after %d consecutive errors", consecutive_errors)
                findings.append(Finding(
                    severity=Severity.WARN,
                    code="F03.RAG_ERROR",
                    title="RAG 符合性判讀中止",
                    message=(f"連續 {consecutive_errors} 批呼叫 LLM 端點失敗，研判端點異常，已中止；"
                             f"已審 {reviewed} 項，尚有未完成判讀項目。"),
                    location=chunk[0][0].loc,
                    source="llm",
                ))
                aborted = True
                _llm_step(batch_idx + 1)
                break
            # 單批失敗 → 記一筆並續審其餘批次。
            # 只記例外型別：LLMError 訊息可能夾帶端點回應內容，不可落 log（比照 f03_evidence）。
            log.warning("f03 rag batch %d failed: %s", batch_idx + 1, type(exc).__name__)
            locs = "、".join(it.loc for it, _ in chunk)
            findings.append(Finding(
                severity=Severity.INFO,
                code="F03.RAG_ITEM_ERROR",
                title="本批 RAG 判讀失敗（已略過）",
                message=f"本批 {len(chunk)} 項 RAG 符合性判讀失敗，已略過續審其餘批次（{locs}）。",
                location=chunk[0][0].loc,
                source="llm",
            ))
            for it, _ in chunk:
                table_rows.append(_row_cells(it, None, [], errored=True))
            _llm_step(batch_idx + 1)
            continue

        consecutive_errors = 0
        for it, _ in chunk:
            raw = verdicts.get(it.item_id)
            if raw is None:
                # 模型漏回此項 → 記一筆並在表中標示，其餘不受影響
                item_errors += 1
                findings.append(Finding(
                    severity=Severity.INFO,
                    code="F03.RAG_ITEM_ERROR",
                    title="單項 RAG 判讀缺漏（已略過）",
                    message="LLM 批次回應未涵蓋此項，已略過；其餘項目不受影響。",
                    location=it.loc,
                    source="llm",
                ))
                table_rows.append(_row_cells(it, None, [], errored=True))
                continue

            verdict = str(raw.get("verdict") or "").strip()
            if verdict not in _VERDICT_WHITELIST:
                verdict = "undetermined"  # 白名單容錯：非法值降級，不丟整批
            reviewed += 1

            canonical = retrieval_map.f03_items[it.item_id]
            refs = _display_refs(canonical, cfg, raw.get("gap_refs"))
            reason = str(raw.get("reason") or "").strip()

            if verdict == "gap":
                gap_count += 1
                msg = f"佐證未涵蓋下列條文要求：{'、'.join(refs)}。請補充佐證後送人工覆核。"
                if reason:
                    msg += f"（{reason}）"
                findings.append(Finding(
                    severity=Severity.WARN,
                    code="F03.RAG_GAP",
                    title="佐證與法規條文存在符合性缺口",
                    message=msg,
                    location=it.loc,
                    source="llm",
                ))
            elif verdict == "undetermined":
                undet_count += 1
                msg = "無法判定佐證是否涵蓋條文要求，請人工覆核。"
                if reason:
                    msg += f"（{reason}）"
                findings.append(Finding(
                    severity=Severity.INFO,
                    code="F03.RAG_UNDETERMINED",
                    title="佐證符合性無法判定",
                    message=msg,
                    location=it.loc,
                    source="llm",
                ))
            # covered：無個別 Finding，只入彙整表 + 摘要計數
            table_rows.append(_row_cells(it, verdict, refs, errored=False))
        _llm_step(batch_idx + 1)

    # ── 彙整表 + 摘要（恆出，若有目標項）──
    if table_rows:
        findings.append(Finding(
            severity=Severity.INFO,
            code="F03.RAG_TABLE",
            title="RAG 符合性判讀彙整表",
            message=_summary_table(table_rows),
            source="llm",
        ))

    log.info("f03 rag done reviewed=%d gap=%d undetermined=%d errors=%d",
             reviewed, gap_count, undet_count, item_errors)
    summary = (f"RAG 符合性判讀：審閱 {reviewed} 項，缺口 {gap_count} 項、"
               f"無法判定 {undet_count} 項，待人工覆核。")
    notes: list[str] = []
    if item_errors:
        notes.append(f"{item_errors} 項判讀失敗")
    if truncated:
        notes.append(f"未送審 {truncated} 項")
    if aborted:
        notes.append("端點異常已中止")
    if notes:
        summary += "（" + "；".join(notes) + "）"
    findings.append(Finding(
        severity=Severity.INFO,
        code="F03.RAG_SUMMARY",
        title="RAG 符合性判讀摘要",
        message=summary,
        source="llm",
    ))
    return findings


# ─────────────────────────────────────────────────────────────────────────────
# TEMPLATE_REF_MODIFIED 比對
# ─────────────────────────────────────────────────────────────────────────────


def _normalize_ref(s: str | None) -> str:
    """比對用正規化：NFKC（全形→半形）、去前後空白、內部連續空白壓縮為單一空格。"""
    if not s:
        return ""
    return " ".join(unicodedata.normalize("NFKC", s).split())


def _template_modified(item: F03ChecklistItem, canonical: F03ItemRetrieval) -> bool:
    """上傳 L 欄 / 檢查項目描述與 canonical 正規化後不符 → True。

    上傳側為空（含 regulation_ref_raw 欄位尚未存在 → getattr 回 None）時 skip 比對，
    避免 parser 尚未擴充或欄位未填時誤觸。
    """
    uploaded_ref = _normalize_ref(getattr(item, "regulation_ref_raw", None))
    if uploaded_ref and uploaded_ref != _normalize_ref(canonical.canonical_ref_raw):
        return True
    uploaded_desc = _normalize_ref(item.description)
    return bool(uploaded_desc and uploaded_desc != _normalize_ref(canonical.canonical_description))


# ─────────────────────────────────────────────────────────────────────────────
# prompt 組裝 + 預算防護
# ─────────────────────────────────────────────────────────────────────────────


def _payload_item(item: F03ChecklistItem, canonical: F03ItemRetrieval, cfg: RagConfig) -> dict:
    """組單項 prompt payload：canonical 描述/摘錄（截斷後）+ 上傳兩段佐證。"""
    sections = canonical.sections[:cfg.max_sections_per_item]
    dropped = len(canonical.sections) - len(sections)
    clipped = sum(1 for s in sections if len(s.excerpt) > cfg.max_excerpt_chars)
    if dropped or clipped:
        # 只記計數，不記摘錄內容
        log.info("f03 rag excerpt budget item=%s sections_dropped=%d excerpts_clipped=%d",
                 item.item_id, dropped, clipped)
    return {
        "item_id": item.item_id,
        "description": canonical.canonical_description or "（未提供）",
        "sections": [
            {
                "reg_code": s.reg_code,
                "section_path": s.section_path,
                "title": s.title,
                "excerpt": s.excerpt[:cfg.max_excerpt_chars],
            }
            for s in sections
        ],
        "proposal": item.evidence_proposal or "（空白）",
        "golive": item.evidence_golive or "（空白）",
    }


def _estimate_tokens(text: str) -> int:
    """CJK 字數 ≈ token；其餘字元約 4 字元 ≈ 1 token（保守估算）。"""
    cjk = sum(1 for c in text if 0x4E00 <= ord(c) <= 0x9FFF or 0x3000 <= ord(c) <= 0x30FF)
    rest = len(text) - cjk
    return cjk + rest // 4


def _build_chunks(
    prepared: list[tuple[F03ChecklistItem, dict]],
    cfg: RagConfig,
) -> list[list[tuple[F03ChecklistItem, dict]]]:
    """依 batch_size 組批；估算超過 prompt 預算的批自動降為單項批（log 只記數字）。"""
    size = max(1, int(cfg.batch_size))
    template_overhead = _estimate_tokens(_SYSTEM + _USER_TEMPLATE)
    est = {id(p): _estimate_tokens(json.dumps(p, ensure_ascii=False)) for _, p in prepared}

    chunks: list[list[tuple[F03ChecklistItem, dict]]] = []
    reduced = 0
    for i in range(0, len(prepared), size):
        chunk = prepared[i:i + size]
        chunk_est = template_overhead + sum(est[id(p)] for _, p in chunk)
        if len(chunk) > 1 and chunk_est > _MAX_PROMPT_TOKENS:
            reduced += 1
            log.info("f03 rag budget split batch_idx=%d est_tokens=%d items=%d",
                     len(chunks), chunk_est, len(chunk))
            chunks.extend([pair] for pair in chunk)
        else:
            chunks.append(chunk)
    if reduced:
        log.info("f03 rag budget reduced_batches=%d total_batches=%d", reduced, len(chunks))
    return chunks


def _judge_batch(
    client: ChatClient,
    chunk: list[tuple[F03ChecklistItem, dict]],
) -> dict[str, dict]:
    """送一批給 LLM，回傳 {item_id: 原始 result dict}；失敗或格式異常丟 LLMError。"""
    payload_items = [payload for _, payload in chunk]
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _USER_TEMPLATE.format(
            items_json=json.dumps(payload_items, ensure_ascii=False, indent=2),
        )},
    ]
    obj = parse_json_object(client.chat(messages))
    results = obj.get("results")
    if not isinstance(results, list):
        raise LLMError("RAG 批次回應缺少 results 陣列")
    by_id: dict[str, dict] = {}
    for r in results:
        if isinstance(r, dict) and r.get("item_id") is not None:
            by_id[str(r["item_id"]).strip()] = r
    return by_id


# ─────────────────────────────────────────────────────────────────────────────
# 引用顯示（canonical 決定性推導）與彙整表
# ─────────────────────────────────────────────────────────────────────────────


def _display_refs(canonical: F03ItemRetrieval, cfg: RagConfig, gap_refs) -> list[str]:
    """引用顯示字串：一律由 canonical section_path 決定性推導。

    LLM 回的 gap_refs 僅作交集過濾提示：非空時取「gap_refs ∩ canonical 顯示字串」
    （正規化後比對、保 canonical 順序）；交集為空或 gap_refs 空 → 列全部 canonical 引用。
    """
    all_refs = [
        format_section_ref(s.reg_code, s.section_path)
        for s in canonical.sections[:cfg.max_sections_per_item]
    ]
    if isinstance(gap_refs, list) and gap_refs:
        wanted = {_normalize_ref(str(g)) for g in gap_refs}
        filtered = [r for r in all_refs if _normalize_ref(r) in wanted]
        if filtered:
            return filtered
    return all_refs


def _row_cells(
    item: F03ChecklistItem,
    verdict: str | None,
    refs: list[str],
    *,
    errored: bool,
) -> tuple[str, ...]:
    """組一列彙整表儲存格：項次｜管理議題｜符合性判讀｜條文引用。"""
    item_id = item.item_id
    topic = item.topic or "—"
    if errored or verdict is None:
        return (item_id, topic, "⚠️ 判讀失敗", "—")
    verdict_cell = {"covered": "✅ 已涵蓋", "gap": "🟡 缺口", "undetermined": "❓ 無法判定"}[verdict]
    refs_cell = "；".join(refs) if refs else "—"
    return (item_id, topic, verdict_cell, refs_cell)


def _summary_table(rows: list[tuple[str, ...]]) -> str:
    header = "| 項次 | 管理議題 | 符合性判讀 | 條文引用 |"
    sep = "| --- | --- | --- | --- |"
    body = [f"| {' | '.join(_cell(c) for c in r)} |" for r in rows]
    return "\n".join([header, sep, *body])


def _cell(text) -> str:
    """正規化表格儲存格：去除會破壞 Markdown 表格的 | 與換行。"""
    s = "" if text is None else str(text)
    s = s.replace("|", "／").replace("\n", " ").replace("\r", " ").strip()
    return s or "—"
