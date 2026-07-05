"""p3-01-regulation-index 契約型別（Pydantic v2）。

本檔為 SDD 契約鏡射：Phase 3b 由 T1 據此在 `src/govcheck/rag/models.py`
建立同名型別。型別名由 Lead 鎖定，**逐字使用、不得改名**。

下游消費者：
- p3-02-canonical-map：消費 RegulationChunk（餵 EmbeddingClient / RegulationStore）、IndexMeta。
- p3-03-rag-checks：不直接消費本檔型別（runtime 走 retrieval_map，屬 p3-02）。

慣例：
- model_config = ConfigDict(extra="forbid")（拒絕未知欄位，schema drift 早爆）。
- str | None（非 Optional[str]）、Field(default_factory=...)、不使用 v1 `class Config`。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RegulationChunk(BaseModel):
    """單一法規切塊。chunker 產出的最小單位；build 時餵 embedding 與寫入 Milvus。

    section_path 正準形式：
      - 條文式（R01）：`第一條`
      - 章節式（R02–R07）：`五/(二)/2`（L1 中文數字 / (L2 中文數字) / L3 阿拉伯數字）

    text 已含 breadcrumb 前綴（見 spec §4.3），故單一 chunk 即可獨立檢索與引用。
    """

    model_config = ConfigDict(extra="forbid")

    reg_code: str = Field(description="法規代碼，如 R01–R07")
    reg_title: str = Field(description="法規全名（辦法名稱），用於 breadcrumb")
    section_path: str = Field(description="正準章節路徑，如 第一條 或 五/(二)/2")
    title: str = Field(description="本 chunk 葉節點之標題文字（條文式=條號；章節式=最深具標題節點之標題）")
    text: str = Field(description="含 breadcrumb 前綴的 chunk 全文（breadcrumb + 內文）")
    chunk_seq: int = Field(description="同一 section_path 內的序號；句號硬切時遞增，其餘為 0")
    order: int = Field(description="該法規內 chunk 的 0-based 文件順序（全域排序用）")

    @property
    def chunk_id(self) -> str:
        """Milvus 決定性主鍵：`{reg_code}:{section_path}:{chunk_seq}`。"""
        return f"{self.reg_code}:{self.section_path}:{self.chunk_seq}"


class IndexMeta(BaseModel):
    """向量索引 sidecar meta（`data/milvus/index_meta.json`）。

    RegulationStore.open() 以此核對 embedding_model / embedding_dim，
    不符即拒開（避免用錯模型或維度的索引做檢索）。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(description="索引 schema 版本；破壞性變更時 +1")
    built_at: str = Field(description="建置時間 ISO 8601 字串（如 2026-07-03T10:00:00+08:00）")
    embedding_model: str = Field(description="建索引所用 embedding 模型名")
    embedding_dim: int = Field(description="向量維度（bge-m3=1024）")
    chunks_per_reg: dict[str, int] = Field(
        default_factory=dict, description="各法規代碼 → chunk 數（如 {'R03': 42}）"
    )
    source_sha256: dict[str, str] = Field(
        default_factory=dict, description="各來源 PDF 檔名 → sha256（可重現性稽核）"
    )
