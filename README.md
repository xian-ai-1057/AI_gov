# AI 治理審查小幫手（govcheck）

內部單機工具：一次上傳提案單位提交的**送件包**（**F01 系統資訊表 / F02 風險評鑑 /
F03 上線檢核表** + 佐證文件），自動分類 → 跑規則與 LLM 判讀 → 產出**結構化初判報告**
（缺件、填寫錯誤、是否符規、待補項）。

> 定位：AI 出「初判草稿」加速人工，**最終判定權在治理人員與三遵**。報告全程標示「AI 初判，需人工覆核」。
> 資料全程地端，不外送雲端（LLM 僅送內部/地端端點）。

---

## 一分鐘理解：這個工具在做什麼

治理人員每次收到一份送件包，要逐表逐欄人工核對「文件齊不齊、欄位填了沒、分級對不對、跨表一不一致」。
本工具把這些**確定性可機檢**的部分自動化，幾秒內產出一份標好「錯誤 / 提醒 / 紀錄」的報告，
讓人把時間花在**需要判斷的實質內容**上。

```
送件包（多檔）  ──①上傳──▶  自動分類  ──②確認/修正──▶  審查 pipeline  ──③──▶  結構化報告
  F01 .xlsx                 依工作表名稱判定          缺件→必填→規則            錯誤/提醒/紀錄
  F02 .xlsm                 F01/F02/F03/佐證          →跨表→F03佐證LLM          + 固有風險分級
  F03 .xlsx                 重複/無法辨識會標示                                 + 下載 Markdown
  佐證 PDF/Word
```

---

## 安裝與啟動

```bash
uv sync --extra dev                              # 安裝依賴（含開發/測試）
uv run python scripts/extract_f02_scoring.py     # 建置一次：抽取 F02 計分常數 → config（換官方範本才需重跑）
uv run python scripts/build_regulation_index.py  # Phase 3：建置 RAG 法規索引（Milvus Lite；首次需數分鐘）
uv run pytest                                    # 跑測試（全綠才算就緒）

# 啟動介面（擇一；兩者共用同一套後端 pipeline，皆地端、無外部請求）
uv run uvicorn govcheck.web.api:app --port 8501  # 高還原 Web 介面（FastAPI + 靜態前端）→ http://localhost:8501
uv run streamlit run app.py                      # Streamlit 介面
```

> 兩種介面差異只在外觀與互動，**判讀邏輯完全相同**。對外展示/實際使用建議用 Web 介面；
> 想快速本機驗證或調參數用 Streamlit。

---

## 使用流程（三步驟操作）

### ① 上傳送件包
把該案的所有檔案一次拖入（或點選）上傳區。**不需要自己分類、不需要先改檔名**。
接受的檔案：

| 文件 | 格式 | 說明 |
|---|---|---|
| F01 系統資訊表 | `.xlsx` | 附件一範本 |
| F02 風險評鑑 | `.xlsm` | 附件二範本（含計分公式的巨集活頁簿） |
| F03 上線檢核表 | `.xlsx` | 附件三範本 |
| 佐證文件 | `.pdf` / `.docx` 等 | 模型卡、測試報告等（非 Excel） |

### ② 確認自動分類
系統依**工作表名稱**自動判定每個檔案是 F01 / F02 / F03 / 佐證 / 無法辨識，並列出判定理由：

- **重複表單**（同類上傳兩份）、**無法辨識**的檔會標示提醒。
- 每個檔可手動**改判定**或設為「**忽略此檔**」排除。
- 無法辨識者預設為「忽略此檔」，逼使用者明確指定，不靜默誤路由。

確認無誤後按「**開始審查**」。

### ③ 審查報告
審查時 Web 介面會顯示**逐階段進度**（落地 → 解析各表 → 規則檢查 →（選用）F03 LLM）。
完成後出報告——詳見下節。

---

## 如何看審查報告

**頂端摘要**

- **通過 / 待處理**徽章：只要有任一「錯誤」即為待處理。
- **固有風險分級** tile（有 F02 才顯示）：**低=綠 / 中=琥珀 / 高=紅**，並附百分比，
  來源為 F02 第一頁【AI系統固有風險分級評估表】重算結果。
- **錯誤 / 提醒 / 紀錄**三個計數。

**分頁**（預設停在【紀錄】，順序：**紀錄 → 錯誤 → 提醒 → 全部**）

| 分頁 | 嚴重度 | 內容 |
|---|---|---|
| 🟢 紀錄 | INFO | 自動分類結果、F03 佐證 LLM 彙整表、通過紀錄——一眼看每項填了什麼 |
| 🔴 錯誤 | ERROR | 明確違規/缺漏，**必須處理**（缺件、單複選違規、分數不符、未續填等）|
| 🟡 提醒 | WARN | 可能有問題，**需人工確認**（如檔內無快取分級無法比對）|
| 全部 | — | 不過濾，全部列出 |

