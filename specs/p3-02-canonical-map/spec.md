# Spec p3-02 — canonical-map（規範參考解析、canonical 檢索映射預計算、runtime loader、F03 L 欄擴充）

> SDD spec，對應已批准計畫 `hi-wondrous-crown.md`（Phase 3）。作者：spec-author S2。
> 定位不變：AI 初判、人工覆核，資料全程地端。本 spec 為 **Phase 3a 交付物**（規格＋合約＋synthetic fixtures），
> 實作於 Phase 3b（T2 = build 側；T3 = mapping loader；T4 = f03_parser / models.f03 / review_config.yaml）。

---

## 1. Context

Phase 3 的 RAG 法規比對採「**build 時預計算檢索映射**」（審查修訂 M1）：檢索 query 全來自
**官方模板靜態文字**（F03 檢核項的 topic/description + L 欄「規範參考」；F02 題文），刻意不含提交者佐證，
故每次審查結果恆定 → build 時對每項預算 top-k，存 `data/rag/retrieval_map.json`；審查時**查表、零 embedding、零 Milvus**。

本 spec 負責「規範參考 → 正準引用 → canonical 檢索映射」這條 build 管線，以及 runtime 唯一入口 loader、
F03 parser 讀 L 欄。上游語料索引（PDF→chunk→embed→Milvus）由 **p3-01** 提供；下游判讀/查表/engine 整合由 **p3-03** 完成。

**檢索基準 = 官方模板，非上傳檔**（審查修訂 H3）：L 欄「規範參考」在上傳檔中可被提案單位改/刪（對抗性輸入）。
build 一律從 `data/original/附件三` 抽 canonical；上傳檔 L 欄僅供 p3-03 比對是否被竄改，判讀以 canonical 為準。

---

## 2. Scope

### In scope
1. `rag/refs.py`：L 欄「規範參考」字串 → `list[RegulationRef]`（tolerant 文法 + 正規化）；section_path prefix 雙向比對。
2. build ② `build_retrieval_map()`（在 `scripts/build_regulation_index.py` 內，函式邊界與 p3-01 的 ① 分離）：
   從官方附件三抽 20 檢核項的 canonical（item_id/topic/description/L 欄）、從官方附件二抽 F02 題文（**僅 build 用**）
   → 每項 curated+semantic 檢索 → 合併去重 → 輸出 `data/rag/retrieval_map.json`（原子寫）。
3. build ④ `run_eval()`（CLI `--eval`）：以 20 項的 curated 對（21 筆 ref）為 ground truth，
   報告 semantic recall@k + 分數分佈（threshold 依此校準）。
4. `rag/mapping.py`：`RetrievalMap` loader（runtime 唯一入口），載入 + schema 驗證；缺檔/版本不符 → 明確例外。
5. `parsers/f03_parser.py` 擴充：讀 L 欄 → `F03ChecklistItem.regulation_ref_raw`（座標入 `review_config.yaml`）。
6. `models/f03.py`：`F03ChecklistItem` 新增 `regulation_ref_raw: str | None = None`。
7. 合約型別（`contracts/schemas.py`）：`RegulationRef` / `RetrievedSection` / `F03ItemRetrieval` /
   `F02QuestionRetrieval` / `RetrievalMap`。

### Out of scope（見 §8）
- runtime embedding / runtime Milvus 呼叫（**第 1 次重複提醒**：runtime 只查表）。
- 上游語料索引 build ①（PDF/chunker/embedding/store）→ p3-01。
- F03 判讀、F02 查表、engine 整合、rag config、Finding 產生 → p3-03。
- F01 任何處理 → Phase 4+。

---

## 3. 核心設計決策

