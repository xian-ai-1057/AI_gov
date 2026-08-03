# AI 治理審查小幫手（govcheck）

內部單機工具：上傳提案單位提交的**送件包**（**F01 系統資訊表 / F02 風險評鑑 /
F03 上線檢核表** + 佐證文件），跑規則檢查與語意判讀 → 產出**結構化初判報告**
（缺件、填寫錯誤、風險分級是否算錯、敘述是否充分、是否符合法規、待補項）。

> 定位：AI 出「初判草稿」加速人工，**最終判定權在治理人員與三遵**。報告全程標示「AI 初判，需人工覆核」。
> 資料全程地端，不外送雲端（LLM 僅送內部/地端端點）。

---

## 五項核心能力

| # | 能力 | 怎麼做 | 型態 |
|---|---|---|---|
| 1 | 檢查文件是否填錯 | 缺件、F01 必填、F02 單複選/系列題完整、F03 勾了卻沒填佐證、跨表欄位不一致 | 規則（無網路） |
| 2 | 檢查風險評級是否算錯 | 依答案還原官方 Excel 公式重算，與檔內快取比對 | 規則 |
| 3 | 檢查敘述內容是否滿足需求 | F03 每個檢核項判內容完整性／合理性，並比對佐證文件是否支持 | LLM |
| 4 | 檢查專案內容是否合規 | 同一次判讀比對法規條文摘錄；F02 觸發題另列對應條號 | LLM + 規則 |
| 5 | 產出綜合報告 | 檢核項狀態總覽表 + 其他待處理 + 可下載 Markdown | — |

需求 3 與 4 刻意在**同一次模型呼叫**判完：兩者看的是同一段文字，拆成兩套會讓每項被判讀兩次、
等待時間加倍，結論還可能互相矛盾。

```
送件包  ──①上傳──▶  審查 pipeline  ──②──▶  結構化報告
 F01 .xlsx            缺件→F01必填→F02規則           錯誤/提醒/紀錄
 F02 .xlsm            →F03佐證缺漏→F03語意判讀        + 固有風險分級
 F03 .xlsx            →F02法規查表→跨表一致           + 檢核項狀態總覽表
佐證 PDF/Word           安全預檢→ts_parser→來源定位     + 下載 Markdown
```

---

## 安裝與啟動

```bash
uv sync --extra dev                              # 安裝依賴（含開發/測試）
uv run python scripts/extract_f02_scoring.py     # 建置一次：抽取 F02 計分常數 → config（換官方範本才需重跑）
uv run pytest                                    # 跑測試（全綠才算就緒）
uv run uvicorn govcheck.web.api:app --port 8501  # 啟動 → http://localhost:8501
```

---

## 本機資產（不入庫，新環境必須另外佈署）

有兩份檔案含**法規原文或其衍生判準**，依專案守則不進 Git。缺了它們程式仍可跑，
但需求 3、4 會降級為「未判讀」——這不是壞掉，是刻意的 fail-safe，報告會明說。

| 資產 | 路徑 | 缺了會怎樣 |
|---|---|---|
| 法規對照表 | `data/rag/retrieval_map.json` | 報告出現 `REGULATION.MAP_UNAVAILABLE`；不引用任何條文、不判法規符合性 |
| 判讀 Prompt | `prompts/*.yaml`（+ `prompts/lenient/`） | 報告出現 `F03.PROMPT_UNAVAILABLE`；F03 語意判讀整段標為未完成 |

法規對照表是**凍結產物**：由官方 R01–R07 PDF 與 F03 模板離線建置一次，runtime 只讀不寫。
建置管線（PDF 切塊 → embedding → Milvus → 產出對照表）已封存於 `_Archive/rag_build/`，
法規改版時才取回重跑。

---

## 使用流程

### ① 上傳送件包

依欄位放入檔案：**F01 / F02 / F03 各一份，佐證可多份**。未上傳的表單會由缺件檢查列出，
不影響其餘檢查。

| 文件 | 格式 | 說明 |
|---|---|---|
| F01 系統資訊表 | `.xlsx` | 附件一範本 |
| F02 風險評鑑 | `.xlsm` | 附件二範本（含計分公式的巨集活頁簿） |
| F03 上線檢核表 | `.xlsx` | 附件三範本 |
| 佐證文件 | `.pdf` / `.docx` 等 | 模型卡、測試報告等 |

