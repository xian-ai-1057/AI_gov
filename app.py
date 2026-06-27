"""AI 治理審查小幫手 — Streamlit 單機介面（Phase 2：送件包初步審查）。

上傳提案單位的 F01 系統資訊表 / F02 風險評鑑 / F03 上線檢核表（+佐證）→
缺件檢查 + F01 必填 + F02 規則 + 跨表一致性 → 顯示結構化初判報告。
資料全程地端，不外送雲端。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import streamlit as st

from govcheck.models import Severity
from govcheck.report.builder import ICON, LABEL, to_markdown
from govcheck.review.engine import review_submission

st.set_page_config(page_title="AI 治理審查小幫手", page_icon="🛡️", layout="wide")

st.title("🛡️ AI 治理審查小幫手")
st.caption("Phase 2 · 送件包初步審查（缺件 + F01 必填 + F02 規則 + 跨表一致性）")
st.warning("⚠️ 本工具產出為 **AI 初判草稿**，需治理人員與三遵人工覆核；最終判定權不在 AI。資料全程地端不外送。")

st.subheader("上傳送件包")
col1, col2, col3 = st.columns(3)
with col1:
    up_f01 = st.file_uploader("附件一 **F01 系統資訊表**（.xlsx）", type=["xlsx"], key="f01")
with col2:
    up_f02 = st.file_uploader("附件二 **F02 風險評鑑**（.xlsm）", type=["xlsm", "xlsx"], key="f02")
with col3:
    up_f03 = st.file_uploader("附件三 **F03 上線檢核表**（.xlsx）", type=["xlsx"], key="f03")

up_support = st.file_uploader(
    "佐證文件（可多檔；R05 委外／R06 可解釋性／R07 公平性等）", accept_multiple_files=True, key="support",
)

if up_f01 is None and up_f02 is None and up_f03 is None:
    st.info("請至少上傳一份表單以開始初步審查。缺哪份系統會在報告中標示。")
    st.stop()

# 暫存上傳檔供解析；用後即刪，避免提案者敏感資料殘留在本機（資料全程地端、最小足跡）。
tmp_paths: list[str] = []


def _spill(uploaded) -> str | None:
    if uploaded is None:
        return None
    with tempfile.NamedTemporaryFile(suffix=Path(uploaded.name).suffix, delete=False) as tmp:
        tmp.write(uploaded.getbuffer())
        tmp_paths.append(tmp.name)
        return tmp.name


try:
    files = {"f01": _spill(up_f01), "f02": _spill(up_f02), "f03": _spill(up_f03)}
    files = {k: v for k, v in files.items() if v is not None}
    supporting = [f.name for f in (up_support or [])]
    report = review_submission(files, supporting=supporting)
except Exception as exc:  # noqa: BLE001 - 介面層需把解析錯誤友善呈現
    st.error(f"解析或審查失敗：{exc}")
    st.stop()
finally:
    for p in tmp_paths:
        if os.path.exists(p):
            os.unlink(p)

# 摘要
c1, c2, c3 = st.columns(3)
c1.metric("🔴 錯誤", report.error_count)
c2.metric("🟡 提醒", report.warn_count)
c3.metric("結果", "✅ 規則通過" if report.passed else "❌ 待處理")

st.divider()

for f in report.findings:
    with st.expander(f"{ICON[f.severity]} [{LABEL[f.severity]}] {f.title}", expanded=f.severity is Severity.ERROR):
        if f.location:
            st.write(f"**位置**：{f.location}")
        if f.expected is not None or f.actual is not None:
            st.write(f"**期望**：{f.expected or '—'}　**實際**：{f.actual or '—'}")
        st.write(f"**說明**：{f.message}")
        st.caption(f"代碼 `{f.code}` · 需人工覆核")

st.divider()
# 專案名稱可能含 "/"（欄名即「專案/服務名稱」），清洗避免瀏覽器把它當路徑分隔
_safe_subject = (report.subject or "未標示").replace("/", "_").replace("\\", "_")
st.download_button(
    "⬇️ 下載 Markdown 報告",
    data=to_markdown(report),
    file_name=f"送件包_審查報告_{_safe_subject}.md",
    mime="text/markdown",
)