| 決策 | 結論與理由 |
|---|---|
| **refs 解析為 tolerant 文法** | L 欄為人工填寫，格式浮動（全形/分隔符/殘缺）。以「盡量救回、無法救回就丟棄並計數」取代嚴格解析，避免一個壞字整項失聯。無法解析殘段只認得 reg code → 退整部規範（`section_path_prefix=""`）。 |
| **正規化統一入口** | 全形→半形、去空白、`（二）`→`(二)`；比對與輸出前一律先正規化，避免同義不同碼。 |
| **prefix 雙向比對** | curated ref 的粒度與 chunk 粒度不必然一致（ref 可能比 chunk 粗或細）→ 雙向 prefix 命中；粗 ref/整部規範展開命中多 chunk 時 **每 ref 上限 3 chunk**（依 `order`），避免灌爆一項。 |
| **canonical 來自官方模板** | 對抗性輸入防護（H3）。build 讀官方附件三；產物含法規摘錄 → 必落 gitignore 區（見隱私）。 |
| **curated 優先、semantic 補充去重** | 每 F03 項：curated（L 欄解析→`RegulationStore.lookup` 粗篩→refs 細篩）為主 + semantic（embed topic:description，top_k=4）補充；合併時 **同 `(reg_code, section_path)` 或互為 prefix 者剔除 semantic**，`cap=max_sections_per_item`。F02 題只有 semantic。 |
| **threshold 由 `--eval` 校準，不拍腦袋** | build ④ 以 21 筆 curated ground truth 算 recall@k + 分數分佈；spec 的 AC **禁止寫死未經校準的門檻值**。 |
| **mapping loader 為 runtime 唯一入口** | 載入 + schema 版本驗證；缺檔/版本不符 → 明確例外（p3-03 engine 攔截降級為 `RAG.SKIPPED`）。runtime 零 embedding、零 Milvus（**第 1 次重複提醒**）。 |
| **parser 不懂 RAG** | f03_parser 只把 L 欄原始字串存進 `regulation_ref_raw`，不解析、不比對。解析發生在 refs.py（build 與 p3-03 判讀時）。 |

---

## 4. 詳細規格

### 4.1 `rag/refs.py`

**API（3b 對接）**
```python
def parse_regulation_refs(raw: str | None) -> list[RegulationRef]: ...
def ref_matches_chunk(ref: RegulationRef, chunk_reg_code: str, chunk_section_path: str) -> bool: ...
def normalize_section_path(raw: str) -> str: ...   # 全形→半形、去空白、（二）→(二)
```

**解析文法**
- `entry := reg_codes ("/" path_token)*`
- `reg_codes := R\d{2} (("&" | "、") R\d{2})*`（同一 entry 的多個 reg code **共用**其後 path）
- 多 entry 以**換行**或 `;` 分隔。
- 正規化：全形數字/字母/斜線/括號→半形；去除各段前後空白；`（二）`→`(二)`。
- 殘段處理：某 entry 認得 reg code 但其後 token 無法解析為合法 path → `RegulationRef(reg_code, "")`（整部規範）；
  某 entry **完全無 reg code** → 丟棄該 entry 並 **DEBUG log 計數**（`refs dropped n=…`，**不 log 原文**）。
- 輸出保序（依 input 出現順序）。

表驅動案例見 `contracts/fixtures/refs_cases.yaml`（14 案，全 synthetic）。

**prefix 雙向比對** `ref_matches_chunk`（reg_code 不同一律 false）：
1. `chunk_section_path == ref.section_path_prefix`（相等）
2. `chunk_section_path` 以 `ref.section_path_prefix + "/"` 開頭（ref 較粗）— **展開比對**，受 per-ref cap 限制
3. `ref.section_path_prefix` 以 `chunk_section_path + "/"` 開頭（ref 較細）
4. `ref.section_path_prefix == ""`（整部規範）→ 命中該 reg 任一 chunk — **展開比對**，受 cap 限制