選好使用角色（提案者／審查者）與流程階段（提案／驗收／正式營運）後按「**開始審查**」。
提案階段不會因尚未填寫上線驗收欄位而誤報。

### ② 審查報告

規則檢查隨即完成；啟用語意判讀時，依模型與檢核項數可能需要數分鐘。

---

## 如何看審查報告

報告頁的設計目標：**一眼看出哪些檢核項需要人工確認**，而不是在幾十筆重複的細節裡逐條翻找。

**頂端摘要（Hero）**

- **通過 / 待處理**徽章依 `decision_status` 顯示；解析或語意判讀未完成時不會被誤判為合規。
- **結論條**：一句話帶出重點，例如「0 錯誤 · 15/20 檢核項需人工確認 · 主因：法規符合性缺口」——
  「主因」由前端依各檢核項的問題欄位確定性統計出來，非 LLM 生成。
- 四個 tile：**固有風險分級**（有 F02 才顯示；低=綠／中=琥珀／高=紅）、**錯誤**、
  **待確認項**（= 有問題的檢核項列數）、**無異常項**。

**其他待處理**（非項次型的錯誤/提醒，如缺件、F01 必填缺漏、F02 分數不符；只在存在時才顯示）

**① 檢核項狀態總覽表**（核心視覺）

把 F03 的 `item_assessments` 與檢核項名冊**依項次**合成一列，欄位為
`項次 | 管理議題 | 提案內容 | 上線內容 | 佐證支持 | 法規符合 | 摘要`：

- 每欄狀態以顏色/符號標示（✓充分／▲有問題／？無法判定／—無資料），有問題的列整列淺色標示。
- 頂部切換【只看有問題】（預設）／【全部】——「全部」會連同已通過的項次一起列出。
- 點列展開可看該項次**完整細節**、法規條號與可驗證的文件定位引用。
- 名冊獨立於語意判讀：標示「不適用」或佐證全空的項次仍會列出，不會整列從報告消失。

**② 彙整參考**（預設收合，展開後為分頁）

| 分頁 | 內容 |
|---|---|
| 判讀摘要 | F03 判讀統計（審了幾項、幾項有缺口） |
| 法規對應 | F02 觸發題對應法規條文（`F02.REG_REF_NOTE`）+ 查表摘要 |
| 通過紀錄 | 其餘 INFO——一眼看每項填了什麼 |
| 全部 | 平鋪列表（紀錄/錯誤/提醒/全部 四個嚴重度篩選），完整不遺漏的原始清單 |

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

## 啟用 F03 語意判讀（預設關閉）

F03 判讀依固定的 `item_id → review_profile → prompt_id` 路由，把每一項的內容完整性、合理性、
佐證支持度、佐證衝突與**法規符合性**合併在同一次判讀。項目會依 context 預算分批，只有低信心
或來源互相衝突才執行 escalation。
正式 Prompt 不存放於 Git；所有模板都經 `PromptGateway` 從受控來源取得。

```bash
# .env（不存在則新建；此檔已 gitignore，機密只放這裡）
GOVCHECK_LLM_ENABLED=true
GOVCHECK_LLM_BASE_URL=http://localhost:11434/v1
GOVCHECK_LLM_MODEL=gemma3:4b
GOVCHECK_LLM_CONTEXT_WINDOW_TOKENS=30000  # 必須與實際部署的 context window 一致
GOVCHECK_LLM_MAX_OUTPUT_TOKENS=4096
GOVCHECK_PROMPT_PROVIDER=file          # file = 讀本機 prompts/；litellm = 走 Prompt Management 端點
GOVCHECK_PROMPT_DIR=prompts
GOVCHECK_PROMPT_LABEL=production       # production 讀 prompts/；其他 label 讀 prompts/<label>/
```

每次 review 開始會把 label 解析成固定版本；審查途中改 prompt 檔不影響該次結果。Prompt 無法取得且
沒有相同固定版本快取時，評估標示為未完成並轉人工覆核，不會回退到硬編碼或過期 Prompt。
可用 `GET /api/readiness` 驗證七個 Prompt 的就緒狀態。

