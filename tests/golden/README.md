# tests/golden/ — 規則層基準 Golden Snapshot

## 目的

用 synthetic 送件包跑**關閉 LLM** 的 engine，把規則層輸出固化為 `rules_baseline.json`。
之後任何不打算改變規則層結論的改動（新增語意判讀能力、調整報告呈現、抽換佐證解析後端），
都必須讓 engine 的輸出與此快照逐位元組相同。

快照只涵蓋規則層：`item_assessments` 與 `checklist_roster` 在序列化時 `exclude=True`，
所以新增呈現欄位不會擾動它。反過來說，**任何新增的規則檢查都會改變 `findings`**，
那時才需要重產快照。

## 檔案說明

| 檔案 | 說明 |
|---|---|
| `generate_snapshot.py` | 快照產生腳本；可重跑覆蓋 |
| `rules_baseline.json` | 規則層基準快照 |
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

## 回歸測試說明

回歸測試驗證：

```python
# 偽碼示意
with tempfile.TemporaryDirectory() as td:
    files = build_full_submission(Path(td))
    report = review_submission(files, enable_llm=False)
    snapshot = json.loads(SNAPSHOT_PATH.read_text())
    baseline = snapshot["submissions"][0]["report"]  # full_f01_f02_f03
    assert report.model_dump(mode="json") == baseline
```

快照不含 `parser_name`，synthetic 送件包也沒有佐證檔，因此佐證解析後端的更換動不到它 ——
若它意外變動，代表有非預期的行為改變，應先查清楚再決定要不要重產。