**per-ref cap**：規則 2、4 屬「一 ref 展開命中多 chunk」，每 ref 上限 **3** chunk，依 `order`
（`RegulationStore.lookup` 回傳 row dict 所附欄位，已依此排序）由小到大取前 3。
以 path **分段**（`/`）比對，`五/(二)` 不得誤命中 `五/(二十)`。案例見 `contracts/fixtures/prefix_match_cases.yaml`。

### 4.2 build ② `build_retrieval_map()`（canonical 抽取 + 預算）— `scripts/build_regulation_index.py`

> **seam 函式名（Lead 鎖定）**：本 script 單檔承載雙 spec。p3-01 暴露 `build_index()` / `print_chunk_tree()` /
> `iter_regulation_chunks()`；本 spec 的入口為 `build_retrieval_map()`（build ②）與 `run_eval()`（build ④）。
> 單一 `main()` CLI 由 3b T2 統一擁有；各入口間**函式邊界分離**、互不呼叫內部細節。

**前置條件**：執行前必須確認 `git check-ignore data/rag/` 通過（T5 於 3b 補 `.gitignore` **先於任何 build 執行**；
未通過即中止，避免含法規摘錄的產物誤入庫）。

流程：
1. 從 `data/original/附件三`（官方 F03 模板）讀 20 檢核項：`item_id / topic / description / canonical_ref_raw`（L 欄）。
2. 從 `data/original/附件二`（官方 F02 模板）讀 F02 題文：`qid / question_text`
   （**僅 build 時記憶體使用；絕不落任何會入 git 的檔**——**第 1 次重複提醒：法規/題文內容不落 git**）。
3. 每 F03 項：
   - curated：`parse_regulation_refs(canonical_ref_raw)` → 對每 ref 呼叫
     `RegulationStore.lookup(reg_code: str, section_path_prefix: str) -> list[dict]`（**scalar filter 粗篩**；
     回傳 **row dict** 含 `id/reg_code/section_path/title/text/order`，依 `order` 排序）→
     **正準 prefix 細篩由本 spec 的 refs 邏輯負責**（`ref_matches_chunk` + per-ref cap）→
     `RetrievedSection(origin="curated", score=None)`。
   - semantic：`EmbeddingClient.embed(f"{topic}:{description}")` → store 向量檢索 `top_k=4`，套 COSINE `score_threshold`
     → `RetrievedSection(origin="semantic", score=…)`。
   - 合併去重：**curated 優先**；semantic 中與任一 curated 同 `(reg_code, section_path)` 或互為 prefix 者剔除；
     `cap = max_sections_per_item`（p3-03 config，預設 3）。
4. 每 F02 題：semantic `top_k` → `F02QuestionRetrieval`。
5. 組 `RetrievalMap(schema_version=1, built_at=<ISO>, embedding_model=<名>, f03_items, f02_questions)`。
6. **原子寫**：寫 `data/rag/retrieval_map.json.tmp` → `os.replace` rename 為 `retrieval_map.json`。
   此檔含法規摘錄，**必在 gitignore 區**（**第 2 次重複提醒：法規內容不落 git**；`data/rag/` 由 T5 於 3b 補入 `.gitignore`）。

### 4.3 build ④ `run_eval()`（CLI `--eval`）

以 20 F03 項的 curated（共 21 筆 ref）為 ground truth，對每項跑 semantic 檢索，報告：
- **recall@k**：curated 命中的 `(reg_code, section_path)` 是否落在 semantic top-k（k∈{1,4} 至少報 top-4）。
- **分數分佈**：命中 vs 未命中的 COSINE 分數直方/分位，供人工定 `score_threshold`。

`score_threshold` **依此報告校準後寫入 config**；本 spec 的 AC **不得寫死未經校準的門檻值**。
`--eval` 輸出走 stdout/報表，**不得重導入任何會被 commit 的檔**（審查修訂 L4）。

### 4.4 `rag/mapping.py`（runtime loader）

