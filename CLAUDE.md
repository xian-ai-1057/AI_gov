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

**收斂到五項核心能力**（其餘輔助功能已刻意移除，不要再長回來）：
1. 文件是否填錯（規則）2. 風險評級是否算錯（規則）3. 敘述是否滿足需求（LLM）
4. 專案內容是否合規（LLM + 規則）5. 產出綜合報告。


需求 3 與 4 在**同一次模型呼叫**判完（`checks/llm/f03_assessment.py`）：兩者看同一段文字，
拆開會讓每項判讀兩次、等待加倍、結論互相矛盾。介面只有一套（FastAPI + `web/` 靜態前端）。

---

## 技術棧

Python 3.12、FastAPI + 靜態前端（vanilla JS）、Pydantic v2、pandas（讀表）+ openpyxl（公式/儲存格層級）、
PyYAML、pytest。**uv** 管理依賴。**佐證文件解析不在本專案內**：`parsers/supporting.py` 只做安全預檢，
再以 HTTP 送到同機的 file_process 服務（`/Users/xian/Documents/00_CODE/Taishin/file_process`，port 3322）；
本專案**不保留任何解析程式快照**，解析 bug 一律修在 file_process。
embedding/LLM 統一走 requests + OpenAI 相容端點（/embeddings、/chat/completions；
內部/地端端點，本機 dev 用 ollama）。

> 本機開發可用 ollama（gemma + bge-m3，`localhost:11434`）做真實端到端測試，免內部端點。

---

## 目錄結構

```
govcheck/
├── pyproject.toml / uv.lock        # uv 管理
├── scripts/extract_f02_scoring.py  # 建置第一步：解析官方 .xlsm 公式 → 計分 config
├── scripts/export_governance_records.py  # 稽核 log + 覆核決定 → output/governance_records.json（給能力目錄）
├── src/govcheck/
│   ├── config/                     # f02_scoring.yaml（勿手改，重跑腳本產生）/ llm / log / review / parser
│   ├── logging_setup.py            # 地端 log：ops(govcheck.log)+稽核(audit.log) 雙管線；profile 控細節程度
│   ├── models/                     # F01/F02/F03 Form、Finding、ReviewReport、evidence (Pydantic v2)
│   ├── parsers/                    # f01/f02/f03 主表 + supporting（佐證文件：預檢 → HTTP → file_process）
│   ├── scoring/f02_score.py        # 還原 Excel 公式：加總→加成→百分比→MAX→分級        ← 需求 2
│   ├── regulations/                # 凍結法規對照表：mapping / models / config（runtime 只讀 JSON）
│   ├── evidence/retrieval.py       # 佐證片段混合檢索（BM25-like + 選用 embedding）
│   ├── prompts/                    # PromptCatalog / Gateway / Provider（正式 prompt 文字不入庫）
│   ├── checks/
│   │   ├── base.py                 # Check 介面：__call__(form) -> list[Finding]
│   │   ├── rule/                   # missing_docs / f01_rules / f02_rules / cross_consistency
│   │   │                           #   / f03_evidence_presence / f02_reg_refs          ← 需求 1、4
│   │   └── llm/f03_assessment.py   # 內容 + 佐證 + 法規符合性一次判完                    ← 需求 3、4
│   ├── review/engine.py            # 編排：parse → 跑 checks → 匯總
│   ├── report/builder.py           # ReviewReport → Markdown                            ← 需求 5
│   ├── report/governance_export.py # 只讀 audit.log／decisions → 交接契約（識別資訊與計數，無內文）
│   └── web/api.py                  # FastAPI：/api/review(+stream)、/decisions、/api/classify、/api/readiness
├── web/                            # 唯一前端（index.html/styles.css/app.js）
├── docs/                           # architecture / f03_review_v2 / prompts / evidence_retrieval
├── tests/                          # 計分還原 + 規則正反例（真檔生成 fixture，皆有 ground truth）
├── data/original/                  # 唯讀來源（PDF/xlsx 模板）；gitignore，禁寫入、禁入庫
├── data/rag/retrieval_map.json     # 凍結法規對照表（gitignore；缺檔→ REGULATION.MAP_UNAVAILABLE）
├── data/rag/form_fields.json       # 表單欄位目錄（gitignore；缺檔→ ASSIST.FIELDS_UNAVAILABLE）
├── prompts/                        # 判讀 prompt YAML（gitignore；缺檔→ F03.PROMPT_UNAVAILABLE）
└── _Archive/rag_build/             # 封存的法規索引建置管線（法規改版時取回重跑）
```

`data/original/` 內容：R01–R07 七份治理辦法 PDF、附件一 F01(xlsx)、附件二 F02(xlsm)、
附件三 F03(xlsx)、附件四/五(pdf)。**worktree 開發時以 symlink 連到主 repo，已 gitignore。**

---

## 常用指令

```bash
uv sync --extra dev                            # 安裝依賴
uv run python scripts/extract_f02_scoring.py   # （重）產出 F02 計分 config
uv run pytest                                  # 跑測試（須全綠才收工）
uv run ruff check src tests scripts           # lint
uv run python tests/golden/generate_snapshot.py  # 規則側 golden 基準（改動規則輸出才需重產）
uv run uvicorn govcheck.web.api:app --port 8501  # 啟動 Web 介面 → http://localhost:8501
```

---

## 架構與擴充原則

- **四段管線**：`parsers → models → checks → review.engine → report`，每段可插拔。
- **規則 / LLM 嚴格分離**：`checks/rule/`（確定性、先跑、可單測、不需網路）與 `checks/llm/`（判讀、後續）分開。
- **新增 Phase = 新增 Check 類別 + 在 engine 註冊**，不動既有程式。
- **計分常數來自 config**，由 `scripts/extract_f02_scoring.py` 從官方檔公式自動抽出，不在程式寫死。
- **條文引用決定性推導**：報告顯示的條號一律由對照表 canonical `section_path` 推導，
  模型回的 `gap_refs` 只作交集過濾——不讓模型憑空造出不存在的條號。
- **保持精簡**：新功能先問「是不是那五項核心能力之一」。不是就別加；是的話優先擴充既有 Check，
  而非另開一條平行管線。

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
  - **沒有例外**：`dump_llm_raw()` 已停用，任何 profile 都回 `None`、不落檔
    （`logging_setup.py`）。早期 `dev` profile 會把 LLM request/response 全文寫進 `logs/llm_raw/`，
    該行為已移除，**不要再加回來**。解析 JSON 失敗請靠例外型別與截斷版訊息除錯。
- ❌ 把 `data/rag/retrieval_map.json`（法規原文摘錄）或 `prompts/*.yaml`（判準衍生自治理辦法）入庫。
- ❌ 在本專案內重新實作或快照佐證解析器（pptx/msg/zip…）；一律呼叫 file_process 服務，主機必須在核准清單內。
  兩者皆已 gitignore；新環境靠另外佈署，缺檔時程式自動降級並在報告明示。

### Git / 推送守則
- 推 GitHub 前確認 `data/original/`、`.env`、`uploads/`、`output/`、`logs/`、`_Archive/` 都在 `.gitignore`
  且不在 staged 清單（原始資料與含識別資訊的稽核 log 絕不入庫）。remote：`https://github.com/xian-ai-1057/AI_gov.git`。
