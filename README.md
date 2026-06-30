# AI 治理審查小幫手（govcheck）

內部單機工具：一次上傳提案單位提交的**送件包**（**F01 系統資訊表 / F02 風險評鑑 /
F03 上線檢核表** + 佐證文件），自動分類 → 跑規則與 LLM 判讀 → 產出**結構化初判報告**
（缺件、填寫錯誤、是否符規、待補項）。

> 定位：AI 出「初判草稿」加速人工，**最終判定權在治理人員與三遵**。報告全程標示「AI 初判，需人工覆核」。
> 資料全程地端，不外送雲端（LLM 僅送內部/地端端點）。

## 現況（Phase 1–3 完成）

端到端可跑：**批次上傳 → 自動分類 → 缺件 / 必填 / 規則 / 跨表一致 / F03 佐證 LLM → 報告**。

- **批次上傳 + 自動分類**：一次拖入所有檔案，依工作表名稱判定 F01/F02/F03/佐證/無法辨識，
  使用者可在審查前逐檔確認或修正；重複表單、無法辨識檔會標示提醒。
- **缺件檢查**：核心表單缺漏、並依重算風險分級要求對應的條件佐證。
- **F01 系統資訊表**：必填欄位、資料列、附屬表內部一致性。
- **F02 風險評鑑**：解析官方 `.xlsm` → 重算固有風險分數並與檔內快取比對 → 規則檢查
  （單選唯一、複選非空、系列題完整、條件依賴、中/高風險續填）。
- **F03 上線檢核表**：
  - 規則層：佐證欄位完整性（依生命週期階段）。
  - LLM 層（Phase 3）：**兩段佐證比較**——比對「提案規劃階段」與「上線階段」佐證差異、
    標示過於草率/不明確的說明，並彙整成一張總覽表。預設關閉；端點不可連線時自動降級，不影響規則檢查。
- **跨表一致性**：F01 ↔ F02 ↔ F03 的系統負責人、填表單位等欄位比對。
- **結構化報告**：錯誤 / 提醒 / 紀錄分級，可在 Web 或 Streamlit 介面瀏覽，並下載 Markdown 報告。

> Phase 3 的 **RAG（向量檢索 R01–R07 治理辦法輔助判讀）尚未實作**，目前 F03 LLM 為純兩段佐證比較。

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

### 啟用 F03 LLM 佐證審查（預設關）

於 Streamlit 側欄切換，或設環境變數：

```bash
export GOVCHECK_LLM_ENABLED=1
export GOVCHECK_LLM_BASE_URL="http://localhost:11434/v1"   # 預設值；地端/內部端點
export GOVCHECK_LLM_MODEL="gemma3:4b"                       # 依端點可用模型
```

本機可用 **ollama** 起地端模型測試；端點不可用時自動略過，不影響規則檢查。資料僅送上述設定端點，不外送公有雲。

## 架構

`parsers → models → checks(rule/llm) → review.engine → report`，四段可插拔。
規則檢查（確定性、先跑、可單測、不需網路）與 LLM 判讀嚴格分離；
新增 Phase = 新增 Check 類別 + 在 engine 註冊，不動既有程式。詳見 `.claude/plans` 設計文件。

## 路線圖

- Phase 1 ✅ F02 風險評鑑規則
- Phase 2 ✅ 缺件 + F01 必填 + F01/F02/F03 跨表一致 + 批次上傳自動分類
- Phase 3 ✅ F03 兩段佐證 LLM 比較 + 彙整表（RAG 向量檢索規劃中）
- Phase 4 ⏳ 佐證充分性（解析佐證文件內容並評估是否支持檢核項）
- Phase 5 ⏳ Agent 協助提案者填寫