```python
class RetrievalMapError(RuntimeError): ...          # 缺檔/版本不符/schema 不合
def load_retrieval_map(path: str | Path | None = None) -> RetrievalMap: ...
```
- 缺檔 → `RetrievalMapError`（訊息含路徑，不含內容）。
- `schema_version` != 現行版本 → `RetrievalMapError`（明確標示版本不符）。
- JSON schema 不合（`model_validate` 失敗，含 `extra="forbid"`）→ `RetrievalMapError`。
- **runtime 零 embedding、零 Milvus**（**第 1 次重複提醒**）：loader 只讀 JSON + Pydantic 驗證，不 import/實例化 EmbeddingClient/RegulationStore。

### 4.5 `parsers/f03_parser.py` + `models/f03.py` + `review_config.yaml`（T4 擁有）

- `models/f03.py`：`F03ChecklistItem` 新增 `regulation_ref_raw: str | None = None`。
- `review_config.yaml`：`f03.checklist.regulation_ref_col: L`。
- `parse_f03_checklist`：讀 `L{r}` → `regulation_ref_raw`（`clean()` 後存原始字串，**不解析**）。
  既有 20 項的其餘欄位解析**完全不變**（回歸）。parser 不懂 RAG。
- 測試 fixture 產生器 `tests/fixture_builder.py` 由 **T4 於 3b 擴充**（加 L 欄寫入），本 spec 僅描述期望行為。

---

## 5. 特別處理

- **隱私**：refs 丟棄殘段只 DEBUG 記**計數**，不記原文。build ② 讀官方題文/法規僅記憶體使用；
  產物 `retrieval_map.json` 含法規摘錄 → 落 `data/rag/`（gitignored）。log 不得出現法規摘錄、題文、L 欄原文。
- **原子寫**：`temp + os.replace`，避免半寫檔被 runtime 讀到（審查修訂 M7）。
- **cap 與去重順序**：先 curated 全收，再逐一 semantic 判去重，最後整項 `cap`（curated 不因 cap 被砍在 semantic 之前）。
- **F02 qid 對齊**：`f02_questions` 的鍵為 `f02_scoring.yaml` 的葉層 qid（如 `UC-01`、`UC-04-01`），與 `F02Form.answers` 鍵一致。

---

## 6. Acceptance Criteria（皆可 pytest 驗證，引用 `contracts/fixtures/`）

- **AC-1**（refs 表驅動）：對 `refs_cases.yaml` 每案，`parse_regulation_refs(input)` 結果的
  `[(reg_code, section_path_prefix)]` 與 `expected` 完全相等（保序）。
- **AC-2**（正規化）：`fullwidth_normalized`、`whitespace_stripped` 兩案通過（全形→半形、去空白、`（二）`→`(二)`）。
- **AC-3**（殘段退整部規範）：`reg_code_with_unparseable_tail` → `[RegulationRef("R06", "")]`。
- **AC-4**（共用 path 展開）：`shared_path_ampersand`、`shared_path_dunhao` → 兩個 ref 共用同一 path、保序。
- **AC-5**（多 entry）：`multi_entry_newline`、`multi_entry_semicolon` 各解析出兩 entry。
- **AC-6**（丟棄計數）：`garbage_no_reg_code_dropped`、`mixed_valid_entry_and_garbage_entry` 的 `expect_dropped` 相符；
  解析過程對丟棄殘段只計數（可透過回傳統計或 caplog DEBUG 斷言），且 **caplog 不含 input 原文**。
- **AC-7**（prefix 布林）：對 `prefix_match_cases.yaml` 的 `match_cases` 每案，`ref_matches_chunk` == `expected`
  （含 `partial_token_not_prefix` 分段比對案）。
- **AC-8**（prefix cap）：對 `cap_cases` 每案，展開命中並依 `order` 取前 3 的路徑集合 == `expected_matched_paths`。
- **AC-9**（RetrievalMap round-trip）：`RetrievalMap.model_validate(json.load(retrieval_map_example.json))` 成功；
  `model_dump(mode="json")` 再 `model_validate` 一致（欄位/值不失真）。
