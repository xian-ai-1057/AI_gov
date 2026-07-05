"""F03 上線檢核表模型。

- F03Identity：Phase 2 最小識別欄（System Owner，B1）供跨表一致性比對。
- F03Checklist / F03ChecklistItem：Phase 3 完整檢核項，含兩段佐證欄（提案規劃 I / 上線 J）
  供規則式缺漏檢查與 LLM 佐證判讀使用。兩者並存、互不影響既有解析。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


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


class F03Checklist(BaseModel):
    """F03「檢核表」完整解析結果。"""

    subject: str | None = None  # 取 B1 系統擁有者（F03 無系統名稱欄）
    items: list[F03ChecklistItem] = Field(default_factory=list)
    sheet_present: bool = True
