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

報告頁的設計目標：**一眼看出哪些檢核項需要人工確認**，而不是在幾十筆重複的細節裡逐條翻找。

**頂端摘要（Hero）**

- **通過 / 待處理**徽章（顯示文字為「規則通過」／「待處理」）：只要有任一「錯誤」即為待處理。
- **結論條**：一句話帶出重點，例如「0 錯誤 · 15/20 檢核項需人工確認 · 主因：上線階段佐證不足」——
  「主因」由前端依各檢核項的問題欄位確定性統計出來，非 LLM 生成。
- 四個 tile：**固有風險分級**（有 F02 才顯示；低=綠／中=琥珀／高=紅，來源為 F02 第一頁
  【AI系統固有風險分級評估表】重算結果）、**錯誤**、**待確認項**（= 有問題的檢核項列數）、**無異常項**。

**其他待處理**（非項次型的錯誤/提醒，如缺件、F01 必填缺漏、F02 分數不符；只在存在時才顯示）

**① 檢核項狀態總覽表**（核心視覺）

把 F03 每個檢核項次的 LLM/RAG 彙整表**依項次合併**成一列，欄位為
`項次 | 管理議題 | 提案佐證 | 上線佐證 | 兩段差異 | 法規符合 | 摘要`：

- 每欄狀態以顏色/符號標示（✓充分／▲有問題／？無法判定／✕判讀失敗／—無資料），有問題的列整列淺色標示。
- 頂部切換【只看有問題】（預設）／【全部】——「全部」會連同已通過的項次一起列出。
- 點列展開可看該項次**完整細節**：期望/實際對照、LLM/RAG 判讀原文、法規條文引用、規則代碼。
- 沒有跑 LLM/RAG 時（規則式模式），狀態欄改由對應規則 finding（如佐證缺漏）推導，不會開天窗。

**② 彙整參考**（預設收合，展開後為分頁）

| 分頁 | 內容 |
|---|---|
| 原始彙整 | F03 LLM/RAG 彙整表原文 + 摘要（含完整判讀說明） |
| 法規對應 | F02 觸發題對應法規條文（`F02.REG_REF_NOTE`）+ 查表摘要 |
| 通過紀錄 | 其餘 INFO（自動分類結果等）——一眼看每項填了什麼 |
| 全部 | 改版前的**平鋪列表**（紀錄/錯誤/提醒/全部 四個嚴重度篩選），做為 fallback 保留 |

> 每一筆 finding 無論在哪個分頁都看得到——狀態總覽表是「摘要視圖」，彙整參考的【全部】分頁
> 是完整、不遺漏的原始清單，兩者資料同源。

**明細**：期望/實際對照、說明文字、規則代碼皆標「需人工覆核」。最後可**下載 Markdown 報告**存檔/轉發。

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

## 啟用 F03 LLM 佐證審查（預設開啟）

F03 的 **LLM 兩段佐證比較**——比對「提案規劃階段」與「上線階段」佐證差異、標示過於草率/不明確的說明，
彙整成一張總覽表（餵進【① 檢核項狀態總覽】的狀態欄，原始表格收在【② 彙整參考】）。
`src/govcheck/config/llm_config.yaml` 已將預設值改為開啟，**不放 `.env` 也會跑 LLM**（只要本機 ollama 或設定的端點可連上）。

`.env` 會在啟動時自動載入（`app.py`／`web/api.py` 皆已呼叫 `load_dotenv()`），可用它覆寫端點/模型或關閉：

```bash
# .env（不存在則新建；此檔已 gitignore，機密只放這裡）
GOVCHECK_LLM_BASE_URL=http://localhost:11434/v1   # 內部/正式端點請覆寫
GOVCHECK_LLM_MODEL=gemma3:4b                       # 依端點可用模型調整
GOVCHECK_LLM_ENABLED=false                         # 想暫時關閉才需要這行
```

> 注意：專案自帶的 [`.env.example`](.env.example) 為求「複製即安全」，範例值刻意寫
> `GOVCHECK_LLM_ENABLED=false` / `GOVCHECK_RAG_ENABLED=false`——**若直接 `cp .env.example .env` 不改動，
> 會蓋掉 YAML 的預設開啟、變成關閉**。要維持預設開啟，複製後把這兩行刪掉或改成 `true`，或乾脆不建立 `.env`。

本機可用 **ollama** 起地端模型測試；**端點不可用時自動略過，不影響規則檢查**。
資料僅送上述設定端點，不外送公有雲。

---

## 啟用 RAG 法規檢索（Phase 3，預設開啟）

F03 LLM 判讀時動態檢索相關 R01–R07 條文輔助判斷，F02 觸發題也會查表提示對應條文（`F02.REG_REF_NOTE`）。
**一體啟停**：實際生效需 `enable_llm` **且** `rag.enabled` 同時為真；缺 `rag:` 設定區段時全用硬碼預設，行為不變。

```bash
uv run python scripts/build_regulation_index.py --dry-run  # 建索引前先看切塊結果（不寫入）
uv run python scripts/build_regulation_index.py             # 建置一次：法規索引 + canonical 檢索映射
uv run python scripts/build_regulation_index.py --eval      # 檢索品質 recall@k 報告（校準 score_threshold 用）
```

同樣已在 `llm_config.yaml` 預設開啟（`rag.enabled: true`），不放 `.env` 也會生效。
`.env` 可用來覆寫 embedding 端點/模型，或關閉（提醒：見上節 `.env.example` 的「複製即安全」注意事項）：

```bash
GOVCHECK_RAG_EMBEDDING_BASE_URL=http://localhost:11434/v1  # 預設值；embedding 端點
GOVCHECK_RAG_EMBEDDING_MODEL=bge-m3                         # 預設值
GOVCHECK_RAG_ENABLED=false                                  # 想暫時關閉才需要這行
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
- **F03 上線檢核表**：規則層（佐證欄位完整性）＋ LLM 層（兩段佐證比較 + 法規符合性判讀，預設開、端點不可用時自動降級）。
- **跨表一致性**：F01 ↔ F02 ↔ F03 的系統負責人、填表單位等欄位比對。
- **RAG（Phase 3）**：Build-time 法規索引（R01–R07 PDF 分塊 + embedding）+ canonical 檢索映射，F03 判讀時動態檢索相關條文。
- **結構化報告**：Web 介面以**檢核項狀態總覽表**呈現（依項次合併 LLM/RAG 判讀，問題列一眼可辨），
  固有風險分級 + 錯誤/提醒/紀錄計數，彙整參考另收合展示，皆可下載 Markdown。

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

> 完整架構圖（含請求生命週期、build-time RAG 索引管線、報告頁狀態機）見
> [docs/architecture.md](docs/architecture.md)。

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
- Phase 3 ✅ F03 兩段佐證 LLM 比較 + 彙整表 + RAG 法規檢索（build-time 索引 + canonical 映射，預設開）
- Phase 4 ⏳ 佐證充分性（解析佐證文件內容並評估是否支持檢核項）
- Phase 5 ⏳ Agent 協助提案者填寫
