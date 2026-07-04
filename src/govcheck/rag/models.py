"""Phase 3 RAG Pydantic 型別集散地（T1 擁有；T2/T3 只 import 不改）。

型別由三份 spec 契約逐字鏡射（型別名/欄位 Lead 鎖定，不得改名）：
- specs/p3-01-regulation-index/contracts/schemas.py：RegulationChunk / IndexMeta
- specs/p3-02-canonical-map/contracts/schemas.py：RegulationRef / RetrievedSection /
  F03ItemRetrieval / F02QuestionRetrieval / RetrievalMap / SCHEMA_VERSION
- specs/p3-03-rag-checks/contracts/schemas.py：RagConfig / F03RagVerdict / F03RagBatchResponse

Pydantic v2 慣例：ConfigDict(extra="forbid")、str | None、Field(default_factory=...)。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ─────────────────────────────────────────────────────────────────────────────
# p3-01：語料索引（build-time）
# ─────────────────────────────────────────────────────────────────────────────


class RegulationChunk(BaseModel):
    """單一法規切塊。chunker 產出的最小單位；build 時餵 embedding 與寫入 Milvus。

    section_path 正準形式：
      - 條文式（R01）：`第一條`
      - 章節式（R02–R07）：`五/(二)/2`（L1 中文數字 / (L2 中文數字) / L3 阿拉伯數字）

    text 已含 breadcrumb 前綴（見 p3-01 spec §4.2），故單一 chunk 即可獨立檢索與引用。
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


# ─────────────────────────────────────────────────────────────────────────────
# p3-02：canonical 檢索映射
# ─────────────────────────────────────────────────────────────────────────────


class RegulationRef(BaseModel):
    """一條「規範參考」解析後的正準引用。

    section_path_prefix：正準 section_path 形式（如 "五/(二)/2"；R01 用 "第一條"）。
    空字串 "" 代表「整部規範」（refs 解析只認得 reg code、無法定位到章節時）。
    """

    model_config = ConfigDict(extra="forbid")

    reg_code: str                       # 如 "R03"
    section_path_prefix: str = ""       # 正準 section_path；"" = 整部規範


class RetrievedSection(BaseModel):
    """檢索映射中的一段命中條文（build 時預算，runtime 只讀）。

    origin：curated = 來自官方模板 L 欄「規範參考」精準定位；semantic = embedding top-k 補充。
    score：semantic 的相似度分數（COSINE）；curated 命中無分數 → None。
    excerpt：條文摘錄（受 max_excerpt_chars 上限）；此檔含法規內容 → 產物必落 gitignore 區。
    """

    model_config = ConfigDict(extra="forbid")

    reg_code: str
    section_path: str                   # 正準 section_path（如 "五/(二)/2"、"第一條"）
    title: str
    excerpt: str
    score: float | None = None          # curated → None；semantic → COSINE 分數
    origin: Literal["curated", "semantic"]


class F03ItemRetrieval(BaseModel):
    """單一 F03 檢核項的 canonical 檢索結果。

    canonical_* 皆來自官方附件三模板（build 時抽取），非上傳檔；
    上傳檔與此不符時由 p3-03 判 F03.TEMPLATE_REF_MODIFIED，判讀一律以此 canonical 為準。
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str
    canonical_topic: str | None = None
    canonical_description: str | None = None
    canonical_ref_raw: str | None = None            # L 欄原始字串（未解析）
    refs: list[RegulationRef] = Field(default_factory=list)
    sections: list[RetrievedSection] = Field(default_factory=list)


class F02QuestionRetrieval(BaseModel):
    """單一 F02 題的語意檢索結果（無 curated；F02 模板無「規範參考」欄）。"""

    model_config = ConfigDict(extra="forbid")

    qid: str
    sections: list[RetrievedSection] = Field(default_factory=list)


class RetrievalMap(BaseModel):
    """runtime 唯一輸入：data/rag/retrieval_map.json 反序列化結果。

    schema_version：loader 以此擋「舊版產物 / 不相容格式」→ 明確例外（engine 攔截降級）。
    built_at：ISO8601 字串；embedding_model：build 時使用的 embedding 模型名（供追溯）。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    built_at: str                                   # ISO8601
    embedding_model: str
    f03_items: dict[str, F03ItemRetrieval] = Field(default_factory=dict)
    f02_questions: dict[str, F02QuestionRetrieval] = Field(default_factory=dict)


# 現行合約版本；loader 只接受相符值，其餘一律拒開（見 p3-02 spec §4.4 / AC）。
SCHEMA_VERSION = 1


# ─────────────────────────────────────────────────────────────────────────────
# p3-03：RAG checks（runtime）
# ─────────────────────────────────────────────────────────────────────────────


class RagConfig(BaseModel):
    """RAG 功能設定（runtime + build-time 鍵）。

    「實際啟用」由 p3-03 engine 以 `enable_llm AND enabled` 決定（見 p3-03 spec §4.4）。
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
    白名單外的值由 f03_rag 一律降為 "undetermined"（見 p3-03 spec §4.2）。
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
