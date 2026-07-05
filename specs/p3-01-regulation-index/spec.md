# Spec p3-01-regulation-index — 法規語料解析、切塊、embedding 與向量索引（build ①③）

> SDD Spec｜作者 S1（spec-author）｜Phase 3a｜狀態：待人工審核
> 對應計畫：`/Users/kee/.claude/plans/hi-wondrous-crown.md`（發現矛盾以計畫為準並於本文 §9 註記）
> 定位不變：**AI 初判、人工覆核，資料全程地端**。本 spec 全部產物為 **build-time 工具**，
> runtime 審查流程**不呼叫** embedding、**不連** Milvus（第 1 次強調，另見 §3-D1、§8）。

---

## 1. Context

Phase 3 建置 R01–R07 治理辦法的向量索引與 canonical 檢索映射（**build-time RAG**），
供審查時對 F03 佐證做 LLM 符合性判讀、對 F02 觸發風險題做規則式義務條文提示，引用到條號/章節層級。

本 spec 負責整條 build 管線的**前半**：把 7 份治理辦法 PDF 轉為乾淨文字行 → 切塊為
`RegulationChunk` → 以 `EmbeddingClient` 產生向量 → 寫入 Milvus Lite collection 與 sidecar `IndexMeta`。
產出的 chunk 語料與索引，供下游 `p3-02-canonical-map` 抽 canonical 映射、`p3-03-rag-checks` 於審查時查表消費。

新增模組（本 spec 範圍，實作屬 Phase 3b）：
`rag/pdf_text.py`、`rag/chunker.py`、`rag/models.py`、`rag/embedding.py`、`rag/store.py`，
以及 `scripts/build_regulation_index.py` 的 build ①（索引）與 ③（`--dry-run`）部分。

---

## 2. Scope

### 2.1 In scope（本 spec 定義、Phase 3b 據此實作）
- **`rag/models.py`**：`RegulationChunk`、`IndexMeta`（Pydantic v2；契約見 `contracts/schemas.py`，逐字使用）。
- **`rag/pdf_text.py`**：pdfplumber 逐頁 `extract_text` → 清洗（刪頁碼行、刪重複頁首尾、CJK 接行）→ `list[str]` 乾淨行。
- **`rag/chunker.py`**：吃「乾淨文字行」→ `list[RegulationChunk]`。條文式（R01）與章節式（R02–R07）兩套規則。
- **`rag/embedding.py`**：`EmbeddingClient`（build-time；鏡射 `llm/client.py` 的 `ChatClient`），`/embeddings` 端點，失敗一律 `EmbeddingError`。
- **`rag/store.py`**：`RegulationStore`（build-time；`pymilvus.MilvusClient` 包裝），collection `regulation_chunks` + sidecar `IndexMeta`；open 時核對 model/dim。
- **`scripts/build_regulation_index.py` 的 ①③**：①PDF→chunk→embed→Milvus Lite + `index_meta.json`；③`--dry-run` 印 chunk 樹供人工抽核。

### 2.2 Out of scope（明確不做，交由他 spec 或 Phase 4+）
- ❌ **runtime embedding 呼叫**：審查時零 embedding、零 Milvus 連線（build 時預算，runtime 查表）。第 1 次強調（另見 §8）。
- ❌ **canonical map 抽取（build ②）與 `--eval`（build ④）**：屬 `p3-02-canonical-map`。第 1 次強調（另見 §8）。
- ❌ `rag/refs.py`、`rag/mapping.py`、`rag/config.py`：屬 p3-02/p3-03。本 spec 僅**引用**其存在，不定義。
- ❌ F03/F02 的 check 邏輯、engine 整合：屬 p3-03。
- ❌ F01 任何比對：Phase 4+。
- ❌ `docs/milvus_migration.md`：Lead 提供骨架、T5 整稿（本 spec 僅於 §5 註明 URI 切換前提）。

---

## 3. 核心設計決策

