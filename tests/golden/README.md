# tests/golden/ — Phase 2 基準 Golden Snapshot

## 目的

在 Phase 3b 修改 engine（`review/engine.py`）之前，先用**未修改的現行 engine** 對 synthetic
送件包執行審查，把結果固化為 `phase2_baseline.json`。

Phase 3b 新增 `_run_rag_checks` 整合後，`rag.enabled=false`（預設值）時 engine 的輸出
必須與此快照逐字節相同（AC-11 回歸保護）。

## 檔案說明

| 檔案 | 說明 |
|---|---|
| `generate_snapshot.py` | 快照產生腳本；可重跑覆蓋 |
| `phase2_baseline.json` | Phase 2 基準快照（schema_version=phase2_baseline_v1） |
| `README.md` | 本說明文件 |

## 快照內容

快照含兩組送件包：

| label | 說明 | 預期 findings |
|---|---|---|
| `full_f01_f02_f03` | F01+F02+F03 合規低風險基線 | `SUBMISSION.OK` (1 筆 INFO) |
| `f02_only` | 僅 F02、缺 F01/F03 | `DOC.MISSING_F01` + `DOC.MISSING_F03` (各 ERROR) |

## 重新產生指令

```bash
uv run python tests/golden/generate_snapshot.py
```

腳本會：
1. 以 `tests/fixture_builder.py` 建立 synthetic 送件包（不依賴官方模板內容）。
2. 呼叫 `review_submission(enable_llm=False)`。
3. 以 `model_dump(mode="json") + json.dumps(sort_keys=True)` 做決定性序列化。
4. 若快照已存在，先比對 SHA-256 確認決定性，再覆蓋。

## 決定性保證

- **輸入固定**：synthetic 送件包由固定答案集（`_BASELINE_ANSWERS`，與 `test_f02_rules.py::BASELINE` 相同）產生。
- **序列化固定**：`json.dumps(sort_keys=True)` 確保物件鍵順序穩定。
- **findings 順序**：維持 engine 的 `sort(severity_order)` 結果（Python stable sort 保序）。
- **無動態成分**：`ReviewReport` 不含 timestamp；排除任何亂數或執行時間欄位。
- **已驗證**：兩次連跑 SHA-256 完全相同（`aa324433d76dc088…`）。

## 隱私合規

快照**不含**：
- 官方模板題文（F01/F02/F03 欄位定義）
- 法規名稱（R01–R07 各治理辦法之正式名稱）
- 任何 PII（真實姓名、單位名）

快照**只含**：
- 結構性文字標籤（如「F01 AI系統資訊表」、「送件包」）
- Finding 代碼、嚴重度、引導性說明訊息
- 合成的示範資料（「智能客服小幫手」、「李大華」）

## AC-11 回歸測試說明

Phase 3b 完成後，回歸測試需驗證：

```python
# 偽碼示意
with tempfile.TemporaryDirectory() as td:
    files = build_full_submission(Path(td))
    report = review_submission(files, enable_llm=False)
    snapshot = json.loads(SNAPSHOT_PATH.read_text())
    baseline = snapshot["submissions"][0]["report"]  # full_f01_f02_f03
    assert report.model_dump(mode="json") == baseline
```

只要 `rag.enabled=false`（預設），`_run_rag_checks` 回 `[]`，engine 輸出不變，斷言成立。
