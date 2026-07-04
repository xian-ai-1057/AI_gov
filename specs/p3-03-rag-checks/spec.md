# Spec p3-03 — rag-checks（F03 LLM 符合性判讀、F02 規則查表、rag config、engine 整合）

> SDD spec，對應已批准計畫 `hi-wondrous-crown.md`（Phase 3）。作者：spec-author S2。
> 定位不變：AI 初判、人工覆核，資料全程地端。本 spec 為 **Phase 3a 交付物**；實作於 Phase 3b（主要 T3 判讀 + T4 整合）。
> 上游合約來自 p3-02（`RetrievalMap` / `RegulationRef` / …）與 p3-01（`EmbeddingClient` / `RegulationStore`，僅 build 用）。

---

## 1. Context

審查時（runtime），系統以 p3-02 預算好的 `retrieval_map.json`（查表、零 embedding、零 Milvus）對送件包做兩件事：
1. **F03 = LLM 符合性判讀**：每檢核項取 canonical 條文摘錄 + 兩段佐證 → LLM 判 `covered / gap / undetermined`，
   結構鏡射既有 `checks/llm/f03_evidence.py`（batch 迴圈、連錯 3 中止、彙整表、永不 raise）。
2. **F02 = 純規則查表，無 LLM**（審查修訂 M3）：觸發風險題（`answers[qid] == score_on`）→ 查映射 → 逐字引用條號+標題的 INFO。

engine 以單一插入點 `_run_rag_checks(sub, progress)` 串起兩者，並以「mapping 缺 → `RAG.SKIPPED`、絕不 raise」為外殼。
實際啟用 = `enable_llm AND rag.enabled`（RAG 功能一體啟停）。

---

## 2. Scope

### In scope
1. `rag/config.py` `load_rag_config()`：讀 `llm_config.yaml` `rag:` 區段 + `GOVCHECK_RAG_*` env 覆寫；**缺區段時完整預設值**。
2. `checks/llm/f03_rag.py` `run_all(checklist, retrieval_map, client, cfg, progress) -> list[Finding]`：
   F03 符合性判讀（批次、prompt 預算防護、降級、彙整表、TEMPLATE_REF_MODIFIED 比對）。
3. `checks/rule/f02_reg_refs.py` `run_all(f02_form, retrieval_map, scoring_cfg) -> list[Finding]`：F02 觸發題規則查表。
4. `review/engine.py` `_run_rag_checks(sub, progress)` 整合（插入點在 `if has_f03_checklist:` 區塊**之外**）。
5. Finding 代碼與引用顯示格式（§4.5）。
6. 合約型別：`RagConfig` / `F03RagVerdict` / `F03RagBatchResponse`（`contracts/schemas.py`）。

### Out of scope（見 §8）
- refs 解析 / canonical 映射預算 / mapping loader / f03_parser L 欄 → p3-02。
- PDF/chunker/EmbeddingClient/RegulationStore 實作 → p3-01。
- **runtime embedding / runtime Milvus**（**第 1 次重複提醒**）：本 spec 只吃 `RetrievalMap`（查表）。
- **F02 上 LLM**（**第 1 次重複提醒**）：F02 一律純規則查表。
- **F01**（**第 1 次重複提醒**）：Phase 4+。

---

## 3. 核心設計決策