### D1. 全部為 build-time 工具，runtime 不觸碰
- **決定**：`EmbeddingClient`、`RegulationStore` 僅在 `scripts/build_regulation_index.py` 內被建立與呼叫；審查管線（`review/engine.py` 及 checks）**不 import 也不呼叫**它們。
- **理由**：計畫審查修訂 M1——檢索 query 全來自模板靜態文字，每次審查結果恆定 → build 時預算，runtime 查表即可；砍掉 runtime 端點依賴、embedding 降級階梯與 Milvus file lock 問題。
- **影響**：本 spec 的測試**不需真實端點、不需網路**；EmbeddingClient 測試以 mock `requests` 驗證；store 測試用 Milvus Lite 於 `tmp_path` 建臨時 db。Phase 4 動態檢索可直接重用這兩個類別。

### D2. `chunker` 只吃文字行，不碰 PDF
- **決定**：切塊與 PDF 解析解耦——`pdf_text.py` 負責 PDF→乾淨行，`chunker.py` 只接受 `list[str]`。
- **理由**：golden test 可用純文字 fixture（`synthetic_r0X_lines.txt`），不依賴 `data/original` 的真 PDF，可離線、可重現、有 ground truth。
- **影響**：chunker 的兩個 golden（AC-2/AC-3）完全離線；真 PDF 只在 `@pytest.mark.local_data` 冒煙（AC-18）用到。

### D3. `EmbeddingClient` 鏡射 `ChatClient`
- **決定**：與 `llm/client.py` 同一模式——`requests.post`、`from_config()`、失敗一律轉自訂例外、`api_key` 只走 env、log 只記端點/狀態碼/例外型別/批量計數。
- **理由**：計畫「統一沿用現有 ChatClient 的 requests + OpenAI 相容接口」，不引入 openai SDK / sentence-transformers；一致的錯誤與隱私處理慣例。
- **影響**：呼叫 `/embeddings`（非 `/chat/completions`），payload `{model, input:[texts]}`，回 `list[list[float]]`；例外類別為 `EmbeddingError`（對應 `LLMError`）。**絕不 log input 文字**（AC-15）。

### D4. section_path 正準化 + breadcrumb 前綴
- **決定**：`section_path` 採機器正準形式（條文式 `第一條`；章節式 `五/(二)/2`，全形括號正規化為半形）；`text` 另含人類可讀的 breadcrumb 前綴（`【R03 辦法名|五、L1標題>(二)L2標題】\n`+內文）。
- **理由**：`section_path` 用於決定性 PK 與引用比對（需穩定正準）；breadcrumb 讓單一 chunk 檢索出來即帶足上下文，供 LLM 判讀與人工回溯。
- **影響**：兩者是**兩種表示並存**（正準路徑 vs 可讀麵包屑），非矛盾；chunk PK = `{reg_code}:{section_path}:{chunk_seq}`（`RegulationChunk.chunk_id`）。

### D5. `RegulationChunk` 是 build 模型，Milvus row 是其子集（Lead 裁決修訂）
- **決定**：`RegulationChunk` 有 7 欄；Milvus collection 落 `id / reg_code / section_path / title / text / order / embedding` **七欄**。`order` 為 INT64 scalar 欄（**Lead 裁決加入**：p3-02 curated lookup 需以 `order` 做 per-ref cap 排序——`section_path` 含中文數字，無法可靠排序）。`chunk_seq` 維持編入 `id` 不獨立落欄；`reg_title` 維持不落欄（已烘進 `text` breadcrumb 且記於 `IndexMeta`，p3-02 的 RetrievedSection 不需要它）。
- **理由**：Milvus 只落檢索/引用/排序必要欄；`reg_title` 可由 breadcrumb 與 `IndexMeta` 對回。
- **影響**：`store.insert()` 負責 `RegulationChunk → row` 投影；`store.search()`/`store.lookup()` 回傳 row dict（含 `order`，不含 `reg_title`；`chunk_seq` 可由 `id` 切出）。§4.5 明列投影表與 `lookup()` 介面。

---

## 4. 詳細規格

