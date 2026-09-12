"""把 ReviewReport 轉成 Markdown（供 CLI / 下載 / Streamlit 顯示）。"""

from __future__ import annotations

from govcheck.models import ReviewReport, Severity

# severity → 圖示 / 中文標籤的單一事實來源；report 與 UI 共用，避免兩處字樣不一致。
ICON = {Severity.ERROR: "🔴", Severity.WARN: "🟡", Severity.INFO: "🟢"}
LABEL = {Severity.ERROR: "錯誤", Severity.WARN: "提醒", Severity.INFO: "通過"}
_ICON = ICON
_LABEL = LABEL


def to_markdown(report: ReviewReport) -> str:
    decision = {
        "approved": "✅ 可進下一步",
        "changes_required": "❌ 需補正",
        "manual_review": "👤 待人工覆核",
        "incomplete": "⚠️ 審查未完成",
    }[report.decision_status.value]
    lines = [
        f"# {report.form_type} 初步審查報告",
        "",
        f"> {report.banner}",
        "",
        f"**受審對象**：{report.subject or '（未標示）'}　|　"
        f"**錯誤 {report.error_count}**　提醒 {report.warn_count}　"
        f"結果：{decision}",
        "",
    ]
    if report.risk_grade:
        pct = f"（{report.risk_score:.1f}%）" if report.risk_score is not None else ""
        lines.append(f"**固有風險分級**：{report.risk_grade}{pct}")
        lines.append("")
    lines += _coverage_lines(report)
    if report.item_assessments:
        lines += [
            "## F03 分項評估",
            "",
            "| 項次 | Profile | 完整性 | 合理性 | 佐證 | 衝突 | 信心 | 引用位置 |",
            "| --- | --- | --- | --- | --- | --- | ---: | --- |",
        ]
        for row in report.item_assessments:
            refs = "、".join(
                f"{citation.document_id} {citation.locator_type}:{citation.locator_value}"
                for citation in row.citations
            ) or "—"
            values = [
                row.item_id,
                row.profile,
                row.completeness,
                row.reasonableness,
                row.evidence_status.value,
                row.conflict_status.value,
                f"{row.confidence:.2f}",
                refs,
            ]
            lines.append("| " + " | ".join(_table_cell(value) for value in values) + " |")
        lines.append("")
        recommendations = [
            (row.item_id, recommendation)
            for row in report.item_assessments
            for recommendation in row.recommendations
        ]
        if recommendations:
            lines += ["### 修改建議", ""]
            lines.extend(
                f"- **{item_id}**：{recommendation}"
                for item_id, recommendation in recommendations
            )
            lines.append("")
    lines += ["---", ""]
    for i, f in enumerate(report.findings, 1):
        lines.append(f"### {i}. {_ICON[f.severity]} [{_LABEL[f.severity]}] {f.title}")
        if f.location:
            lines.append(f"- **位置**：{f.location}")
        if f.expected is not None or f.actual is not None:
            lines.append(f"- **期望**：{f.expected or '—'}　**實際**：{f.actual or '—'}")
        if "\n" in f.message:
            # 多行說明（如 LLM 佐證彙整表的 Markdown 表格）需脫離清單項才能正常成表
            lines.append("- **說明**：")
            lines.append("")
            lines.append(f.message)
            lines.append("")
        else:
            lines.append(f"- **說明**：{f.message}")
        lines.append(f"- **代碼**：`{f.code}`　_(需人工覆核)_")
        lines.append("")
    return "\n".join(lines)


def _coverage_lines(report: ReviewReport) -> list[str]:
    """檢核表涵蓋範圍：把「沒被判讀的項次」明寫出來。

    分項評估表只涵蓋送進語意判讀的項次；不適用與佐證空白的項次不會出現在該表，
    若不在此列出，讀報告的人會誤以為整份檢核表都已判讀。
    """
    roster = report.checklist_roster
    if not roster:
        return []
    skipped = [row for row in roster if row.coverage != "assessable"]
    lines = [
        "## F03 檢核表涵蓋範圍",
        "",
        f"檢核項共 {len(roster)} 項，其中 {len(roster) - len(skipped)} 項送語意判讀、"
        f"{len(skipped)} 項未送（下列項次的狀態非合規結論，須人工認定）。",
        "",
    ]
    if skipped:
        lines += [
            "| 項次 | 管理議題 | 未送原因 | 提案單位說明 |",
            "| --- | --- | --- | --- |",
        ]
        for row in skipped:
            reason = (
                "標示不適用" if row.coverage == "not_applicable" else "佐證欄空白"
            )
            values = [row.item_id, row.topic or "—", reason, row.reason or "—"]
            lines.append("| " + " | ".join(_table_cell(value) for value in values) + " |")
        lines.append("")
    return lines


def table_cell(value: object) -> str:
    """Markdown 表格儲存格逸出（管線符與換行）；審查報告與協填建議書共用。"""
    return str(value).replace("|", "\\|").replace("\n", " ")


_table_cell = table_cell