| 決策 | 結論與理由 |
|---|---|
| **f03_rag 結構鏡射 f03_evidence.py** | 沿用久經測試的 batch 迴圈、`_MAX_CONSECUTIVE_ERRORS=3`、`parse_json_object` 復用、彙整表+摘要、**永不 raise**。差別只在：判讀對象改「canonical 摘錄 + 兩段佐證」、verdict 白名單容錯、獨立 timeout。 |
| **verdict 白名單容錯** | LLM 回 `covered/gap/undetermined` 以外值 → 一律降 `undetermined`（不因單項壞值丟整批）。解析走 `parse_json_object` + 逐項寬鬆取值，**不以 Pydantic Literal 嚴格擋**（`F03RagVerdict.verdict` 的 Literal 僅記錄合法集合）。 |
| **RAG 判讀獨立 timeout=120s** | 不沿用 chat 60s（審查修訂 H2）：canonical 摘錄使 prompt 變長、prefill 久，避免連環超時誤觸「連錯 3 中止」。 |
| **prompt 預算防護** | 組批前以 **CJK 字數 ≈ token** 估算 prompt 長度，超 `max_items`/上下文預算 → 自動降 batch / 砍摘錄（每段 ≤ `max_excerpt_chars`、每項 ≤ `max_sections_per_item` 段），並 **log 計數（不 log 內容）**。 |
| **canonical 為判讀基準** | 摘錄一律取自 `RetrievalMap` 的 canonical sections（build 自官方模板）。上傳檔 L 欄/描述與 canonical 不符 → `F03.TEMPLATE_REF_MODIFIED`（WARN），**判讀仍以 canonical 為準**（審查修訂 H3）。 |
| **F02 純規則查表** | 觸發題只有 Y/N、無佐證文字 → 無判讀任務；逐字引用條號+標題比 LLM 轉述更可追溯（M3）。放 `checks/rule/`，`source="rule"`。 |
| **F02 也掛 RAG 開關（取捨記錄）** | F02 查表雖是規則、無 LLM，仍與 F03 判讀共用「RAG 功能一體啟停」開關（`enable_llm AND rag.enabled`），使使用者對「RAG 引用功能」有單一心智開關。**取捨**：犧牲「F02 規則本可恆跑」的一致性，換取啟停語意單純與進度/降級的統一外殼。 |
| **engine 插入點在 f03 區塊外** | 審查修訂 M5：現行 104–106 行在 `if has_f03_checklist:` 內，照字面插會漏掉 **F02-only 送件包**（無 F03 但有 F02 仍應跑 F02 查表）。故 `_run_rag_checks` 插在該區塊之外、`cross_consistency` 之前，內部各自判 `sub.f03_checklist` / `sub.f02` 是否存在。 |
| **絕不 raise** | `_run_rag_checks` 外層 try/except → 任何未預期例外收斂為單筆 `RAG.SKIPPED`（INFO）。mapping 缺檔/版本不符同樣 → `RAG.SKIPPED`。 |

---

## 4. 詳細規格

### 4.1 `rag/config.py` `load_rag_config()`

- 讀 `llm_config.yaml` 的 `rag:` 區段（若無此區段 → 全用預設值，**不依賴 yaml 已更新**）+ `GOVCHECK_RAG_*` env 覆寫。
- 回傳形狀 = `RagConfig`（見 `contracts/schemas.py`），鍵與預設：
  - runtime：`enabled=false` / `mapping_path=data/rag/retrieval_map.json` / `batch_size=2` /
    `max_sections_per_item=3` / `max_excerpt_chars=300` / `timeout=120` / `max_items=30`。
  - build-time：`embedding_base_url` / `embedding_model` / `embedding_dim=1024` / `milvus_uri` /
    `top_k=4` / `score_threshold`（由 p3-02 build ④ `run_eval()`（CLI `--eval`）校準，未校準前 `None`）。
- **機密只走 env**（如 embedding endpoint 的 api_key），不入 `RagConfig`、不入 yaml。
- env 覆寫解析比照既有 `llm/config.py`（空字串/非數字不塌縮成 0）。

### 4.2 `checks/llm/f03_rag.py`

**API**
```python
def run_all(
    checklist: F03Checklist,
    retrieval_map: RetrievalMap,
    client: ChatClient | None,
    cfg: RagConfig,
    progress: Callable[[dict], None] | None = None,
) -> list[Finding]: ...
```

**測試場景（fixtures 據此）**：一批含檢核項 `1-1`、`1-2`，皆有兩段佐證、`retrieval_map.f03_items` 有其
canonical sections、上傳檔 L 欄與 canonical 相符（除 TEMPLATE_REF_MODIFIED 專案）。`batch_size` 使兩項併一批。

**流程（鏡射 f03_evidence）**
1. 目標項 = 有兩段佐證且在 `retrieval_map.f03_items` 有對應者；無則回 `[]`（不產摘要噪音）。`max_items` 截斷（記 note）。
2. **TEMPLATE_REF_MODIFIED 比對**（判讀前，逐項）：上傳 `item.regulation_ref_raw` / `item.description` 與 map 的
   `canonical_ref_raw` / `canonical_description` 正規化後不符 → 加 `F03.TEMPLATE_REF_MODIFIED`（WARN）。判讀摘錄仍用 canonical。