### 4.1 `rag/pdf_text.py`
**介面（建議）**：
```python
def load_clean_lines(pdf_path: str | Path) -> list[str]: ...       # 真 PDF 入口（pdfplumber）
def clean_pages(pages: list[list[str]]) -> list[str]: ...          # 純函式，吃「每頁原始行」→ 乾淨行（golden 測此函式）
```
`load_clean_lines` 內部：逐頁 `page.extract_text()` → `splitlines()` 得 `pages: list[list[str]]` → 交給 `clean_pages`。
**清洗規則（`clean_pages`，順序固定）**：
1. **R1 刪頁碼行**：某行 `strip()` 後符合任一 pattern 即刪：`^-?\s*\d+\s*-?$`、`^第\s*\d+\s*頁$`、`^\d+\s*/\s*\d+$`。
2. **R2 刪重複頁首尾**：統計每個 `strip()` 文字出現在幾頁；當 `頁數 >= 3` 且 `出現頁數 / 總頁數 >= 0.60`，該文字所有出現全數刪除（頁首/頁尾樣板）。`頁數 < 3` 時不套用（樣本太少易誤刪）。
3. **R3 CJK 接行**（於各頁「殘餘行」內、**不跨頁**）：對相鄰行，若「上一行末字 ∉ `{。：；！？}`」**且**「下一行非空」**且**「下一行非標題 token」→ 直接串接（CJK 無空白）。標題 token = 符合 `^第[一二三四五六七八九十百]+條` / `^[一二三四五六七八九十]+、` / `^[（(][一二三四五六七八九十]+[）)]` / `^\d+[.、]` 任一。
4. 攤平所有頁的行為單一 `list[str]` 回傳。

（golden：`contracts/fixtures/dirty_pdf_lines.yaml`，`pages → expected_lines`，已於 S1 端以參考實作驗證自洽。）

### 4.2 `rag/chunker.py`
**介面（建議）**：
```python
def chunk_regulation(lines: list[str], *, reg_code: str, reg_title: str,
                     style: Literal["article", "chapter"]) -> list[RegulationChunk]: ...
```
`style` 由呼叫端依 reg 註冊表提供（R01=`article`，R02–R07=`chapter`）；亦可提供依 reg_code 推定的預設，但**契約以顯式傳入為準**。
**共通**：`order` 為該法規內 chunk 的 0-based 文件順序；`chunk_seq` 預設 0，僅「句號硬切」時於同一 `section_path` 內遞增；`title`=本 chunk 葉節點標題文字（見下）；`text`=breadcrumb + 內文。

**A. 條文式（R01）**
- `^第[一二三四五六七八九十百]+條` 起新 chunk；`section_path` = 該條號（如 `第一條`）；`title` = 條號。
- 條號標籤（`第N條[\s　]*`）移入 breadcrumb，並自 body 剝除；同條後續行（未被 pdf_text 併入者）以 `\n` 保留於 body。
- breadcrumb：`【{reg_code} {reg_title}|{section_path}】\n`。
- golden：`synthetic_r01_lines.txt` → `expected_chunks_r01.yaml`（4 chunk；第三條含一續行）。

**B. 章節式（R02–R07）三層**
- 層級 token：L1 `^([一二三四五六七八九十]+)、`、L2 `^[（(]([一二三四五六七八九十]+)[）)]`、L3 `^(\d+)[.、]`。
- **chunk 單位 = L2 節點**（含其下 L3 子項，L3 以原 marker 內聯保留於 body）。
- **L1 引言自成 chunk**：L1 標題行後、第一個 L2 之前的內文 → 一個 chunk，`section_path` 如 `五`，`title`=L1 標題文字。若 L1 無引言內文則不產生 L1 chunk。
- **section_path 正準化**：L1=中文數字（去「、」）、L2=`(中文數字)`（**全形括號 → 半形**）、L3=阿拉伯數字；以 `/` 串接（如 `五/(二)/2`）。
- **breadcrumb**：列出本 chunk 各祖先層級與標題，L1=`中文、標題`、L2=`(中文)標題`、L3=`數字.`（如 `【R03 辦法名|二、風險評鑑作業>(一)評鑑程序>1.】`）。標題行本身不重複進 body。
- **長度控制（目標 200–800 字，以 body 內文字數計，不含 breadcrumb）**：
  - L2 body ≤ 800 → 單一 chunk（L3 內聯，marker 保留）。
  - L2 body **> 800 → 降為逐 L3 切**：每個 L3 一個 chunk，`section_path` 補上 L3 號（`五/(二)/2`），`title`=父 L2 標題；L3 marker（`^\d+[.、]\s*`）移入 breadcrumb 並自 body 剝除。
  - **單一 L3 仍 > 800 → 按句號（`。`）硬切**：`section_path` **不變**，以 `chunk_seq` 0,1,… 區分；貪婪打包句子，當「現有緩衝非空且加入下一句後長度 > 800」即先 flush；串接所有 seq 之 body 應可還原原 L3 內文。
