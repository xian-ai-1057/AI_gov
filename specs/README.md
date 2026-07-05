# Phase 3 — RAG 法規比對 Spec 總覽

## 階段定位

**Phase 3:build-time RAG(R01–R07 法規向量索引 + canonical 檢索映射)→ 審查時 F03 LLM 符合性判讀 + F02 規則式義務條文提示。**

檢索在建置階段以官方模板 canonical 文字預計算完成;審查時零 embedding 呼叫,只查映射檔。
引用精確到條號/章節路徑(如 `R01 第一條`、`R03 五、(二) 2`)。AI 初判、人工覆核,資料全程地端。

---

## 三份 Spec 依賴地圖

```
p3-01-regulation-index  →  p3-02-canonical-map  →  p3-03-rag-checks
(語料/索引,源頭)          (refs/映射預計算)        (checks/engine 整合)
```

### 1. p3-01-regulation-index — 法規語料索引建置

- `rag/pdf_text.py`(PDF→乾淨文字行)、`rag/chunker.py`(條文式 R01 / 章節式 R02–R07 切塊)
- `rag/embedding.py` EmbeddingClient(**OpenAI 相容 `/embeddings` 端點,requests 實作,build-time 專用**)
- `rag/store.py` RegulationStore(Milvus Lite 包裝)+ `scripts/build_regulation_index.py` ①③(--dry-run)
- 契約:`RegulationChunk`、`IndexMeta`
- 產物:`data/milvus/governance.db` + `index_meta.json`(gitignore 區)

### 2. p3-02-canonical-map — 規範參考解析與 canonical 映射

- `rag/refs.py`(L 欄「規範參考」字串正規化 + section_path prefix 比對)
- build ②④:從**官方模板**抽 canonical(F03 20 項 + F02 題文)→ 預算檢索 → `data/rag/retrieval_map.json`(gitignore 區);`--eval` recall@k 校準 threshold
- `rag/mapping.py` RetrievalMap loader(**runtime 唯一入口,零 embedding、零 Milvus**)
- `parsers/f03_parser.py` L 欄擴充 → `F03ChecklistItem.regulation_ref_raw`
- 契約:`RegulationRef`、`RetrievedSection`、`F03ItemRetrieval`、`F02QuestionRetrieval`、`RetrievalMap`

### 3. p3-03-rag-checks — 判讀 checks 與 engine 整合

- `checks/llm/f03_rag.py`:canonical 條文摘錄 + 兩段佐證 → LLM 判 covered/gap/undetermined(鏡射 f03_evidence 降級範式;prompt 預算防護;TEMPLATE_REF_MODIFIED 比對)
- `checks/rule/f02_reg_refs.py`:觸發題(`answers[qid]==score_on`)→ 查表 → 模板化 INFO(**純規則,零 LLM**)
- `rag/config.py` + `review/engine.py` `_run_rag_checks`(插於 `if has_f03_checklist:` 區塊之外、cross_consistency 之前)
- 契約:`RagConfig`、`F03RagVerdict`、`F03RagBatchResponse`
- 回歸硬條件:`rag.enabled=false`(預設)→ engine 輸出與 golden snapshot 完全一致

---

## 明確 Out of scope(全 Phase)

- **F01 的法規比對**(Phase 4+)
- **runtime embedding / runtime Milvus**(檢索已於 build 時預計算)
- **F02 上 LLM**(無佐證即無判讀任務;逐字引用優於轉述)
- **sentence-transformers / openai SDK**(統一 requests + OpenAI 相容端點)

---

## 進度狀態

| Spec | 狀態 | AC 數 |
|------|------|------|
| p3-01 | ✅ 3a 完成(Lead 審查通過,含 order 欄/lookup 簽章修訂) | 18 |
| p3-02 | ✅ 3a 完成(Lead 審查通過,含 lookup 對齊/gitignore 前置修訂) | 19 |
| p3-03 | ✅ 3a 完成(Lead 審查通過) | 19 |

**Phase 3a**:✅ 完成——spec + contracts + synthetic fixtures 交付,Lead 一致性驗證全過(8 項 fixture round-trip、跨 spec 簽章逐字比對)
**Phase 3b**(待放行):使用者人工審核 spec 後,impl-team(T1–T5)實作

---

## 開發者指引

- 契約 schema 與 fixtures 位於各 spec 的 `contracts/`、`contracts/fixtures/`(fixtures 一律 synthetic)
- 遵守 `specs/CLAUDE.md` 協作守則;跨 spec 型別名逐字引用
- 3b 實作的測試開新檔(`tests/test_rag_*.py` 等),既有測試檔不修改