3. **prompt 預算防護**：組批前估 CJK 字數；超限 → 降 batch / 砍摘錄（每段 ≤ `max_excerpt_chars`、每項 ≤ `max_sections_per_item` 段）
   並 `log.info` 計數（不 log 內容）。
4. 每批：組 `{item_id, description(canonical), sections(canonical 摘錄), proposal, golive}` → `client.chat`（`timeout=cfg.timeout`）
   → `parse_json_object` → 逐項取 `verdict`（白名單容錯，非法→`undetermined`）。
5. verdict 映射：
   - `gap` → `F03.RAG_GAP`（WARN，location=item.loc，message 含 canonical 條號引用；`source="llm"`）。
   - `undetermined` → `F03.RAG_UNDETERMINED`（INFO）。
   - `covered` → 無個別 Finding（僅入彙整表 + 摘要計數）。
   - 模型漏回此 item_id → `F03.RAG_ITEM_ERROR`（INFO，location=item.loc）。
6. 降級：單批 `LLMError`/解析失敗 → `F03.RAG_ITEM_ERROR`（INFO）並續審；連續 3 批失敗 → `F03.RAG_ERROR`（WARN）中止。
7. 一律產 `F03.RAG_TABLE`（INFO，彙整表）+ `F03.RAG_SUMMARY`（INFO，摘要）。
8. **進度**：每批 emit `stage="llm"` 事件（併入 engine 的 llm 總批數，見 §4.4）。

**摘要格式**（供 fixtures `summary_contains` 對齊）：
`RAG 符合性判讀：審閱 {reviewed} 項，缺口 {gap} 項、無法判定 {undet} 項，待人工覆核。` +（可選 note：
`{n} 項判讀失敗` / `未送審 {n} 項` / `端點異常已中止`，以「（…；…）」附加）。`reviewed` = 取得有效 verdict 的項數。

罐裝案例：`contracts/fixtures/canned_llm_responses/*` → `contracts/fixtures/expected_findings/*.yaml`（逐筆代碼/嚴重度/location 序列）。

### 4.3 `checks/rule/f02_reg_refs.py`

**API**
```python
def run_all(f02_form: F02Form, retrieval_map: RetrievalMap, scoring_cfg: dict) -> list[Finding]: ...
```

- **`score_on` 語意（已核實）**：`scoring/f02_score.py` L59 `if ans is not None and ans == spec["score_on"]` —
  題目答案等於 `score_on` 時該題「觸發」（貢獻風險分數）。`scoring_cfg = f02_scoring.yaml`，`questions[qid].score_on`；
  `qid` 為葉層鍵（`UC-01`/`UC-04-01`…），與 `F02Form.answers` 鍵一致。
- 觸發判準：`f02_form.answers.get(qid) == scoring_cfg["questions"][qid]["score_on"]`（未填/不等 → 不觸發）。
- 對每觸發題：若 `retrieval_map.f02_questions[qid]` 有 sections → 產 `F02.REG_REF_NOTE`（INFO，`source="rule"`），
  message **逐字引用**條號+標題（不轉述），引用顯示格式見 §4.5。
- 收尾一筆 `F02.REG_REF_SUMMARY`（INFO，`source="rule"`，觸發題數/引用條數計數）。
- **純規則、零 LLM**（**第 1 次重複提醒**）。

### 4.4 `review/engine.py` 整合

新增 `_run_rag_checks(sub, progress) -> list[Finding]`，於 `review_submission` 中呼叫，**插入點在
`if has_f03_checklist:` 區塊之外、`cross_consistency` 之前**（審查修訂 M5，避免漏 F02-only 送件包）：

```
if has_f03_checklist:
    findings += _rule_step(f03_evidence_presence.run_all(...))
    if enable_llm:
        findings += _run_f03_llm(sub.f03_checklist, progress)
findings += _run_rag_checks(sub, enable_llm, progress)   # ← F02-only 也會跑
findings += _rule_step(cross_consistency.run_all(sub, review_cfg))
```

