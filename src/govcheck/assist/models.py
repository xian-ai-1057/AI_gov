"""協填結果的穩定契約（API 與報告建構器都依此，勿隨意改欄位）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from govcheck.models import Citation, DocumentArtifact, Finding, PromptRunRef, UsageSummary


class Suggestion(BaseModel):
    """單一欄位的填寫建議。

    covered=False 代表佐證文件沒有支持這個欄位——此時 suggestion 一律 None（不憑空生成），
    citations 只是「可能相關的候選出處」供使用者自行參考。
    """

    form: Literal["f01", "f02", "f03"]
    field_id: str
    label: str
    cell: str
    kind: Literal["text", "yes_no"]
    covered: bool
    suggestion: str | None = None
    confidence: float = 0.0
    citations: list[Citation] = Field(default_factory=list)
    rationale: str = ""


class AssistResult(BaseModel):
    assist_id: str
    forms: list[str]
    suggestions: list[Suggestion]
    document_statuses: list[DocumentArtifact]
    notices: list[Finding]
    usage: UsageSummary
    prompt_refs: list[PromptRunRef]
    # llm：模型已產生建議；retrieval_only：只有候選出處（LLM 關閉／prompt 缺檔／模型失敗）；
    # unavailable：欄位目錄缺檔，沒有東西可以填。
    mode: Literal["llm", "retrieval_only", "unavailable"]