> 落地時預設顯示【紀錄】；若該案沒有任何紀錄項，會自動退回【全部】，避免畫面空白藏住錯誤。

**明細卡片**：點開可看「期望 / 實際」對照、說明文字、規則代碼；錯誤項預設展開。
每項皆標「需人工覆核」。最後可**下載 Markdown 報告**存檔/轉發。

---

## 重點：F02 風險分級與續填規則

F02 的固有風險分數/分級**不採信檔內填的值**，而是依答案**重算**（還原官方 Excel 公式：
四風險域加總 → 加成因子 → 百分比 → 取最大 → 分級），再與檔內快取比對（不符會報 `F02.GRADE_MISMATCH`）。
分級依重算結果驅動兩條續填規則：

1. **分級為「中」或「高」** → 必須填【AI系統剩餘風險評鑑表】，未填報 `F02.RESIDUAL_MISSING`。
2. **剩餘風險評鑑分數 ≥ 6**（= 控管強度 × 固有風險評估分數）→ 必須填【AI系統風險處理計畫表】，
   未填報 `F02.TREATMENT_MISSING`。

> 規則依據：附件四〈四、風險分數說明〉。低風險毋需續填剩餘風險評鑑表。

---

## 啟用 F03 LLM 佐證審查（預設關閉）

F03 的 **LLM 兩段佐證比較**——比對「提案規劃階段」與「上線階段」佐證差異、標示過於草率/不明確的說明，
彙整成一張總覽表（放在【紀錄】分頁）。於 Streamlit 側欄切換，或設環境變數後重啟：

```bash
export GOVCHECK_LLM_ENABLED=1
export GOVCHECK_LLM_BASE_URL="http://localhost:11434/v1"   # 預設值；地端/內部端點
export GOVCHECK_LLM_MODEL="gemma3:4b"                       # 依端點可用模型
```

本機可用 **ollama** 起地端模型測試；**端點不可用時自動略過，不影響規則檢查**。
資料僅送上述設定端點，不外送公有雲。

---

## 啟用 RAG 法規檢索（Phase 3，預設關閉）

F03 LLM 判讀時動態檢索相關 R01–R07 條文輔助判斷，F02 觸發題也會查表提示對應條文（`F02.REG_REF_NOTE`）。
**一體啟停**：實際生效需 `enable_llm` **且** `rag.enabled` 同時為真；缺 `rag:` 設定區段時全用硬碼預設，行為不變。

```bash
uv run python scripts/build_regulation_index.py --dry-run  # 建索引前先看切塊結果（不寫入）
uv run python scripts/build_regulation_index.py             # 建置一次：法規索引 + canonical 檢索映射
uv run python scripts/build_regulation_index.py --eval      # 檢索品質 recall@k 報告（校準 score_threshold 用）

export GOVCHECK_LLM_ENABLED=1        # RAG 需先啟用 LLM 判讀
export GOVCHECK_RAG_ENABLED=1
export GOVCHECK_RAG_EMBEDDING_BASE_URL="http://localhost:11434/v1"  # 預設值；embedding 端點
export GOVCHECK_RAG_EMBEDDING_MODEL="bge-m3"                        # 預設值
```

索引/映射為 build-time 產物（`data/rag/`、`data/milvus/`，已 gitignore），本機開發預設用 **Milvus Lite**（單機嵌入式）；
多程序部署或需要併發檢索時，遷移到 Milvus Server 見 [docs/milvus_migration.md](docs/milvus_migration.md)。
索引未建置或端點不可用時自動降級略過，不影響規則檢查與既有 LLM 判讀。

---

## 現況（Phase 1–3 完成）

端到端可跑：**批次上傳 → 自動分類 → 缺件 / 必填 / 規則 / 跨表一致 / F03 佐證 LLM + RAG 檢索 → 報告**。

- **批次上傳 + 自動分類**：依工作表名稱判定 F01/F02/F03/佐證/無法辨識，審查前可逐檔確認或修正。
- **缺件檢查**：核心表單缺漏，並依重算風險分級要求對應的條件佐證。
- **F01 系統資訊表**：必填欄位、資料列、附屬表內部一致性。
- **F02 風險評鑑**：重算固有風險分數並與檔內快取比對 → 規則檢查（單選唯一、複選非空、系列題完整、
  條件依賴、中/高風險續填）+ 義務條文提示（RAG 檢索）。
