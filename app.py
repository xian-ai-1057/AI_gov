"""AI 治理審查小幫手 — Streamlit 單機介面（Phase 1：F02 風險評鑑規則檢查）。

上傳提案單位填好的官方 F02 (.xlsm) → 重算並比對風險分數 → 跑規則檢查 → 顯示結構化初判報告。
資料全程地端，不外送雲端。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import streamlit as st

from govcheck.models import Severity
from govcheck.report.builder import to_markdown
from govcheck.review.engine import review_f02

st.set_page_config(page_title="AI 治理審查小幫手", page_icon="🛡️", layout="wide")

st.title("🛡️ AI 治理審查小幫手")
st.caption("Phase 1 · F02 AI 風險評鑑規則檢查")
st.warning("⚠️ 本工具產出為 **AI 初判草稿**，需治理人員與三遵人工覆核；最終判定權不在 AI。資料全程地端不外送。")

uploaded = st.file_uploader(
    "上傳提案單位填好的 **附件二 F02 AI風險評鑑（.xlsm）**",
    type=["xlsm", "xlsx"],
)

if uploaded is None:
    st.info("請上傳 F02 檔案以開始初步審查。")
    st.stop()

# 暫存上傳檔供解析；用後即刪，避免提案者敏感資料殘留在本機（資料全程地端、最小足跡）。
tmp_path: str | None = None
try:
    with tempfile.NamedTemporaryFile(suffix=Path(uploaded.name).suffix, delete=False) as tmp:
        tmp.write(uploaded.getbuffer())
        tmp_path = tmp.name
    report = review_f02(tmp_path)
except Exception as exc:  # noqa: BLE001 - 介面層需把解析錯誤友善呈現
    st.error(f"解析或審查失敗：{exc}")
    st.stop()
finally:
    if tmp_path and os.path.exists(tmp_path):
        os.unlink(tmp_path)

# 摘要
c1, c2, c3 = st.columns(3)
c1.metric("🔴 錯誤", report.error_count)
c2.metric("🟡 提醒", report.warn_count)
c3.metric("結果", "✅ 規則通過" if report.passed else "❌ 待處理")

st.divider()

_ICON = {Severity.ERROR: "🔴", Severity.WARN: "🟡", Severity.INFO: "🟢"}
for f in report.findings:
    label = {Severity.ERROR: "錯誤", Severity.WARN: "提醒", Severity.INFO: "通過"}[f.severity]
    with st.expander(f"{_ICON[f.severity]} [{label}] {f.title}", expanded=f.severity is Severity.ERROR):
        if f.location:
            st.write(f"**位置**：{f.location}")
        if f.expected is not None or f.actual is not None:
            st.write(f"**期望**：{f.expected or '—'}　**實際**：{f.actual or '—'}")
        st.write(f"**說明**：{f.message}")
        st.caption(f"代碼 `{f.code}` · 需人工覆核")

st.divider()
st.download_button(
    "⬇️ 下載 Markdown 報告",
    data=to_markdown(report),
    file_name=f"F02_審查報告_{Path(uploaded.name).stem}.md",
    mime="text/markdown",
)
