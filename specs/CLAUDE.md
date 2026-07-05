# specs/ 開發規約 — Phase 3 Spec Team

> 本檔自動載入 specs/ 目錄的 AI teammate context。遵守以下原則，確保跨 spec 介面穩定、測試守門、隱私零洩漏。

---

## SDD（Spec-Driven Development）原則

- **Spec 為單一事實源**：發現 spec 描述不清或漏洞，**立即回報 Lead 修改 spec**，不自行解讀或調整實作。
- **Contracts 鎖死介面**：`contracts/` 目錄內 Pydantic schema + golden fixtures 是跨 spec 的唯一契約；修改須 Lead 核准。
- **測試 = 驗收**：pytest 全綠 ✅ = spec 完成並驗證；紅燈或 skip 絕不收工。

---

## 檔案隔離與協作

- **一檔一人**：兩位 teammate 不可同時編輯同一檔案；如需變更他人負責的檔案，**須提報 Lead 重新分配**。
- **測試隔離**：既有測試檔不修改；新 feature 的測試開新檔（例：`tests/test_p3_01_*.py`），命名含 spec 代號。
- **溝通機制**：跨 spec 型別引用 → 在 Lead 的協調下修改 contracts，確保雙邊同步。

---

## Pydantic v2 慣例

```python
from pydantic import BaseModel, Field, ConfigDict
from enum import StrEnum

class MyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")  # 禁止額外欄位
    
    field_name: str | None = None              # 用 | None，不用 Optional
    items: list[str] = Field(default_factory=list)  # 動態列表用 default_factory
    
class Status(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
```

- Field 驗證：用 `model_validate()` 拋錯；`.model_dump(mode='python')` 序列化。

---

## Fixture 紀律

- **Synthetic Only**：所有 fixture 模仿資料結構，**絕不**包含真實治理辦法條文、官方模板題目、使用者實名、單位名等。
- **示例格式**：每個主要 DTO 至少提供 1 個 example；命名：`{model}_example.json`（最小樣本 `{model}_minimal.json`）。
- **CI 守門**：每份 spec 的測試應包含 fixture 對 schema 的 `model_validate` 驗證，確保 fixture 對得上 schema。
- **路徑約定**：contract fixtures 存放於各 spec 的 `specs/p3-0X-*/contracts/fixtures/`；3b 實作測試置於 `tests/`（新檔，不動既有測試檔）。

---

## 隱私紅線（重複 root CLAUDE.md）

### 禁止項目
- ❌ **寫入 `data/original/`**：該目錄唯讀，存官方模板與法規；所有產物入 `data/rag/` 或 `data/milvus/`（已 gitignore）。
- ❌ **硬寫法規內容/模板題文**：R01–R07 條款、F01/F02/F03 欄位定義不得落任何會入 git 的檔案（含 spec doc 本身）；參考內容只由 config 與 loader 在執行時動態讀取。
- ❌ **Log 記錄敏感內容**：禁記原始檔內容、提案者姓名、提案單位、F03 佐證全文、LLM prompt/response 全文、api_key、Authorization。
  - ✅ 只准記：系統代碼、單位代碼（匿名化）、Finding 代碼、計數、耗時、例外類型。

---

## AC（Acceptance Criteria）撰寫檢查清單

完成 spec 前，每條 AC 都須通過以下檢驗：

1. **可測性**：是否能用一個 pytest 驗證？引用具體 fixture 路徑。
2. **邊界與失敗**：包含邊界情況（空值、超大量、逾時）與負向測試（格式錯、權限不足）。
3. **無重複**：與同 spec 或其他 spec 的 AC 無重疊；相似 AC 應合併或明確區分。
4. **Contracts 對齊**：引用的模型、欄位、enum 必須在 `contracts/` 已定義；型別名逐字 copy，不改寫。

---

## 跨 Spec 介面規則

- **型別引用**：若 A spec 需使用 B spec 的 Pydantic model，在 spec §7「與其他 spec 的介面」表中逐字列出型別名與來源 spec；3b 實作時 schema 落地於 `src/govcheck/rag/models.py` 等模組，spec contracts 為其藍本。
- **版本鎖定**：修改已發佈的 contract model 須通知所有依賴 spec；若破壞相容，bump major version，並在文件說明遷移方式。
- **測試涵蓋**：跨 spec 的資料流轉應有 integration test，mock 化的 spec 不再依賴真實下游實作。

---

## 推送與收工

- 推送前確認：所有 test 綠燈、no linting error（ruff check）、fixture 無洩漏個資。
- PR commit 訊息格式：`spec(p3-0X): 核心描述` + 列舉變更範圍與關鍵 fixture。
- 每次收工前跑一次 `uv run pytest --co -q` 確保測試探測無誤；CI 若紅必修復後重新提交。
