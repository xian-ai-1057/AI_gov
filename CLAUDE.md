# AI 治理審查小幫手（govcheck）— Project Guide

> 本檔在專案 root，會自動載入 Claude 的 context。請遵守以下守則。

---

## 專案總覽

台新銀行內部 AI 治理審查工具。治理人員上傳提案單位提交的
**F01 系統資訊表 / F02 風險評鑑 / F03 上線檢核表**（+佐證文件），
系統依 **R01–R07 治理辦法**與 F03 檢核表自動做「初步審查」，產出**結構化審查報告**，指出：
(1) 缺少文件/欄位、(2) 是否符合規範、(3) 哪些部分需要補足。

**定位**：AI 產出「初判草稿」加速人工審閱，**最終判定權在治理人員與三遵**。
報告全程標示「AI 初判，需人工覆核」。資料全程地端，**不外送雲端**。

**現況：Phase 1–3 已完成** — 批次上傳自動分類 → 缺件 + F01 必填 + F02 規則 + F03 兩段佐證 LLM 比較（+彙整表）
+ 跨表一致性，端到端可跑；介面含高還原 Web（FastAPI + 靜態前端）與 Streamlit。
（Phase 3 的 RAG 向量檢索尚未實作；F03 LLM 目前為純兩段佐證比較。）

---

## 技術棧

Python 3.12、Streamlit、Pydantic v2、pandas（讀表）+ openpyxl（公式/儲存格層級）、
PyYAML、pdfplumber/python-docx（佐證，後續）、pytest。**uv** 管理依賴。
後續 Phase：sentence-transformers(bge-m3) + Milvus(pymilvus) + OpenAI SDK（指內部/地端端點）。

> 本機開發可用 ollama（gemma + bge-m3，`localhost:11434`）做真實端到端測試，免內部端點。

---

## 目錄結構

```
AI治理/
├── app.py                          # Streamlit 入口（上傳 F02 → 報告）
├── pyproject.toml / uv.lock        # uv 管理
├── scripts/extract_f02_scoring.py  # 建置第一步：解析官方 .xlsm 公式 → 計分 config
├── src/govcheck/
│   ├── config/f02_scoring.yaml     # 自動抽出的 F02 計分常數（勿手改，重跑腳本產生）
│   ├── logging_setup.py            # 地端 log：ops(govcheck.log)+稽核(audit.log) 雙管線；profile 控細節程度
│   ├── models/                     # F02Form / Finding / ReviewReport (Pydantic v2)
│   ├── parsers/                    # f02_parser.py（pandas 讀續填表 + openpyxl 讀公式/快取）
│   ├── scoring/f02_score.py        # 還原 Excel 公式：加總→加成→百分比→MAX→分級
│   ├── checks/
│   │   ├── base.py                 # Check 介面：__call__(form) -> list[Finding]
│   │   ├── rule/                   # ✅ 規則式檢查：f02_rules / f01_rules / missing_docs / cross_consistency / f03_evidence_presence
│   │   └── llm/f03_evidence.py     # ✅ F03 兩段佐證 LLM 比較 + 彙整表（預設關、自動降級；測試一律 mock）
│   ├── review/engine.py            # 編排：parse → 跑 checks → 匯總
│   ├── report/builder.py           # Finding → Markdown
│   └── web/api.py                  # FastAPI：/api/classify + /api/review + 靜態掛載（介面層，零判讀邏輯）
├── web/                            # 高還原靜態前端（index.html/styles.css/app.js；移植自 Claude Design 設計稿）
├── tests/                          # 計分還原 + 規則正反例（真檔生成 fixture，皆有 ground truth）
└── data/original/                  # 唯讀來源（PDF/xlsx 模板）；gitignore，禁寫入、禁入庫
```

`data/original/` 內容：R01–R07 七份治理辦法 PDF、附件一 F01(xlsx)、附件二 F02(xlsm)、
附件三 F03(xlsx)、附件四/五(pdf)。**worktree 開發時以 symlink 連到主 repo，已 gitignore。**

---

## 常用指令

```bash
uv sync --extra dev                            # 安裝依賴
uv run python scripts/extract_f02_scoring.py   # （重）產出 F02 計分 config
uv run pytest                                  # 跑測試（須全綠才收工）
uv run ruff check src tests scripts app.py     # lint
uv run uvicorn govcheck.web.api:app --port 8501  # 啟動高還原 Web 介面（FastAPI + 靜態前端）
uv run streamlit run app.py                    # 啟動 Streamlit 介面（兩者共用同一後端）
```

---

## 架構與擴充原則

- **四段管線**：`parsers → models → checks → review.engine → report`，每段可插拔。
- **規則 / LLM 嚴格分離**：`checks/rule/`（確定性、先跑、可單測、不需網路）與 `checks/llm/`（判讀、後續）分開。
- **新增 Phase = 新增 Check 類別 + 在 engine 註冊**，不動既有程式。
- **計分常數來自 config**，由 `scripts/extract_f02_scoring.py` 從官方檔公式自動抽出，不在程式寫死。

**路線圖**：Phase 1 ✅F02 規則 → Phase 2 缺件+F01必填+跨表一致 → Phase 3 F03 兩段式(LLM+RAG) →
Phase 4 佐證充分性 → Phase 5 Agent 協助提案者填寫。

---

## 行為守則

### 必須做
- ✅ 規則式檢查與 LLM 判讀**分離**；LLM 相關測試一律 **mock**，不打真端點。
- ✅ 測試要有 **ground truth**（正例/反例皆有正確答案），須全綠才收工。
- ✅ 動 `data/original/` 前確認：**唯讀來源，禁止寫入或更動**；測試 fixture 一律寫到 `tests/` 或暫存。
- ✅ Logging 經 `logging_setup`（`get_logger` 記 ops、`audit()` 記稽核）；細節程度由
  `GOVCHECK_LOG_PROFILE`（dev=全流程 / prod=重點 / quiet=只剩錯誤）控制。各插入點標明 level
  （DEBUG=流程 / INFO=重點 / WARNING=降級 / exception=堆疊）。

### 不能做
- ❌ 寫入 `data/original/`（唯讀來源）。
- ❌ 把提交者資料、原始模板或規範內容 push 上 GitHub 或送往**外部雲端**（僅地端/內部端點）。
- ❌ 跳過測試直接收工。
- ❌ Log 記錄**原始檔內容/解析後儲存格值/F03 佐證全文/LLM prompt 回應全文/api_key/Authorization**；
  只准記識別資訊與數量（系統名/單位/Finding 代碼/計數/耗時/例外型別）。

### Git / 推送守則
- 推 GitHub 前確認 `data/original/`、`.env`、`uploads/`、`output/`、`logs/`、`_Archive/` 都在 `.gitignore`
  且不在 staged 清單（原始資料與含識別資訊的稽核 log 絕不入庫）。remote：`https://github.com/xian-ai-1057/AI_gov.git`。