`_run_rag_checks` 行為：
- 實際啟用 = `enable_llm AND cfg.enabled`（`load_rag_config()`）；未啟用 → 回 `[]`（零事件、零 Finding）。
- 外層 `try/except`：`load_retrieval_map` 缺檔/版本不符或任何未預期例外 → 單筆 `RAG.SKIPPED`（INFO），**絕不 raise**。
- 內部各自判存在：`sub.f03_checklist`（且 `sheet_present`）在 → 跑 `f03_rag`（需建 `ChatClient`）；`sub.f02` 在 → 跑 `f02_reg_refs`。
- **進度**：RAG 的 llm 批次 emit **併入 `stage="llm"` 的 done/total 總批數**（與 `_run_f03_llm` 的批數合計），
  避免前端進度條凍結（審查修訂 M6）；不改 web 層。
- log 只記代碼/計數/耗時/例外型別。

### 4.5 Finding 代碼與引用顯示格式（鎖定）

| code | severity | source | 觸發 |
|---|---|---|---|
| `F03.RAG_GAP` | WARN | llm | verdict=gap |
| `F03.TEMPLATE_REF_MODIFIED` | WARN | llm | 上傳 L 欄/描述 ≠ canonical |
| `F03.RAG_UNDETERMINED` | INFO | llm | verdict=undetermined（含非法值降級） |
| `F03.RAG_TABLE` | INFO | llm | 彙整表（恆出，若有目標項） |
| `F03.RAG_SUMMARY` | INFO | llm | 摘要（恆出，若有目標項） |
| `F03.RAG_ITEM_ERROR` | INFO | llm | 單批失敗 / 模型漏回單項 |
| `F03.RAG_ERROR` | WARN | llm | 連續 3 批失敗中止 |
| `F02.REG_REF_NOTE` | INFO | rule | 觸發題有對應條文 |
| `F02.REG_REF_SUMMARY` | INFO | rule | F02 查表收尾 |
| `RAG.SKIPPED` | INFO | rule | mapping 缺/版本不符/未預期例外 |

所有 Finding `needs_human=True`（Finding 預設即是）。

**引用顯示格式**（section_path 正準形式 → 顯示字串）：
- 章節式 `五/(二)/2` → `R03 五、(二) 2`（首段中文序號後接 `、`，括號層直接接續，阿拉伯數字前空格）。
- 條文式 `第一條` → `R01 第一條`。
- 一般規則：`f"{reg_code} " + 各段以顯示規則接合`；由 f03_rag / f02_reg_refs 共用一個 helper。

---

## 5. 特別處理

- **隱私（硬守線）**：log 只記代碼/計數/耗時/例外型別；**絕不記** canonical 摘錄、佐證原文、prompt/回應全文、L 欄原文、
  api_key/Authorization。批失敗只記例外型別（`LLMError` 訊息可能夾帶端點回應 → 不可記本文，比照 f03_evidence L108–110）。
- **timeout**：`f03_rag` 建 `ChatClient` 時以 `cfg.timeout`（120s）覆寫，不用 chat 預設 60s。
- **絕不 raise**：任何路徑（缺 mapping、client 初始化失敗、批次連錯）都收斂為 Finding，規則檢查與介面不中斷。
- **回歸硬條件**：`rag.enabled=false`（預設）→ `_run_rag_checks` 回 `[]`、零事件 → engine 輸出與 **golden snapshot**
  （3b 由 T4 於動工前先產出並 commit，輸入 synthetic）diff 為零。**此 AC 依賴「快照先行」的順序**（見 §6 AC-11）。

---

## 6. Acceptance Criteria（可 pytest 驗證，引用 `contracts/fixtures/`）

- **AC-1**（canned→expected 逐筆）：對 `canned_llm_responses/{gap,covered,undetermined,missing_item,illegal_verdict}.json`
  各以 `FakeClient`（單批一輸出，範式同 `tests/test_f03_evidence_llm.py`）跑 `f03_rag.run_all`，產出 Finding 的
  `(code, severity, source)` 序列與對應 `expected_findings/*.yaml` 的 `findings` 完全一致；有 `location_contains` 者子字串成立。
