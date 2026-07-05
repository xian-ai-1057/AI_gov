"""p3-02 canonical-map — contract schemas（Lead 鎖定型別名，3b 逐字鏡射到 src/govcheck/rag/models.py）。

本檔為「合約母本」：
- 型別名與欄位由 Lead 鎖定，實作階段（Phase 3b）必須逐字對接，不得改名。
- 刻意「不 import govcheck」，使 contracts/fixtures/ 能被 spec 測試獨立 model_validate，
  在實作尚未落地前即可驗證 fixture 與合約一致。
- Pydantic v2 慣例：ConfigDict(extra="forbid")、str | None、Field(default_factory=...)。

上游（p3-01，S1 撰寫，型別名同樣鎖定）：RegulationChunk / IndexMeta / EmbeddingClient /
RegulationStore。本檔僅 runtime/build 側消費 RegulationStore.lookup 的結果，故不重定義上游型別。

註：本合約母本刻意「不」用 `from __future__ import annotations`，使任何載入方式（含 importlib
    以合成模組名載入）都能解析 forward ref、獨立 model_validate。3b 鏡射到 rag/models.py 時可依
    repo 慣例自行加回 future import（屆時為套件正常 import，無此顧慮）。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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


# 現行合約版本；loader 只接受相符值，其餘一律拒開（見 spec §4.4 / AC）。
SCHEMA_VERSION = 1
