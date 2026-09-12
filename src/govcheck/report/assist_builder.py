"""把 AssistResult 轉成「填寫建議書」Markdown（供下載、對照抄寫到官方 Excel）。"""

from __future__ import annotations

from collections import Counter

from govcheck.assist.models import AssistResult, Suggestion
from govcheck.models import Citation
from govcheck.report.builder import table_cell

TITLE = "# 填寫建議書（AI 協填草稿）"
DISCLAIMER = (
    "本建議書供對照填寫官方 Excel 之用，AI 建議僅供參考；官方表單仍由您親自填寫、"
    "由單位主管核可；本工具不代填、不匯出表單；文件未涵蓋的欄位如實標示，不憑空生成。"
)
UNCOVERED_CELL = "—（文件未涵蓋，請自行填寫）"

FORM_LABELS = {
    "f01": "F01 AI系統資訊表",
    "f02": "F02 AI風險評鑑",
    "f03": "F03 上線檢核表",
}
MODE_LABELS = {
    "llm": "AI 建議（LLM）",
    "retrieval_only": "僅候選出處（未產生 AI 建議）",
    "unavailable": "不可用",
}


def to_markdown(result: AssistResult) -> str:
    covered = [row for row in result.suggestions if row.covered]
    uncovered = [row for row in result.suggestions if not row.covered]
    parsed = sum(1 for doc in result.document_statuses if doc.status.value == "parsed")
    lines = [
        TITLE,
        "",
        f"> {DISCLAIMER}",
        "",
        f"**協填編號**：{result.assist_id}　|　"
        f"**模式**：{MODE_LABELS.get(result.mode, result.mode)}　|　"
        f"**欄位** {len(result.suggestions)}（已建議 {len(covered)} / 未涵蓋 {len(uncovered)}）　|　"
        f"**佐證文件** {parsed}/{len(result.document_statuses)} 份可讀",
        "",
    ]
    if result.notices:
        lines += ["## 提示", ""]
        lines += [f"- `{notice.code}` {notice.title}：{notice.message}" for notice in result.notices]
        lines.append("")
    for form in result.forms:
        rows = [row for row in result.suggestions if row.form == form]
        lines += [f"## {FORM_LABELS.get(form, form.upper())}", ""]
        if not rows:
            lines += ["（本表沒有可處理的欄位）", ""]
            continue
        lines += [
            "| 欄位（儲存格） | 建議內容 | 依據出處 | 信心 |",
            "| --- | --- | --- | ---: |",
        ]
        lines += [_table_row(row) for row in rows]
        lines.append("")
    lines += ["## 未涵蓋欄位", ""]
    if uncovered:
        lines += [
            f"- {FORM_LABELS.get(row.form, row.form.upper())}：{row.label}（{row.cell}）"
            for row in uncovered
        ]
    else:
        lines.append("（無）")
    lines.append("")
    lines += _run_info(result)
    return "\n".join(lines).rstrip() + "\n"


def _table_row(row: Suggestion) -> str:
    citations = "；".join(_citation_text(citation) for citation in row.citations)
    if row.covered:
        values = [
            f"{row.label}（{row.cell}）",
            row.suggestion or "",
            citations or "—",
            f"{row.confidence:.2f}",
        ]
    else:
        values = [
            f"{row.label}（{row.cell}）",
            UNCOVERED_CELL,
            f"候選：{citations}" if citations else "—",
            "—",
        ]
    return "| " + " | ".join(table_cell(value) for value in values) + " |"


def _citation_text(citation: Citation) -> str:
    text = f"{citation.document_id} · {citation.locator_type}:{citation.locator_value}"
    if citation.quote:
        text += f" · 「{citation.quote}」"
    return text


def _run_info(result: AssistResult) -> list[str]:
    usage = result.usage
    status_counts = Counter(doc.status.value for doc in result.document_statuses)
    lines = ["## 執行資訊", ""]
    if result.prompt_refs:
        lines += [
            f"- prompt：`{ref.prompt_id}` 版本 {ref.prompt_version}"
            f"（schema {ref.schema_version}，hash {ref.content_hash[:12]}）"
            for ref in result.prompt_refs
        ]
    else:
        lines.append("- prompt：未使用模型")
    lines += [
        f"- 模型呼叫 {usage.review_llm_primary_calls} 次　"
        f"輸入 {usage.input_tokens} tokens　輸出 {usage.output_tokens} tokens　"
        f"耗時 {usage.latency_ms} ms",
        f"- 佐證檢索 embedding 呼叫 {usage.evidence_embedding_calls} 次",
        "- 佐證文件狀態："
        + ("、".join(f"{status} {count}" for status, count in sorted(status_counts.items()))
           or "（無）"),
    ]
    return lines
