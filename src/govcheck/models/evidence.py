"""佐證文件、F03 分項評估與審查執行 metadata 的穩定契約。"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class WorkflowStage(str, Enum):
    """治理流程只有兩段：提案送審、上線送審。

    上線階段＝提案內容已核可、系統實際要上線前的複審，因此除了提案內容外還要看
    上線階段佐證（F03 J 欄）與「上線後跟提案差了什麼」。
    """

    PROPOSAL = "proposal"
    GOLIVE = "golive"


class DocumentParseStatus(str, Enum):
    PARSED = "parsed"
    UNSUPPORTED = "unsupported"
    UNREADABLE = "unreadable"
    PARSER_UNAVAILABLE = "parser_unavailable"


class EvidenceStatus(str, Enum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    NOT_FOUND = "not_found"
    UNREADABLE = "unreadable"


class ConflictStatus(str, Enum):
    CONSISTENT = "consistent"
    CONTRADICTED = "contradicted"
    CONFLICTING_SOURCES = "conflicting_sources"
    UNKNOWN = "unknown"


class DecisionStatus(str, Enum):
    APPROVED = "approved"
    CHANGES_REQUIRED = "changes_required"
    MANUAL_REVIEW = "manual_review"
    INCOMPLETE = "incomplete"


class SourceUnit(BaseModel):
    """一段可回溯至原文件位置的解析內容。"""

    unit_id: str
    content: str
    locator_type: str = "page"
    locator_value: str
    attachment_path: str | None = None


class DocumentArtifact(BaseModel):
    """佐證文件解析後的標準化產物；不保存顯示用原始檔名。"""

    document_id: str
    filename_hash: str
    media_type: str
    parser_name: str = "ts_parser"
    parser_source_commit: str = "snapshot-unversioned"
    parser_config_hash: str
    status: DocumentParseStatus
    source_units: list[SourceUnit] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Citation(BaseModel):
    document_id: str
    unit_id: str
    locator_type: str
    locator_value: str
    quote: str = ""


class F03ItemAssessment(BaseModel):
    item_id: str
    profile: str
    completeness: Literal["complete", "incomplete", "undetermined"] = "undetermined"
    reasonableness: Literal[
        "sufficient", "reasonable", "vague", "unreasonable", "undetermined"
    ] = "undetermined"
    evidence_status: EvidenceStatus = EvidenceStatus.NOT_FOUND
    conflict_status: ConflictStatus = ConflictStatus.UNKNOWN
    # 佐證是否涵蓋該檢核項對應的法規條文要求。undetermined 也用於「本項無條文對照」
    # （對照表缺檔或該項未映射）——沒有基準就不宣稱符合，也不宣稱有缺口。
    regulation_gap: Literal["covered", "gap", "undetermined"] = "undetermined"
    # 條文引用顯示字串，一律由對照表的 canonical section_path 推導，不採信模型自述的條號。
    regulation_refs: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    # False = 模型從未對此項做出判讀（端點失敗/回應缺漏/Prompt 未就緒）。
    # 此時 evidence_status 等欄位只是佔位預設值，不得當成實質合規結論。
    assessed: bool = True
    citations: list[Citation] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    prompt_id: str | None = None
    prompt_version: str | None = None


class UsageSummary(BaseModel):
    # parser_*_calls 計的是「需要視覺模型的解析需求次數」，含端點未啟用/未核准而
    # fail closed、實際未外送的情形（見 parsers/supporting.py 與其測試）。
    # 要判斷是否真的有外送，請看 DocumentArtifact.status 是否為 PARSER_UNAVAILABLE。
    parser_ocr_calls: int = 0
    parser_vlm_calls: int = 0
    evidence_embedding_calls: int = 0
    review_llm_primary_calls: int = 0
    review_llm_escalation_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hits: int = 0
    latency_ms: int = 0

    def add(self, other: UsageSummary) -> None:
        for name in type(self).model_fields:
            setattr(self, name, getattr(self, name) + getattr(other, name))


class PromptRunRef(BaseModel):
    prompt_id: str
    prompt_version: str
    content_hash: str
    schema_version: str


class ReviewRunMeta(BaseModel):
    workflow_stage: WorkflowStage = WorkflowStage.PROPOSAL
    prompt_refs: list[PromptRunRef] = Field(default_factory=list)
    parser_source_commit: str | None = None
    policy_version: str = "f03-policy-v1"
    output_schema_version: str = "2"
    model_deployment: str | None = None
