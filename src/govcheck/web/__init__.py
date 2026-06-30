"""高還原 Web 介面（FastAPI + 靜態前端）。

純介面層：只負責把既有 pipeline（分類 → 路由 → 審查 → Markdown）包成 HTTP API，
並提供靜態前端。**不放任何檢查邏輯**（規則/LLM 仍在 checks/，介面與判讀分離）。
與既有 Streamlit `app.py` 共用同一套後端；資料全程地端，API 不外送雲端。
"""