- **`< 30 字純標題併入父層**：某節點自身文字（標題＋內文）少於 30 字且無實質內文（純標題）→ 不自成 chunk，其標題行 append 到父層 chunk 的 body 末尾（以 `\n` 相接）。
- golden：`synthetic_r03_lines.txt` → `expected_chunks_r03.yaml`（5 chunk）覆蓋：L1 引言（`一`，含併入的純標題「（二）保留」）、L2 內聯（`一/(一)`，body 112 字）、L2 body 884 字 > 800 降為逐 L3（`二/(一)/1..3`，各 < 800）。**句號硬切**分支以 property-based AC-6 驗（不入 golden，避免逐字元脆弱）。

### 4.3 `rag/models.py`
逐字實作 `contracts/schemas.py` 的 `RegulationChunk`、`IndexMeta`（`model_config = ConfigDict(extra="forbid")`）。
`RegulationChunk.chunk_id` property = `{reg_code}:{section_path}:{chunk_seq}`（Milvus PK，決定性）。

### 4.4 `rag/embedding.py` — `EmbeddingClient`
鏡射 `ChatClient`：
- `__init__(*, base_url, model, api_key=None, timeout=..., ...)`；`base_url.rstrip("/")`。
- `from_config(cfg: dict | None = None)`：讀 build-time 設定（`embedding_base_url / embedding_model / embedding_dim`）；`api_key` **只從 env**（沿用 `GOVCHECK_*` 慣例，鍵名由 p3-02 的 `rag/config.py` 決定；本 spec 僅要求「機密只走 env、不從 YAML 退回」）。
- `endpoint` property = `{base_url}/embeddings`。
- `embed(texts: list[str]) -> list[list[float]]`：POST payload `{"model": model, "input": texts}`；回傳依 OpenAI 相容格式 `data[i].embedding` 組成 `list[list[float]]`，順序須對齊輸入。
- **失敗一律 `EmbeddingError`**（連線例外 / 非 2xx / JSON 解析失敗 / 格式非預期 / 回傳筆數與輸入不符）。
- **隱私**：`log.warning` 只記 `endpoint / status_code / type(exc).__name__ / len(texts)`；**絕不記 `input` 文字、回傳向量、payload、resp.text**（AC-15）。

### 4.5 `rag/store.py` — `RegulationStore`
`pymilvus.MilvusClient` 包裝。
- collection `regulation_chunks`，欄位（**七欄**，Lead 裁決定案）：
  | 欄位 | 型別 | 來源（RegulationChunk→row 投影） |
  |---|---|---|
  | `id` | VARCHAR PK | `chunk.chunk_id` |
  | `reg_code` | VARCHAR | `chunk.reg_code` |
  | `section_path` | VARCHAR | `chunk.section_path` |
  | `title` | VARCHAR | `chunk.title` |
  | `text` | VARCHAR | `chunk.text` |
  | `order` | INT64 | `chunk.order`（p3-02 per-ref cap 排序用；Lead 裁決加入） |
  | `embedding` | FLOAT_VECTOR(dim) | 由 `EmbeddingClient.embed` 產生；dim 來自 config（bge-m3=1024） |
  - 索引 AUTOINDEX + metric COSINE（~200–400 chunk，FLAT 足矣）。
  - 不落欄：`chunk_seq`（編入 `id`）、`reg_title`（breadcrumb 已含、`IndexMeta` 可對回）。
  - `VARCHAR` 各欄 `max_length` 由實作以寬裕餘量設定（見 §5「VARCHAR 長度語意」註記；**AC 不綁定具體長度值**）。
- **URI**：Milvus Lite `data/milvus/governance.db`（worktree 本地）；URI 填 `http(s)://` 即切 Server（程式零改動）。
- **sidecar** `data/milvus/index_meta.json`：`IndexMeta` 序列化（`schema_version / built_at / embedding_model / embedding_dim / chunks_per_reg / source_sha256`）。**原子寫入**（temp + rename）。
- 介面（建議）：`recreate(dim)`（drop-and-recreate）、`insert(chunks, vectors)`、`write_meta(meta)`、`open()`（載入既有索引 + 讀 sidecar）、`search(vector, top_k)`、`lookup(...)`（見下）。
- **`lookup(reg_code: str, section_path_prefix: str) -> list[dict]`**（Lead 裁決新增，供 p3-02 curated lookup）：
  scalar filter 查詢（不走向量），回傳 row dict 含 `id / reg_code / section_path / title / text / order`，**依 `order` 升冪排序**。
  prefix 語意＝正準 `section_path` 的 **prefix 粗篩**（如 `"五/(二)"` 命中 `五/(二)` 與 `五/(二)/1`、`五/(二)/2`…）；
  細篩（全形容錯、`R03&R07` 展開等 refs 邏輯）由 p3-02 `rag/refs.py` 負責，本方法不做。