- **AC-2**（摘要計數）：各案 `F03.RAG_SUMMARY.message` 含 `summary_contains` 全部字串。
- **AC-3**（非 JSON 容錯）：`not_json.txt` → 出 `F03.RAG_ITEM_ERROR` + 表 + 摘要，且 `absent_codes`（`F03.RAG_ERROR`）不出現。
- **AC-4**（verdict 白名單容錯）：`illegal_verdict.json` 的 `maybe` → `F03.RAG_UNDETERMINED`（非拋例外、非丟整批）。
- **AC-5**（連錯 3 中止）：`FakeClient` 連續 3 批 `LLMError` → 出 `F03.RAG_ERROR`（WARN）、`client.calls == 3`、後續批不再呼叫。
- **AC-6**（單批失敗續審）：3 批中第 1 批 `LLMError`、其餘正常 → 出 `F03.RAG_ITEM_ERROR`、不出 `F03.RAG_ERROR`、續審完。
- **AC-7**（prompt 預算降批）：構造超長 canonical 摘錄使估算超限 → 實際批數 > `ceil(n/batch_size)`（自動降批）或摘錄被截至
  `max_excerpt_chars`；且降批/截斷只 `log.info` 計數，caplog **不含**摘錄/佐證內容。
- **AC-8**（TEMPLATE_REF_MODIFIED 正例）：上傳 item `regulation_ref_raw` 與 map `canonical_ref_raw` 不符 → 出
  `F03.TEMPLATE_REF_MODIFIED`（WARN），且該項判讀摘錄仍取自 canonical（可斷言 prompt 組裝用 canonical 值）。
- **AC-9**（TEMPLATE_REF_MODIFIED 反例）：上傳與 canonical 相符 → **不**出 `F03.TEMPLATE_REF_MODIFIED`。
- **AC-10**（F02 查表正反例）：構造 `F02Form`，某題 `answers[qid]==score_on`（觸發）且 map 有對應 → 出 `F02.REG_REF_NOTE`
  （INFO/source=rule，message 含引用條號+標題）；另一題 `answers[qid]!=score_on`（不觸發）→ 該題不產 note。收尾出 `F02.REG_REF_SUMMARY`。
- **AC-11**（回歸：預設關 = 零差異）：`rag.enabled=false`（預設）時 `_run_rag_checks(sub, ...)` 回 `[]`、不 emit 進度；
  engine 對 synthetic 送件包輸出與事先 commit 的 golden snapshot 完全一致。**順序要求**：golden snapshot 由 T4 於動工前先產出並 commit。
- **AC-12**（F02-only 送件包）：送件包**只有 F02、無 F03**，`enable_llm=True` 且 `rag.enabled=True`、mapping 存在 →
  仍跑 `f02_reg_refs`（出 `F02.REG_REF_*`），不因無 F03 而略過（驗證插入點在 f03 區塊外）。
- **AC-13**（mapping 缺失僅多 RAG.SKIPPED）：`enable_llm=True`、`rag.enabled=True` 但 mapping 檔缺 →
  engine 不 raise，findings 僅較「關閉」多一筆 `RAG.SKIPPED`（INFO），其餘規則檢查結果不變。
- **AC-14**（config 缺區段全預設）：`llm_config.yaml` **無 `rag:` 區段** 時 `load_rag_config()` 回全預設 `RagConfig`
  （`enabled=False`、`batch_size=2`、`timeout=120`…）；`RagConfig.model_validate(rag_config_example.json)` 成功。
- **AC-15**（env 覆寫）：設 `GOVCHECK_RAG_ENABLED=1` / `GOVCHECK_RAG_BATCH_SIZE=4` → 對應鍵被覆寫；空字串/非數字不塌縮成 0。
- **AC-16**（引用顯示格式）：helper 對 `("R03","五/(二)/2")` → `"R03 五、(二) 2"`；`("R01","第一條")` → `"R01 第一條"`。
- **AC-17**（隱私 log 斷言）：跑含佐證/canonical 摘錄的判讀後，讀 `govcheck.log`：**不含**任何佐證原文、canonical 摘錄、
  prompt/回應全文（比照 `test_f03_evidence_llm.py::test_batch_failure_does_not_leak_llm_body_into_log`），只含代碼/計數/例外型別。
- **AC-18**（絕不 raise）：對 `f03_rag.run_all` 與 `_run_rag_checks` 注入各式壞輸入（client=None、mapping schema 壞、
  chat 拋非 LLMError 例外）→ 皆回 Finding 清單、無例外逸出。
- **AC-19**（contract 驗證）：`F03RagBatchResponse.model_validate(json.load(gap.json))` 成功；注入未知鍵 → `ValidationError`（`extra="forbid"`）。

