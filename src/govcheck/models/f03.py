"""F03 上線檢核表模型。

- F03Identity：Phase 2 最小識別欄（System Owner，B1）供跨表一致性比對。
- F03Checklist / F03ChecklistItem：Phase 3 完整檢核項，含兩段佐證欄（提案規劃 I / 上線 J）
  供規則式缺漏檢查與 LLM 佐證判讀使用。兩者並存、互不影響既有解析。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from govcheck.models.evidence import WorkflowStage

# 不適用理由帶進報告供治理人員判斷「主張是否成立」；截斷避免整段長文灌進 API payload。
_REASON_MAX_CHARS = 200
# 檢查項目與佐證說明原文帶進報告供人工對照 AI 判讀；同樣截斷，避免長文灌爆 payload。
_TEXT_MAX_CHARS = 600


def _clip(text: str | None, limit: int = _TEXT_MAX_CHARS) -> str | None:
    """去頭尾空白後截斷；空字串與 None 一律回 None（報告端才好判斷「有沒有填」）。"""
    stripped = (text or "").strip()
    return stripped[:limit] or None


class F03Identity(BaseModel):
    """F03 識別欄（最小解析）。"""

    system_owner: str | None = None  # B1（標籤 A1="系統擁有者"）
    system_name: str | None = None   # Phase 2 無可靠來源；保留給 Phase 3
    sheet_present: bool = True


class F03ChecklistItem(BaseModel):
    """F03「檢核表」單一檢核項（資料列）。"""

    item_id: str                          # 項次 A，如 "1-1"、"4-5"
    lifecycle: str | None = None          # 生命週期 B（合併儲存格，往下沿用群組值）
    topic: str | None = None              # 管理議題 C
    description: str | None = None        # 檢查項目 D（LLM 判讀的上下文）
    check_state: str | None = None        # 是否完成檢核：是 / 否 / 不適用（由 F/G/H 推得）
    evidence_proposal: str | None = None  # I 前項佐證說明(提案規劃階段)
    evidence_golive: str | None = None    # J 前項佐證說明(上線階段;列出與提案差異)
    regulation_ref_raw: str | None = None  # L 規範參考（parse 存原始字串、不解析；供 RAG 判 TEMPLATE_REF_MODIFIED）

    @property
    def is_done(self) -> bool:
        """勾選「是」（已完成檢核）。"""
        return self.check_state == "是"

    @property
    def loc(self) -> str:
        """Finding 定位字串（題號＋管理議題），規則與 LLM 檢查共用。"""
        return f"項次 {self.item_id}" + (f"（{self.topic}）" if self.topic else "")

    def evidence_for_stage(self, stage: WorkflowStage) -> str | None:
        """該階段可供判讀的佐證文字；空字串與 None 一律視為無。

        語意判讀的送審條件與總覽表名冊必須用同一條件判斷，否則會出現
        「名冊說可判讀、卻沒有任何 assessment」的矛盾列。
        """
        if stage is WorkflowStage.PROPOSAL:
            return self.evidence_proposal or None
        return self.evidence_proposal or self.evidence_golive or None

    def golive_diff_for_stage(self, stage: WorkflowStage) -> str | None:
        """上線佐證與提案佐證的差異（規則式文字比對，非語意判讀）。

        提案階段不看 J 欄，故回 None。比對前把所有空白去掉，避免排版差異被誤判成內容差異。
        """
        if stage is WorkflowStage.PROPOSAL:
            return None
        golive = "".join((self.evidence_golive or "").split())
        if not golive:
            return "missing"
        return "same" if golive == "".join((self.evidence_proposal or "").split()) else "changed"

    def coverage_for_stage(self, stage: WorkflowStage) -> F03ItemCoverage:
        """本項在該階段的涵蓋狀態（供報告列出**每一個**檢核項）。"""
        # 原文一律帶進報告供人工對照 AI 判讀；提案階段不帶 J 欄，與 evidence_for_stage 一致
        # ——該階段的 J 欄尚未進入審查範圍，帶出來會讓人以為 AI 看過了。
        texts = {
            "description": _clip(self.description),
            "evidence_proposal_text": _clip(self.evidence_proposal),
            "evidence_golive_text": (
                None if stage is WorkflowStage.PROPOSAL else _clip(self.evidence_golive)
            ),
            "golive_diff": self.golive_diff_for_stage(stage),
        }
        if self.check_state == "不適用":
            reason = (self.evidence_proposal or "").strip()
            return F03ItemCoverage(
                item_id=self.item_id, topic=self.topic, check_state=self.check_state,
                coverage="not_applicable", reason=reason[:_REASON_MAX_CHARS] or None,
                **texts,
            )
        coverage = "assessable" if self.evidence_for_stage(stage) else "no_evidence"
        return F03ItemCoverage(
            item_id=self.item_id, topic=self.topic,
            check_state=self.check_state, coverage=coverage,
            **texts,
        )


class F03ItemCoverage(BaseModel):
    """檢核項在本次審查中的涵蓋狀態。

    語意判讀只吃得下「非不適用且有填佐證」的項次（見 checks/llm/f03_assessment._targets），
    其餘項次既無 assessment 也未必有 finding。若不隨名冊一併帶進報告，整列會從總覽表消失，
    治理人員無從得知提案單位主張了哪些項目不適用、又有哪些項目根本沒填。
    """

    item_id: str
    topic: str | None = None
    check_state: str | None = None                # 是 / 否 / 不適用 / None（未勾選）
    coverage: Literal["assessable", "not_applicable", "no_evidence"]
    reason: str | None = None                     # 不適用理由（提案單位填在佐證欄時）
    # 以下四欄供治理人員「對照原文看 AI 判讀」；皆為提案單位填寫內容，截斷後帶進報告。
    description: str | None = None                # D 檢查項目
    evidence_proposal_text: str | None = None     # I 前項佐證說明(提案規劃階段)
    evidence_golive_text: str | None = None       # J 前項佐證說明(上線階段)；提案階段為 None
    # 上線佐證相對提案佐證的差異；提案階段為 None（該階段不看 J 欄）。
    golive_diff: Literal["missing", "same", "changed"] | None = None


class F03Checklist(BaseModel):
    """F03「檢核表」完整解析結果。"""

    subject: str | None = None  # 取 B1 系統擁有者（F03 無系統名稱欄）
    items: list[F03ChecklistItem] = Field(default_factory=list)
    sheet_present: bool = True

    def roster(self, stage: WorkflowStage) -> list[F03ItemCoverage]:
        """全部檢核項的涵蓋狀態名冊，順序同檢核表。"""
        return [item.coverage_for_stage(stage) for item in self.items]