- **`open()` 核對**：讀 sidecar `IndexMeta`，若 `embedding_model` 或 `embedding_dim` 與傳入 config 不符 → **raise**（拒開，避免用錯模型/維度檢索）。缺 sidecar 亦 raise（AC-10）。

### 4.6 `scripts/build_regulation_index.py`（本 spec：①③）
- **①（build_index）**：對 7 份 PDF：`load_clean_lines` → `chunk_regulation`（依 reg 註冊表帶 style/title）→ 統計每 reg chunk 數 → `EmbeddingClient.embed`（**每批 32**）→ `store.recreate(dim)` → `insert` → `write_meta`（含 `source_sha256`：對每份來源 PDF 算 sha256）。
- **③（`--dry-run`）**：跑到 chunk + 統計為止，**印 chunk 樹**（reg → section_path → title/字數）供人工抽核；**不呼叫 embedding、不寫 Milvus**。
- **輸出合規**：`--dry-run` 只印到 stdout；**不得寫出任何會被 commit 追蹤的檔案**（不寫 `src/`、`specs/`、`tests/` 下受追蹤路徑）。索引產物一律落 `data/`（gitignored）。→ AC-17。
- **建置產物皆屬 `data/`（gitignored）**：法規摘錄/切塊內容**不落 git 區**（隱私）。
- **seam 給 p3-02（Lead 裁決核准，函式名正式鎖定）**：本 spec 定義 ①③ 的可組合函式——
  `build_index()`（①）、`print_chunk_tree()`（`--dry-run` ③）、`iter_regulation_chunks()`（供 build ② 消費 chunk 流）。
  p3-02 的 build ②④ 入口鎖定為 `build_retrieval_map()`、`run_eval()`；**單一 `main()` CLI 由 Phase 3b 的 T2 統一擁有**，
  於其中依 argparse 分派上述四個入口 + `--dry-run`。

---

## 5. 特別處理
- **隱私 / log**：全模組經 `logging_setup`（`get_logger`）；只記代碼/計數/耗時/例外型別/端點/狀態碼；**絕不記**規範摘錄、chunk 內文、embedding input、payload、resp.text、api_key、Authorization（CLAUDE.md 硬性）。level 慣例：DEBUG=流程（如「reg=R03 chunks=45」）、INFO=重點（如「index built regs=7 total=312」）、WARNING=降級/端點異常、exception=堆疊。
- **`data/original` 唯讀**：build 只讀不寫；本 worktree symlink 未建，真 PDF 相關測試一律 `@pytest.mark.local_data` 且 data 缺 → `skip`。
- **Milvus Lite 檔鎖**：預計算模式下，索引僅在 build script 內開啟一次即用即關；runtime 不開，故無並發鎖問題（M7）。
- **VARCHAR 長度語意（Lead 裁決註記）**：實作時（3b T2）**必須確認** pymilvus `VARCHAR max_length` 的語意（bytes vs chars——`id`/`section_path`/`text` 含 CJK 多位元組），並以**寬裕餘量**設定（建議 `text` 8192、`id` 256，其餘比照放寬）；**AC 不綁定具體長度值**。
- **URI 切 Server**：`docs/milvus_migration.md`（T5）載明 air-gapped 前提（pymilvus wheel 可得性、PDF 進 prod 機器）；本 spec 僅保證「URI 前綴 http(s):// → Server，程式零改動」。
- **`rag/__init__.py` 規定留空**（所有權糾紛預防）。
- **L1 無引言時的 <30 字純標題 L2（Lead 裁決，3b T1 plan 批准時定案）**：若 L1 節點無 intro chunk 可併入，<30 字純標題 L2（如「（二）保留」）**直接 skip、不產 chunk**，以 DEBUG log 記計數。理由：無義務內容，獨立成 chunk 徒增檢索噪音。