> AC 總數：**19**。

---

## 7. 與其他 spec 的介面（型別名逐字引用，Lead 鎖定）

| 型別 / 符號 | 定義處 | 本 spec 用途 |
|---|---|---|
| `RetrievalMap(schema_version, built_at, embedding_model, f03_items, f02_questions)` | p3-02 | runtime 輸入（經 `load_retrieval_map`）；f03_rag/f02_reg_refs 的 canonical 來源 |
| `F03ItemRetrieval(item_id, canonical_topic, canonical_description, canonical_ref_raw, refs, sections)` | p3-02 | F03 判讀摘錄 + TEMPLATE_REF_MODIFIED 比對 |
| `F02QuestionRetrieval(qid, sections)` | p3-02 | F02 查表引用來源 |
| `RetrievedSection(reg_code, section_path, title, excerpt, score, origin)` | p3-02 | 摘錄與引用顯示（section_path→顯示字串） |
| `RegulationRef(reg_code, section_path_prefix)` | p3-02 | F02/F03 引用比對（如需） |
| `load_retrieval_map(...)` / `RetrievalMapError` | p3-02 `rag/mapping.py` | engine `_run_rag_checks` 載入 + 攔截降級 |
| `ChatClient` / `LLMError` / `parse_json_object` | 既有 `llm/client.py` | f03_rag 判讀呼叫與寬鬆 JSON 解析；`timeout=cfg.timeout` 覆寫 |
| `F03Checklist` / `F03ChecklistItem`（含新 `regulation_ref_raw`） | 既有 `models/f03.py`（p3-02 擴充欄） | f03_rag 輸入 |
| `F02Form`（`answers`） | 既有 `models/f02.py` | f02_reg_refs 觸發判準 |
| `Finding` / `Severity` | 既有 `models/finding.py` | 全部產出 |
| `f02_scoring.yaml` `questions[qid].score_on` | 既有 `config/f02_scoring.yaml` | F02 觸發判準（本 spec 只讀） |
| `RagConfig` / `F03RagVerdict` / `F03RagBatchResponse` | **本 spec** `contracts/schemas.py` | config 形狀 + LLM 回應解析 |
| engine `review_submission` 插入點（104–106 行區塊外） | 既有 `review/engine.py`（T4 改） | `_run_rag_checks` 呼叫位置 |

---

## 8. Out of scope（再次強調）

- **F01 不做**（**第 2 次重複提醒**）：本 spec 與整個 Phase 3 不處理 F01，屬 Phase 4+。
- **runtime embedding / runtime Milvus 不做**（**第 2 次重複提醒**）：只吃 `RetrievalMap`（p3-02 的 loader 讀 JSON）。
- **F02 上 LLM 不做**（**第 2 次重複提醒**）：`f02_reg_refs` 純規則查表、零 LLM；有佐證可判的 LLM 化留 Phase 4，映射已就位。
- canonical 映射預算 / refs 解析 / mapping loader 實作 → p3-02。
- 語料索引 build ① / EmbeddingClient / RegulationStore → p3-01。
- web/前端進度條渲染不改（僅併入 total 計算）。

---

## 9. 參考

- 計畫：`hi-wondrous-crown.md`（M3 F02 純規則、M5 插入點、H2 prompt 預算+獨立 timeout、H3 canonical 基準、M4 golden snapshot、M6 進度）。
- 既有程式：`src/govcheck/checks/llm/f03_evidence.py`（鏡射母本）、`src/govcheck/llm/client.py`（ChatClient/LLMError/parse_json_object）、
  `src/govcheck/llm/config.py`（env 覆寫範式）、`src/govcheck/review/engine.py`（插入點、`_run_f03_llm` 降級範式）、
  `src/govcheck/scoring/f02_score.py`（`score_on` 語意 L59）、`tests/test_f03_evidence_llm.py`（FakeClient 範式、log 隱私斷言）。
- 上游合約：p3-02 `contracts/schemas.py`（`RetrievalMap` 家族）、p3-01 `contracts/schemas.py`（`EmbeddingClient`/`RegulationStore`，僅 build）。
- 本 spec 合約：`contracts/schemas.py`；fixtures：`contracts/fixtures/{canned_llm_responses/*, expected_findings/*.yaml, rag_config_example.json}`。
