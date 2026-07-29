"""審查結果的通用資料模型：Finding 與 ReviewReport。

所有檢查（規則式或未來的 LLM 判讀）都產出 Finding，engine 匯總成 ReviewReport。
這是 parser/checks/report 之間的共同契約，新增任何一種檢查都不需改動報告層。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from govcheck.models.evidence import (
    DecisionStatus,
    DocumentArtifact,
    F03ItemAssessment,
    ReviewRunMeta,
    UsageSummary,
)


class Severity(str, Enum):
    ERROR = "error"  # 明確違規 / 缺漏，必須處理
    WARN = "warn"    # 可能有問題，需人工確認
    INFO = "info"    # 提示 / 通過紀錄


class Finding(BaseModel):
    """單一審查發現。"""

    severity: Severity
    code: str                       # 規則代碼，如 "F02.SINGLE_CHOICE"
    title: str                      # 簡短標題
    message: str                    # 說明（給治理人員看）
    location: str | None = None     # 題號 / 欄位 / 分頁
    expected: str | None = None     # 期望值
    actual: str | None = None       # 實際值
    source: str = "rule"            # rule / llm
    needs_human: bool = True        # 一律需人工覆核


class ReviewReport(BaseModel):
    """一次審查的完整報告。"""

    form_type: str                          # "F02" 等
    subject: str | None = None              # 受審專案/服務名稱
    findings: list[Finding] = Field(default_factory=list)
    risk_score: float | None = None         # F02 固有風險百分比（overall 0–100）；無 F02 則 None
    risk_grade: str | None = None           # F02 固有風險分級：低/中/高；無 F02 則 None
    # exclude=True keeps the legacy ``model_dump``/golden contract stable. API v2 projects these
    # fields explicitly; v1 callers continue seeing the original response shape.
    decision_status: DecisionStatus = Field(default=DecisionStatus.MANUAL_REVIEW, exclude=True)
    item_assessments: list[F03ItemAssessment] = Field(default_factory=list, exclude=True)
    document_statuses: list[DocumentArtifact] = Field(default_factory=list, exclude=True)
    usage_summary: UsageSummary = Field(default_factory=UsageSummary, exclude=True)
    review_run_meta: ReviewRunMeta = Field(default_factory=ReviewRunMeta, exclude=True)
    banner: str = "⚠️ 本報告為 AI 初判草稿，需治理人員與三遵人工覆核，最終判定權不在 AI。"

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity is Severity.ERROR)

    @property
    def warn_count(self) -> int:
        return sum(1 for f in self.findings if f.severity is Severity.WARN)

    @property
    def passed(self) -> bool:
        """v1 相容欄位；新介面應使用 decision_status。"""
        return self.error_count == 0