- **F03 上線檢核表**：規則層（佐證欄位完整性）＋ LLM 層（兩段佐證比較 + 法規符合性判讀，預設關、自動降級）。
- **跨表一致性**：F01 ↔ F02 ↔ F03 的系統負責人、填表單位等欄位比對。
- **RAG（Phase 3）**：Build-time 法規索引（R01–R07 PDF 分塊 + embedding）+ canonical 檢索映射，F03 判讀時動態檢索相關條文。
- **結構化報告**：錯誤 / 提醒 / 紀錄分級 + 固有風險分級，Web / Streamlit 皆可瀏覽並下載 Markdown。

---

## 日誌與稽核（地端）

執行期產生**兩條分流日誌**，皆寫入 `logs/`（已 gitignore，**不入庫**）：

- **技術維運**（`logs/govcheck.log`，並同時輸出 console）：管線各階段、解析與 LLM 降級、
  錯誤堆疊，供除錯與維運。
- **治理稽核**（`logs/audit.log`，每次審查一行 JSON）：`subject`（系統名）、`filing_unit`
  （送件單位）、`error`/`warn` 計數、`passed`、`duration_ms`、`operator`、`enable_llm` 等，
  供合規 / 三遵事後查核。

兩條以 `request_id` 串連同一次審查。**維運日誌的細節程度可調**：以 `GOVCHECK_LOG_PROFILE`
一個友善開關切換——

| `GOVCHECK_LOG_PROFILE` | 維運日誌（govcheck.log） | 適用 |
| --- | --- | --- |
| `dev` | 全流程（每階段、每批、每解析步驟 + 下列重點） | 開發 / 除錯 |
| `prod`（預設） | 重點（審查起訖、端點命中、降級、錯誤堆疊） | 正式使用 |
| `quiet` | 只剩降級與錯誤 | 高量 / 極簡 |

> **稽核日誌固定完整落檔，不受 profile 影響**——調低維運細節不會讓合規軌跡消失。

```bash
export GOVCHECK_LOG_PROFILE=dev      # 細節程度（預設 prod）
export GOVCHECK_LOG_LEVEL=DEBUG      # 直接指定 level，優先於 profile（選用、細粒度逃生門）
export GOVCHECK_LOG_DIR=/var/log/govcheck   # 落地目錄（預設 repo 根的 logs/）
export GOVCHECK_OPERATOR=alice       # 稽核「操作者」欄（目前無登入機制，best-effort）
# 其餘可調：GOVCHECK_LOG_CONSOLE / GOVCHECK_LOG_MAX_BYTES / GOVCHECK_LOG_BACKUP_COUNT
```

**隱私**：日誌只記識別資訊與數量（系統名／送件單位／Finding 代碼／計數／耗時／例外型別），
**絕不**記原始檔內容、F03 佐證全文、LLM prompt／回應全文、api_key。

## 架構

`parsers → models → checks(rule/llm) → review.engine → report`，四段可插拔。
規則檢查（確定性、先跑、可單測、不需網路）與 LLM 判讀嚴格分離；
新增 Phase = 新增 Check 類別 + 在 engine 註冊，不動既有程式。設計守則見專案根 `CLAUDE.md`。

```
src/govcheck/
├── parsers/      讀官方 .xlsx/.xlsm → 乾淨資料模型
├── models/       F01/F02/F03 表單、Finding、ReviewReport 等契約
├── scoring/      還原 F02 Excel 計分公式 → 分數/分級
├── classify/     依工作表名稱自動分類送件包
├── rag/          Build-time RAG：pdf_text/chunker/refs/embedding/store/mapping
├── checks/
│   ├── rule/     確定性規則（缺件/F01/F02/跨表/F03佐證存在性/F02觸發題條文查表）
│   └── llm/      F03 兩段佐證 LLM 比較 + RAG 檢索輔助判讀
├── review/       engine：編排 parse → checks → 匯總
├── report/       Finding → Markdown
└── web/          FastAPI（/api/classify、/api/review、/api/review/stream）+ 靜態前端掛載

scripts/build_regulation_index.py   # Phase 3：R01–R07 PDF 分塊 + embedding → 索引 + canonical 映射
specs/                              # SDD spec：p3-01 法規索引 / p3-02 canonical 映射 / p3-03 RAG 判讀
docs/milvus_migration.md            # Milvus Lite → Milvus Server 遷移手冊
```

---

## 路線圖

- Phase 1 ✅ F02 風險評鑑規則
- Phase 2 ✅ 缺件 + F01 必填 + F01/F02/F03 跨表一致 + 批次上傳自動分類
- Phase 3 ✅ F03 兩段佐證 LLM 比較 + 彙整表 + RAG 法規檢索（build-time 索引 + canonical 映射，預設關）
- Phase 4 ⏳ 佐證充分性（解析佐證文件內容並評估是否支持檢核項）
- Phase 5 ⏳ Agent 協助提案者填寫
