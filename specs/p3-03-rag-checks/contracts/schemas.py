"""p3-03 rag-checks — contract schemas（Lead 鎖定型別名，3b 逐字鏡射）。

- `RagConfig`：`rag/config.py::load_rag_config()` 的輸出形狀（YAML `rag:` 區段 + GOVCHECK_RAG_* env 覆寫）。
  **缺 yaml 區段時仍須有完整預設值**（不依賴 yaml 已更新）。機密（api_key）只走 env、不入本結構。
- `F03RagVerdict` / `F03RagBatchResponse`：`checks/llm/f03_rag.py` 解析 LLM 批次回應的形狀。

Pydantic v2 慣例：ConfigDict(extra="forbid")、str | None、Field(default_factory=...)。
刻意不 import govcheck、且不用 `from __future__ import annotations`，使 contracts/fixtures
可在任何載入方式下獨立 model_validate（3b 鏡射時可依 repo 慣例加回 future import）。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RagConfig(BaseModel):
    """RAG 功能設定（runtime + build-time 鍵）。

    「實際啟用」由 p3-03 engine 以 `enable_llm AND enabled` 決定（見 spec §4.4）。
    保守起步值（batch_size=2 / max_excerpt_chars=300 / timeout=120），e2e 實測後定版。
    """

    model_config = ConfigDict(extra="forbid")

    # --- runtime 鍵 ---
    enabled: bool = False
    mapping_path: str = "data/rag/retrieval_map.json"
    batch_size: int = 2
    max_sections_per_item: int = 3
    max_excerpt_chars: int = 300
    timeout: float = 120.0                 # RAG 判讀獨立 timeout（不沿用 chat 60s）
    max_items: int = 30

    # --- build-time 鍵 ---
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_dim: int = 1024
    milvus_uri: str | None = None
    top_k: int = 4
    score_threshold: float | None = None   # 由 build --eval 校準；未校準前為 None


class F03RagVerdict(BaseModel):
    """單一 F03 檢核項的符合性判讀結果。

    verdict：covered（佐證涵蓋 canonical 條文要求）/ gap（有缺口）/ undetermined（無法判定）。
    白名單外的值由 f03_rag 一律降為 "undetermined"（見 spec §4.2）。
    gap_refs：verdict=gap 時，指出缺口對應的條號字串（如 "R03 五、(二) 2"）；其餘可為空。
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str
    verdict: Literal["covered", "gap", "undetermined"]
    gap_refs: list[str] = Field(default_factory=list)
    reason: str = ""


class F03RagBatchResponse(BaseModel):
    """一批 F03 判讀的 LLM 回應（{"results": [F03RagVerdict, ...]}）。"""

    model_config = ConfigDict(extra="forbid")

    results: list[F03RagVerdict] = Field(default_factory=list)
