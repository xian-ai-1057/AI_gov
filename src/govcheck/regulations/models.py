"""法規對照表的 Pydantic 型別（runtime 唯讀）。

`data/rag/retrieval_map.json` 是**凍結的靜態資產**：由官方 R01–R07 PDF 與 F03 模板
離線建置一次，之後 runtime 只讀不寫。建置腳本封存於 `_Archive/rag_build/`，
法規改版時才取回重跑。

欄位名沿用建置時的契約，不得改名，否則既有 mapping 檔會驗證失敗。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RegulationRef(BaseModel):
    """一條「規範參考」解析後的正準引用。

    section_path_prefix：正準 section_path 形式（如 "五/(二)/2"；R01 用 "第一條"）。
    空字串 "" 代表「整部規範」（只認得 reg code、無法定位到章節時）。
    """

    model_config = ConfigDict(extra="forbid")

    reg_code: str                       # 如 "R03"
    section_path_prefix: str = ""       # 正準 section_path；"" = 整部規範


class RetrievedSection(BaseModel):
    """對照表中的一段命中條文。

    origin：curated = 來自官方模板 L 欄「規範參考」精準定位；semantic = 建置時 embedding top-k 補充。
    score：semantic 的相似度分數（COSINE）；curated 命中無分數 → None。
    excerpt：條文摘錄；此檔含法規內容 → 產物必落 gitignore 區。
    """

    model_config = ConfigDict(extra="forbid")

    reg_code: str
    section_path: str                   # 正準 section_path（如 "五/(二)/2"、"第一條"）
    title: str
    excerpt: str
    score: float | None = None          # curated → None；semantic → COSINE 分數
    origin: Literal["curated", "semantic"]


class F03ItemRetrieval(BaseModel):
    """單一 F03 檢核項的 canonical 條文對照。

    canonical_* 皆來自官方附件三模板（建置時抽取），非上傳檔；上傳檔與此不符時
    由 f03_assessment 判 F03.TEMPLATE_REF_MODIFIED，判讀一律以此 canonical 為準。
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str
    canonical_topic: str | None = None
    canonical_description: str | None = None
    canonical_ref_raw: str | None = None            # L 欄原始字串（未解析）
    refs: list[RegulationRef] = Field(default_factory=list)
    sections: list[RetrievedSection] = Field(default_factory=list)


class F02QuestionRetrieval(BaseModel):
    """單一 F02 題的條文對照（無 curated；F02 模板無「規範參考」欄）。"""

    model_config = ConfigDict(extra="forbid")

    qid: str
    sections: list[RetrievedSection] = Field(default_factory=list)


class RetrievalMap(BaseModel):
    """runtime 唯一輸入：`data/rag/retrieval_map.json` 反序列化結果。

    schema_version：loader 以此擋「舊版產物 / 不相容格式」→ 明確例外（呼叫端攔截降級）。
    built_at：ISO8601 字串；embedding_model：建置時使用的 embedding 模型名（供追溯）。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    built_at: str                                   # ISO8601
    embedding_model: str
    f03_items: dict[str, F03ItemRetrieval] = Field(default_factory=dict)
    f02_questions: dict[str, F02QuestionRetrieval] = Field(default_factory=dict)


# 現行合約版本；loader 只接受相符值，其餘一律拒開。
SCHEMA_VERSION = 1
