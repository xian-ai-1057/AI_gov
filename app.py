"""AI 治理審查小幫手 — Streamlit 單機介面（批次上傳自動分類 + 送件包初步審查）。

一次拖入送件包所有檔案（F01/F02/F03 + 佐證）→ 依工作表名稱自動分類 →
使用者確認/修正判定 → 缺件 + F01 必填 + F02 規則 + 跨表一致性 → 結構化初判報告。
資料全程地端，不外送雲端。
"""

from __future__ import annotations

import io
import tempfile
from pathlib import Path

import streamlit as st

from govcheck.classify import (
    KIND_LABEL,
    FileClassification,
    FileKind,
    classify_fileobj,
    route_classifications,
)
from govcheck.models import Severity
from govcheck.report.builder import ICON, LABEL, to_markdown
from govcheck.review.engine import review_routed

st.set_page_config(page_title="AI 治理審查小幫手", page_icon="🛡️", layout="wide")

st.title("🛡️ AI 治理審查小幫手")
st.caption("批次上傳自動分類 · 送件包初步審查（缺件 + F01 必填 + F02 規則 + 跨表一致性）")
st.warning("⚠️ 本工具產出為 **AI 初判草稿**，需治理人員與三遵人工覆核；最終判定權不在 AI。資料全程地端不外送。")

# 可指派的判定（UNKNOWN 不可手動指派；以「忽略此檔」表示排除）
_IGNORE = "忽略此檔"
_ASSIGNABLE = [FileKind.F01, FileKind.F02, FileKind.F03, FileKind.SUPPORTING]
_OPTIONS = [KIND_LABEL[k] for k in _ASSIGNABLE] + [_IGNORE]
_LABEL_TO_KIND = {KIND_LABEL[k]: k for k in _ASSIGNABLE}


def _default_label(c: FileClassification) -> str:
    # 無法辨識 → 預設「忽略此檔」，逼使用者人工指定，不靜默誤路由。
    return KIND_LABEL[c.kind] if c.kind in _LABEL_TO_KIND.values() else _IGNORE


st.subheader("上傳送件包")
ups = st.file_uploader(
    "一次拖入所有檔案（F01/F02/F03 + 佐證，可多檔）",
    accept_multiple_files=True,
    type=None,  # 接受所有格式；由分類器而非上傳框決定路由
    key="bundle",
)

if not ups:
    st.info("請上傳至少一個檔案。系統會自動辨識 F01/F02/F03 與佐證，你可在審查前修正判定。")
    st.stop()

# ── 步驟 1：記憶體分類預覽（免落地），逐檔讓使用者確認/修正 ──────────────────
st.subheader("① 自動分類（可修正）")
auto: list[FileClassification] = [classify_fileobj(io.BytesIO(u.getvalue()), u.name) for u in ups]

confirmed_labels: list[str] = []
for i, (u, c) in enumerate(zip(ups, auto)):
    col_name, col_kind, col_reason = st.columns([3, 2, 4])
    col_name.write(f"📄 {u.name}")
    default = _default_label(c)
    sel = col_kind.selectbox(
        "判定",
        _OPTIONS,
        index=_OPTIONS.index(default),
        key=f"kind_{i}",  # 以索引為鍵，避免同名檔衝突；rerun 間保留使用者選擇
        label_visibility="collapsed",
    )
    col_reason.caption(c.reason)
    confirmed_labels.append(sel)

if not st.button("② 開始審查", type="primary"):
    st.stop()

# ── 步驟 2：依確認結果落地 → 路由 → 審查 ────────────────────────────────
# 用後即刪：所有暫存檔在 TemporaryDirectory 結束時一併清除（最小足跡、地端不外送）。
report = None
with tempfile.TemporaryDirectory() as tmpdir:
    confirmed: list[FileClassification] = []
    for u, label in zip(ups, confirmed_labels):
        if label == _IGNORE:
            continue
        dest = Path(tmpdir) / u.name
        dest.write_bytes(u.getvalue())
        confirmed.append(FileClassification(
            path=str(dest), filename=u.name, kind=_LABEL_TO_KIND[label], reason="使用者確認/修正",
        ))

    if not confirmed:
        st.warning("所有檔案都被標記為「忽略此檔」，沒有可審查的內容。")
        st.stop()

    try:
        files, supporting, class_findings = route_classifications(confirmed)
        report = review_routed(files, supporting, class_findings)
    except Exception as exc:  # noqa: BLE001 - 介面層需把解析錯誤友善呈現
        st.error(f"解析或審查失敗：{exc}")
        st.stop()

# ── 步驟 3：報告（沿用既有摘要 / 明細 / 下載）─────────────────────────────
st.divider()
st.subheader("③ 審查報告")
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
