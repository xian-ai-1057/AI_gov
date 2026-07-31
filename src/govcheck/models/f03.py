"""F03 上線檢核表模型。

- F03Identity：Phase 2 最小識別欄（System Owner，B1）供跨表一致性比對。
- F03Checklist / F03ChecklistItem：Phase 3 完整檢核項，含兩段佐證欄（提案規劃 I / 上線 J）
  供規則式缺漏檢查與 LLM 佐證判讀使用。兩者並存、互不影響既有解析。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from govcheck.models.evidence import WorkflowStage

# 不適用理由帶進報告供治理人員判斷「主張是否成立」；截斷避免整段長文灌進 API payload。
_REASON_MAX_CHARS = 200
# 檢查項目與佐證說明原文帶進報告供人工對照 AI 判讀；同樣截斷，避免長文灌爆 payload。
_TEXT_MAX_CHARS = 600
# 檢核項短名上限：總覽表列首只有一行的寬度，超過就失去「一眼分辨」的作用。
_SHORT_NAME_MAX_CHARS = 14
# 短名切點：取第一個句讀／括號之前的片段。D 欄是「檢查項目＋填寫指引」的完整句子，
# 首個子句幾乎都是該項在講什麼（如「模型監控機制」「資料取得與傳輸安全」）。
# 刻意不含頓號「、」——它是詞組內的並列符（「資料取得、傳輸與儲存」），切在那裡會腰斬語意。
_SHORT_NAME_CUT = re.compile(r"[，,。：:；;（(【\[\n]")
# D 欄偶爾以項次或條列符號起頭（"1-1 "、"(1) "、"一、"）；短名不需要重複項次。
_SHORT_NAME_LEAD = re.compile(r"^\s*(?:\d+[-.]\d+|[（(]?[0-9a-zA-Z一二三四五六七八九十]+[）).、])\s*")


def _clip(text: str | None, limit: int = _TEXT_MAX_CHARS) -> str | None:
    """去頭尾空白後截斷；空字串與 None 一律回 None（報告端才好判斷「有沒有填」）。"""
    stripped = (text or "").strip()
    return stripped[:limit] or None


def derive_short_name(description: str | None, topic: str | None = None) -> str | None:
    """由 D 欄「檢查項目」推導總覽表列首的短名。

    刻意用推導而非硬編對照表：短名來自提案單位上傳的檢核表，硬編一份等於把範本內容
    複製進 repo，且範本改版就會漂移。推導規則是確定性的（去項次前綴 → 切第一個句讀 →
    截長），任何範本都給得出結果。

    D 欄空白時退回管理議題——不理想（同一議題常有多列），但至少不是空字串。
    """
    text = (description or "").strip()
    if not text:
        return _clip(topic, _SHORT_NAME_MAX_CHARS)
    text = _SHORT_NAME_LEAD.sub("", text)
    cut = _SHORT_NAME_CUT.search(text)
    if cut:
        text = text[:cut.start()]
    text = text.strip()
    if not text:
        return _clip(topic, _SHORT_NAME_MAX_CHARS)
    if len(text) > _SHORT_NAME_MAX_CHARS:
        # 省略號也算顯示寬度，確保 fallback 最終不超過 14 字。
        return text[:_SHORT_NAME_MAX_CHARS - 1] + "…"
    return text


def _char_count(text: str | None) -> int:
    """佐證字數（去掉所有空白）。字數是可核對的事實，勝過「過於草率」這種評語。"""
    return len("".join((text or "").split()))


class F03Identity(BaseModel):
    """F03 識別欄（最小解析）。"""

    system_owner: str | None = None  # B1（標籤 A1="系統擁有者"）
    system_name: str | None = None   # Phase 2 無可靠來源；保留給 Phase 3
    sheet_present: bool = True


class F03ChecklistItem(BaseModel):
    """F03「檢核表」單一檢核項（資料列）。"""

    item_id: str                          # 項次 A，如 "1-1"、"4-5"
    # 該項在檢核表的實際列號；供報告組出可稽核座標（「F03·檢核表 J14」），
    # 沒有它治理人員只能拿項次去人工找欄位。範本外的來源可能給不出列號，故可為 None。
    row_index: int | None = None
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
        # 延後匯入以避開 models package 初始化時的循環相依。標準 20 項優先使用 catalog
        # 的固定標籤；範本新增項次時才由 D 欄確定性推導。
        from govcheck.prompts.catalog import short_name_for_item

        # 原文一律帶進報告供人工對照 AI 判讀；提案階段不帶 J 欄，與 evidence_for_stage 一致
        # ——該階段的 J 欄尚未進入審查範圍，帶出來會讓人以為 AI 看過了。
        texts = {
            "description": _clip(self.description),
            "short_name": (
                short_name_for_item(self.item_id)
                or derive_short_name(self.description, self.topic)
            ),
            "evidence_proposal_text": _clip(self.evidence_proposal),
            "evidence_golive_text": (
                None if stage is WorkflowStage.PROPOSAL else _clip(self.evidence_golive)
            ),
            "golive_diff": self.golive_diff_for_stage(stage),
            # 字數取自未截斷的原文：截斷是為了 payload 大小，不該讓「佐證過短」誤判。
            "evidence_proposal_chars": _char_count(self.evidence_proposal),
            "evidence_golive_chars": (
                None if stage is WorkflowStage.PROPOSAL else _char_count(self.evidence_golive)
            ),
            "row_index": self.row_index,
            "lifecycle": self.lifecycle,
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
    # 總覽表列首字樣（由 description 推導，見 derive_short_name）。同一「管理議題」常橫跨
    # 五、六列，只顯示議題名等於整批列都長一樣、無從分辨要看哪一列。
    short_name: str | None = None
    lifecycle: str | None = None                  # B 生命週期（分群用）
    row_index: int | None = None                  # 檢核表列號，供組出可稽核座標
    # 佐證字數（去空白）：規則式「佐證過短」的判定事實，也是總覽表次行的脈絡資訊。
    evidence_proposal_chars: int = 0
    evidence_golive_chars: int | None = None      # 提案階段不看 J 欄，故為 None


class F03Checklist(BaseModel):
    """F03「檢核表」完整解析結果。"""

    subject: str | None = None  # 取 B1 系統擁有者（F03 無系統名稱欄）
    items: list[F03ChecklistItem] = Field(default_factory=list)
    sheet_present: bool = True

    def roster(self, stage: WorkflowStage) -> list[F03ItemCoverage]:
        """全部檢核項的涵蓋狀態名冊，順序同檢核表。"""
        return [item.coverage_for_stage(stage) for item in self.items]
