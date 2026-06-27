"""把 ReviewReport 轉成 Markdown（供 CLI / 下載 / Streamlit 顯示）。"""

from __future__ import annotations

from govcheck.models import ReviewReport, Severity

# severity → 圖示 / 中文標籤的單一事實來源；report 與 UI 共用，避免兩處字樣不一致。
ICON = {Severity.ERROR: "🔴", Severity.WARN: "🟡", Severity.INFO: "🟢"}
LABEL = {Severity.ERROR: "錯誤", Severity.WARN: "提醒", Severity.INFO: "通過"}
_ICON = ICON
_LABEL = LABEL


def to_markdown(report: ReviewReport) -> str:
    lines = [
        f"# {report.form_type} 初步審查報告",
        "",
        f"> {report.banner}",
        "",
        f"**受審對象**：{report.subject or '（未標示）'}　|　"
        f"**錯誤 {report.error_count}**　提醒 {report.warn_count}　"
        f"結果：{'✅ 規則檢查通過' if report.passed else '❌ 有須處理項目'}",
        "",
        "---",
        "",
    ]
    for i, f in enumerate(report.findings, 1):
        lines.append(f"### {i}. {_ICON[f.severity]} [{_LABEL[f.severity]}] {f.title}")
        if f.location:
            lines.append(f"- **位置**：{f.location}")
        if f.expected is not None or f.actual is not None:
            lines.append(f"- **期望**：{f.expected or '—'}　**實際**：{f.actual or '—'}")
        lines.append(f"- **說明**：{f.message}")
        lines.append(f"- **代碼**：`{f.code}`　_(需人工覆核)_")
        lines.append("")
    return "\n".join(lines)
