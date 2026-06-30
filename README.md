# AI 治理審查小幫手（govcheck）

內部單機工具：上傳提案單位填寫的 **F01 系統資訊表 / F02 風險評鑑 / F03 上線檢核表**（+佐證），
自動產出**結構化初判報告**（缺件、填寫錯誤、是否符規、待補項）。

> 定位：AI 出「初判草稿」加速人工，**最終判定權在治理人員與三遵**。報告全程標示「AI 初判，需人工覆核」。
> 資料全程地端，不外送雲端。

## 現況（Phase 1 / MVP）

**F02 風險評鑑規則檢查**：解析官方 `.xlsm` → 重算固有風險分數並與檔內快取比對 → 跑規則檢查
（單選唯一、複選非空、系列題完整、條件依賴、中/高風險續填）→ 產出報告。

## 快速開始

```bash
uv sync --extra dev
# 建置第一步：抽取 F02 計分常數（產出 src/govcheck/config/f02_scoring.yaml）
uv run python scripts/extract_f02_scoring.py
# 測試
uv run pytest
# 啟動介面（擇一；兩者共用同一套後端 pipeline，皆地端不外送）
uv run uvicorn govcheck.web.api:app --port 8501   # 高還原 Web 介面（FastAPI + 靜態前端）
uv run streamlit run app.py                        # Streamlit 介面
```

> Web 介面（`web/` + `src/govcheck/web/api.py`）由 Claude Design 設計稿移植，三步驟：
> 上傳送件包 → 確認自動分類 → 審查報告；字型用系統 CJK 堆疊，頁面無任何外部請求。

## 架構

`parsers → models → checks(rule/llm) → review.engine → report`，四段可插拔。詳見 `.claude/plans` 設計文件。

擴充路線：Phase 2 缺件+F01必填+跨表一致 → Phase 3 F03 兩段式(LLM+RAG) → Phase 4 佐證充分性 → Phase 5 Agent 協填。