- **AC-10**（minimal 驗證）：`retrieval_map_minimal.json` 通過 `model_validate`（空 map 合法）。
- **AC-11**（extra 禁止）：於 example JSON 頂層注入未知鍵 → `model_validate` 拋 `ValidationError`（`extra="forbid"`）。
- **AC-12**（loader 缺檔）：`load_retrieval_map(<不存在路徑>)` → `RetrievalMapError`。
- **AC-13**（loader 版本不符）：`load_retrieval_map(retrieval_map_bad_version.json)` → `RetrievalMapError`（版本不符）。
- **AC-14**（loader 零依賴）：以 monkeypatch 使 `EmbeddingClient` / `RegulationStore` 一經實例化即 raise，
  `load_retrieval_map(retrieval_map_example.json)` 仍成功（證明 runtime 不觸網、不開 Milvus）。
- **AC-15**（parser L 欄）：以 `tests/fixture_builder.py`（3b 由 T4 擴充寫入 L 欄）產含 L 欄的 F03 fixture，
  `parse_f03_checklist` 讀出對應 `regulation_ref_raw`；未填 L 欄者為 `None`。
- **AC-16**（parser 回歸不變）：既有 F03 fixture 解析出的 `item_id/lifecycle/topic/description/check_state/evidence_*`
  與擴充前一致（新增欄不影響既有欄；比對既有 `tests/test_f03_parser*.py` 斷言不需改動即通過）。
- **AC-17**（build ② 合併去重，mock embedding）：以 fake `EmbeddingClient` + fake `RegulationStore`
  （`lookup` 回傳 row dict 含 `order`）跑 `build_retrieval_map()`，產出的 `F03ItemRetrieval.sections` 中
  curated 全保留，且與 curated 同 `(reg_code, section_path)` 或互為 prefix 的 semantic 被剔除，
  整項 `len(sections) <= max_sections_per_item`。
- **AC-18**（build ④ eval 跑通，mock embedding）：以 fake embedding 對 21 筆 curated ground truth 跑 `run_eval()`，
  輸出含 recall@k 與分數分佈欄位且不拋例外；**AC 不斷言任何具體 threshold 數值**（待人工校準）。
- **AC-19**（原子寫 + gitignore）：`build_retrieval_map()` 寫檔採 temp+rename（可斷言過程無殘留 `.tmp`）；
  執行前置檢查 `git check-ignore data/rag/` 未通過即中止；`git check-ignore data/rag/retrieval_map.json` 命中
  （此 gitignore 條目由 T5 於 3b 補、**先於任何 build 執行**；AC 於 3b 生效）。

> AC 總數：**19**。

---

## 7. 與其他 spec 的介面（型別名逐字引用，Lead 鎖定）