---

## 6. Acceptance Criteria

> 每條可用一個 pytest 驗證；引用具體 fixture；含邊界與失敗情況。LLM/端點一律 mock，不打真端點。

- **AC-1（pdf_text 髒輸入 golden）**：載入 `contracts/fixtures/dirty_pdf_lines.yaml`，`clean_pages(pages) == expected_lines`。涵蓋：刪頁碼行（`- 3 -`）、刪 100% 重複頁首（「台新銀行內部文件範例」）與頁尾（「機密」）、CJK 接行（第一條斷句併回）、**不併行**邊界（下一行為標題 token「第三條…」／上一行以 `：` 結尾）。
- **AC-2（chunker 條文式 golden）**：`chunk_regulation(load(synthetic_r01_lines.txt), reg_code="R01", reg_title="AI系統範例導入辦法（測試用）", style="article")` 之輸出，**逐欄**等於 `expected_chunks_r01.yaml`（4 chunk；`section_path` 為 `第一條`..`第四條`；第三條 body 含 `\n` 續行；breadcrumb 前綴正確、條號自 body 剝除）。
- **AC-3（chunker 章節式 golden）**：以 `synthetic_r03_lines.txt`、`reg_code="R03"`、`reg_title="AI系統範例治理辦法（測試用）"`、`style="chapter"` 產出，逐欄等於 `expected_chunks_r03.yaml`（5 chunk）。斷言覆蓋：L1 引言自成 chunk（`一`）、**`<30 字純標題「（二）保留」併入父層 `一`**（在 chunk `一` 的 text 末尾）、L2 內聯 L3（`一/(一)`，marker 保留）、**全形括號正規化**（`（一）`→ `section_path`/breadcrumb 的 `(一)`）。
- **AC-4（超長 L2 降 L3）**：AC-3 同一輸出中，L2 `二/(一)`（body 884 字 > 800）**降為 3 個 chunk**，`section_path` 分別 `二/(一)/1`、`二/(一)/2`、`二/(一)/3`，`title` 皆為父標題「評鑑程序」，各 chunk body < 800，L3 marker 移入 breadcrumb 並自 body 剝除，`order` 連續。
- **AC-5（section_path 正準 & breadcrumb 契約）**：對 AC-2/AC-3 產物斷言 `chunk.chunk_id == f"{reg_code}:{section_path}:{chunk_seq}"`，且 breadcrumb 前綴格式符合 §4.2（`【code title|…】\n` 開頭、以正確層級路徑呈現）。
- **AC-6（單 L3 超長 → 句號硬切，property-based）**：測試內以 synthetic 句子（`「…。」×N`）組出一個 > 800 字的單一 L3 輸入，斷言：產出多個 chunk 共用**同一 `section_path`**、`chunk_seq` 為 `0,1,…` 連續遞增、每個 chunk body ≤ 800、串接所有 seq 的 body 可**無損還原**原 L3 內文。
- **AC-7（schema round-trip + extra=forbid）**：`RegulationChunk`/`IndexMeta` 能載入 `regulation_chunk_example.json`/`index_meta_example.json` 並 `model_dump()` 往返一致；傳入未知欄位 → `ValidationError`（`extra="forbid"`）。
- **AC-8（chunk_id 決定性 PK）**：對固定 `RegulationChunk`，`chunk_id` 穩定等於 `{reg_code}:{section_path}:{chunk_seq}`（如 `R03:一/(一):0`）；不同 `chunk_seq` 產生不同 id。
- **AC-9（store round-trip，真 Milvus Lite、無網路）**：於 `tmp_path` 建 Milvus Lite db，`recreate(dim=8)` → `insert` 數個 chunk + 對應假向量（zero/隨機，維度 8）→ `write_meta` → `open()` → 兩條讀取路徑皆驗證：
  (a) `search(query_vector, top_k)` 能取回既插入之 `id`/`text`；
  (b) `lookup(reg_code, section_path_prefix)` 以 prefix 粗篩取回正確子集（如插入 `五`、`五/(二)/1`、`五/(二)/2`、`六` 後，`lookup("R0X", "五/(二)")` 恰回 2 筆、不含 `六`），回傳 row dict 含 `id/reg_code/section_path/title/text/order` 且**依 `order` 升冪**。全程不連任何網路端點。
- **AC-10（meta 不符拒開）**：sidecar `IndexMeta` 的 `embedding_dim`（或 `embedding_model`）與 `open()` 傳入 config 不符 → raise；缺 sidecar 檔 → raise。
- **AC-11（EmbeddingClient 2xx 正常）**：mock `requests.post` 回 OpenAI 相容 `{"data":[{"embedding":[...]},...]}`，`embed([...])` 回 `list[list[float]]`，長度與順序對齊輸入。
- **AC-12（EmbeddingClient 4xx）**：mock 回 `status_code>=400` → `EmbeddingError`。
- **AC-13（EmbeddingClient 連線失敗）**：mock `requests.post` 丟 `requests.RequestException` → `EmbeddingError`。
- **AC-14（EmbeddingClient 格式異常）**：mock 回 2xx 但 JSON 缺 `data` / 非預期結構 / 回傳筆數 ≠ 輸入筆數 → `EmbeddingError`。
- **AC-15（log 不含 input 文字）**：以 `caplog` 觸發 AC-11/AC-12/AC-13 路徑，斷言任何 log record 的 message **不含** input 文字內容，只含端點/狀態碼/例外型別/批量計數。
- **AC-16（from_config + api_key 只走 env）**：`EmbeddingClient.from_config(cfg)` 正確取 `base_url/model/timeout`；`api_key` 僅在對應環境變數存在時帶入 `Authorization`，YAML 內即使有 key 也不採用。
- **AC-17（build `--dry-run` 合規）**：以 synthetic chunk 樹跑 `--dry-run`，斷言：印出 chunk 樹到 stdout、**不呼叫 embedding、不建 Milvus**、且**未寫出任何受 git 追蹤路徑下的檔案**（僅允許 `data/`/tmp）。
- **AC-18（`@pytest.mark.local_data` 真 PDF 冒煙）**：`data/original` 存在時，對真 R01 與某章節式 R0X 跑 `load_clean_lines` + `chunk_regulation`，僅斷言**結構事實**：chunk 數落合理範圍（全 7 份估 200–400）、R01 產出 `第一條`~`第四條`、章節式產出形如 `R03:五/(二)/2` 的 `section_path`（`:` 切出的中段符合 `L1/(L2)/L3` 正規式）。**不斷言任何條文內容**。data 缺 → `pytest.skip`。

---

## 7. 與其他 spec 的介面

| 對象 | 本 spec 暴露 | 型別 / 位置 | 消費方式 |
|---|---|---|---|
| **p3-02-canonical-map** | `RegulationChunk` | `rag/models.py`（契約 `contracts/schemas.py`） | canonical 抽取後餵 `EmbeddingClient`/`RegulationStore`；讀 chunk 的 `section_path`/`text` 做預算檢索 |
| p3-02 | `IndexMeta` | 同上 | 讀 `embedding_model/dim` 對齊、寫入 sidecar |
| p3-02 | `EmbeddingClient.embed(texts) -> list[list[float]]`（+`EmbeddingError`）、`EmbeddingClient.from_config()` | `rag/embedding.py` | build ②（canonical query 嵌入）與 ④（`--eval`）重用同一 client；失敗語意統一 `EmbeddingError` |
| p3-02 | `RegulationStore.search(vector, top_k)`、**`RegulationStore.lookup(reg_code, section_path_prefix) -> list[dict]`**（row dict 含 `id/reg_code/section_path/title/text/order`，依 `order` 升冪；prefix=正準 prefix 粗篩，細篩由 p3-02 `refs.py` 負責）、`open()`（meta 核對） | `rag/store.py` | ②語意 top-k 走 `search`；②curated（canonical L 欄）走 `lookup` 並以 `order` 做 per-ref cap 排序 |
| p3-02 | **`iter_regulation_chunks()`**（chunk 流，供 build ② 消費）、`build_index()`（①）、`print_chunk_tree()`（③） | `scripts/build_regulation_index.py` | seam 函式名由 Lead 鎖定；p3-02 於同檔以 `build_retrieval_map()`（②）、`run_eval()`（④）擴充，單一 `main()` 由 3b T2 統一擁有 |
| p3-02 | `pdf_text.load_clean_lines`、`chunk_regulation` | `rag/pdf_text.py`、`rag/chunker.py` | 從官方附件三抽 canonical L 欄/描述時重用清洗與切塊 |
| **p3-03-rag-checks** | （無直接型別依賴） | — | runtime 走 `retrieval_map`（屬 p3-02）；本 spec 產物皆 build-time，p3-03 不 import |

**契約凍結**：`RegulationChunk` / `IndexMeta` 欄位名與型別由 Lead 鎖定，p3-02 逐字消費；任何欄位變更須經 Lead 更新契約後同步三份 spec。

---

## 8. Out of scope（再次強調）
- ❌ **runtime embedding 呼叫 / runtime Milvus 連線**：**第 2、3 次強調**——審查時零 embedding、零向量庫連線，全部 build-time 預算、runtime 查表（查表屬 p3-02 的 `mapping.py`）。本 spec 的 `EmbeddingClient`/`RegulationStore` 只被 `scripts/build_regulation_index.py` 使用。
- ❌ **canonical map 抽取（build ②）＋ `--eval`（build ④）**：**第 2 次強調**——屬 `p3-02-canonical-map`。本 spec 只定義 ①（索引）③（`--dry-run`）與給 ②④ 的可組合 seam。
- ❌ **F01 任何比對**：Phase 4+，本 spec 不涉及。
- ❌ F03/F02 check 邏輯、engine 整合、`refs.py`/`mapping.py`/`config.py`/RAG checks：屬 p3-02/p3-03。

---

## 9. 參考 / 提請 Lead 裁決

**參考程式**：
- `src/govcheck/llm/client.py`（`ChatClient` — `EmbeddingClient` 鏡射對象）。
- `src/govcheck/llm/config.py`（`load_llm_config` — env 覆寫 + 機密只走 env 慣例）。
- `src/govcheck/checks/llm/f03_evidence.py`（批次/降級/彙整表範式；本 spec 的 build 批次每批 32 沿用「分批」精神）。
- `scripts/extract_f02_scoring.py`（離線 build 腳本範式：只讀 `data/original`、輸出到指定路徑、`if __name__=="__main__"`）。
- `tests/conftest.py`（`@pytest.fixture` 隔離 log、`skip if not exists` 慣例 — AC-18 沿用）。

**Lead 裁決紀錄（S1 四項疑義，均已裁決並反映於本文）**：
1. **build script seam — 核准，函式名鎖定**：`build_index()`（①）、`print_chunk_tree()`（③ `--dry-run`）、`iter_regulation_chunks()`（供 build ② 消費）；p3-02 入口 `build_retrieval_map()`（②）、`run_eval()`（④）；單一 `main()` CLI 由 3b T2 統一擁有。已落 §4.6、§7。
2. **投影修訂 — 裁決必改（與 p3-02 有實際衝突）**：collection 增 `order` INT64 scalar 欄（七欄）；`chunk_seq` 維持編入 id、`reg_title` 維持不落欄。`RegulationStore` 補 `lookup(reg_code, section_path_prefix) -> list[dict]`（scalar filter、依 `order` 排序、prefix 粗篩，細篩歸 p3-02 refs）。已落 §3-D5、§4.5，AC-9 加 lookup 驗證路徑。
3. **VARCHAR 長度語意 — 不改 spec 主體**：§5 加註「實作（3b T2）必須確認 pymilvus max_length 語意（bytes vs chars），寬裕餘量設定（text 建議 8192、id 256），AC 不綁定具體長度值」。§4.5 投影表已移除具體長度數字。
4. **句號硬切 property-based — 核准**：AC-6 維持 property-based，不補 golden。

**與計畫的一致性**：本 spec 未發現與 `hi-wondrous-crown.md` 的實質矛盾；上列 1–4 為計畫未明述、經 Lead 裁決補完之細節（`order` 欄為對計畫「collection 六欄」描述的**經核准偏離**，理由如上）。