佐證文件只走 `TsParserAdapter`；設定見 `src/govcheck/config/parser_config.yaml`。OCR/VLM 預設關閉，
且 `MODEL_HOST` 必須在核准清單內，否則回傳 `parser_unavailable`。
佐證檢索預設為 BM25-like 關鍵字排序；設 `GOVCHECK_EVIDENCE_EMBEDDING_ENABLED=true` 可加上
embedding 分數融合（端點見 `GOVCHECK_EMBEDDING_BASE_URL` / `GOVCHECK_EMBEDDING_MODEL`）。
Chat 判讀會依 `GOVCHECK_LLM_CONTEXT_WINDOW_TOKENS` 的完整預算自動分批；embedding 輸入也會依
設定的單筆長度、batch size 與每次 review 的 chunk 總上限切分。BM25 會串流掃過全部
原文視窗，但每題只保留最高分的小型候選池，避免超大文件在呼叫模型前就用盡記憶體。若
embedding 的 chunk 上限或端點故障使語意檢索未涵蓋全文，空結果會標成「檢索未完整」而非
`not_found`，避免把取樣限制誤報為沒有佐證。

---

## 日誌與稽核（地端）

執行期產生**兩條分流日誌**，皆寫入 `logs/`（已 gitignore，**不入庫**）：

- **技術維運**（`logs/govcheck.log`，並同時輸出 console）：管線各階段、解析與 LLM 降級、
  錯誤堆疊，供除錯與維運。
- **治理稽核**（`logs/audit.log`，每次審查一行 JSON）：只記表單類型、計數、`decision_status`、
  parser／模型呼叫數、耗時與操作者等非內容欄位。

兩條以 `request_id` 串連同一次審查。**維運日誌的細節程度可調**：

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

**隱私**：所有 profile 都**不記**系統名、送件單位、真實檔名、原始文件、儲存格、Prompt 變數、
模型回應、API key 或 Authorization header。

---

## 架構

`parsers → models → checks(rule/llm) → review.engine → report`，四段可插拔。
規則檢查（確定性、先跑、可單測、不需網路）與 LLM 判讀嚴格分離；
新增檢查 = 新增 Check + 在 engine 註冊，不動既有程式。設計守則見專案根 `CLAUDE.md`。

```
src/govcheck/
├── parsers/      讀官方 .xlsx/.xlsm 與佐證文件 → 乾淨資料模型
├── models/       F01/F02/F03 表單、Finding、ReviewReport 等契約
├── scoring/      還原 F02 Excel 計分公式 → 分數/分級           ← 需求 2
├── evidence/     來源定位 chunk 的混合檢索
├── prompts/      PromptCatalog / Provider / Gateway（無正式 Prompt 文字）
├── regulations/  凍結法規對照表的 loader 與條文引用顯示
├── checks/
│   ├── rule/     缺件 / F01 / F02 / 跨表 / F03 佐證缺漏 / F02 觸發題條文查表   ← 需求 1、4
│   └── llm/      f03_assessment：內容 + 佐證 + 法規符合性一次判完              ← 需求 3、4
├── review/       engine：編排 parse → checks → 匯總
├── report/       ReviewReport → Markdown                                      ← 需求 5
└── web/          FastAPI（/api/review、/api/readiness）+ 靜態前端

src/ts_parser/                    # 上游 parser 原始碼整合邊界
web/                              # 單一前端（index.html / app.js / styles.css）
_Archive/rag_build/               # 封存：法規索引建置管線（法規改版時取回重跑）
```

API 只有兩個端點：`POST /api/review`（上傳送件包 → 報告 JSON + Markdown）與
`GET /api/readiness`（Prompt / 模型就緒查詢）。

---

## 路線圖

- ✅ F02 風險評鑑規則與計分還原
- ✅ 缺件 + F01 必填 + F01/F02/F03 跨表一致
- ✅ F03 語意判讀（內容 / 佐證 / 法規符合性合一）+ 佐證文件解析與來源定位
- ✅ 檢核項狀態總覽報告 + Markdown 匯出
- ⏳ 上線校準：去識別案例雙人標註、shadow mode 品質／成本門檻驗收