| 型別 / 符號 | 定義處 | 本 spec 用途 |
|---|---|---|
| `RegulationChunk(reg_code, reg_title, section_path, title, text, chunk_seq, order)` | p3-01 | 索引最小單位（僅語意對齊參考；build ② **不**直接消費此型別，見下列 lookup） |
| `RegulationStore.lookup(reg_code: str, section_path_prefix: str) -> list[dict]` | p3-01 | build ② curated 檢索：scalar filter **粗篩**，回傳 **row dict**（含 `id/reg_code/section_path/title/text/order`，依 `order` 排序）。**注意**：row 不含 `reg_title`/`chunk_seq`——`RetrievedSection` 不需要它們，確認無缺。正準 prefix **細篩**由本 spec `ref_matches_chunk` + per-ref cap 負責 |
| collection `order` INT64 欄 | p3-01（Milvus schema） | per-ref cap 排序依據（lookup 已依此排序） |
| `IndexMeta(...)` | p3-01 | build ①/② 共用；本 spec 不直接消費 |
| `EmbeddingClient`（build-time，POST `/embeddings`） | p3-01 | build ②/④ 語意檢索；AC-14/17/18 以 fake 注入 |
| `RegulationStore`（Milvus Lite 包裝） | p3-01 | build ② lookup + 向量檢索；AC-14 以 fake 注入 |
| `build_index()` / `print_chunk_tree()` / `iter_regulation_chunks()` | p3-01（`scripts/build_regulation_index.py`） | 同檔 seam 函式（Lead 鎖定命名）；本 spec 不呼叫其內部 |
| `build_retrieval_map()` / `run_eval()` | **本 spec**（同 script 檔） | build ②/④ 入口（Lead 鎖定命名）；單一 `main()` CLI 由 3b T2 統一擁有 |
| `RegulationRef(reg_code, section_path_prefix)` | **本 spec** `contracts/schemas.py` | refs 解析輸出；p3-03 F02 查表復用 |
| `RetrievedSection(reg_code, section_path, title, excerpt, score, origin)` | **本 spec** | 映射條目；p3-03 判讀摘錄來源 |
| `F03ItemRetrieval(item_id, canonical_topic, canonical_description, canonical_ref_raw, refs, sections)` | **本 spec** | p3-03 F03 判讀輸入 + TEMPLATE_REF_MODIFIED 比對 |
| `F02QuestionRetrieval(qid, sections)` | **本 spec** | p3-03 F02 查表輸入 |
| `RetrievalMap(schema_version, built_at, embedding_model, f03_items, f02_questions)` | **本 spec** | runtime 唯一輸入；p3-03 engine 整合經 `load_retrieval_map` 取得 |
| `F03ChecklistItem.regulation_ref_raw: str \| None` | **本 spec**（改 `models/f03.py`） | p3-03 判 TEMPLATE_REF_MODIFIED |
| `f02_scoring.yaml` `questions[qid].score_on` | 既有 `config/f02_scoring.yaml` | F02 qid 對齊（本 spec 只讀語意，不改） |

section_path 正準形式：`五/(二)/2`；R01 用 `第一條`。引用顯示格式（`R03 五、(二) 2`）由 **p3-03** 負責。

---

## 8. Out of scope（再次強調）

- **runtime embedding / runtime Milvus 不做**（**第 2 次重複提醒**）：runtime 只經 `load_retrieval_map` 讀 JSON；
  所有 embedding/Milvus 呼叫僅存在於 build-time script。AC-14 明文驗證。
- **F01 不做**（**第 1 次重複提醒**）：本 spec 與整個 Phase 3 皆不處理 F01，屬 Phase 4+。
- **F02 不上 LLM**（**第 1 次重複提醒**）：本 spec 只產 F02 語意映射；F02 的使用是 p3-03 的**純規則查表**，無任何 LLM。
- 判讀 / Finding 產生 / engine 整合 / rag config → p3-03。
- PDF 解析 / chunker / EmbeddingClient / RegulationStore 實作 → p3-01。

---

## 9. 參考

- 計畫：`hi-wondrous-crown.md`（M1 build 預計算、H3 canonical 基準、M7 原子寫、H1 內容不落 git、M2 eval 校準）。
- 既有程式：`src/govcheck/parsers/f03_parser.py`（L 欄擴充母本）、`src/govcheck/models/f03.py`、
  `src/govcheck/config/review_config.yaml`（`f03.checklist`）、`src/govcheck/config/f02_scoring.yaml`（`score_on`/qid）。
- 上游合約：p3-01 `contracts/schemas.py`（`RegulationChunk` / `IndexMeta` / `EmbeddingClient` / `RegulationStore`）。
- 本 spec 合約：`contracts/schemas.py`；fixtures：`contracts/fixtures/{refs_cases.yaml, prefix_match_cases.yaml,
  retrieval_map_example.json, retrieval_map_minimal.json, retrieval_map_bad_version.json}`。
