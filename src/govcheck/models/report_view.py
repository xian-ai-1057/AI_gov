"""報告頁呈現層模型：缺陷（Defect）、判定（Verdict）、分層狀態（LayerStatus）。

這些型別**不進 ReviewReport 的既有序列化契約**（golden snapshot 走 `model_dump()`，
只鎖規則側 findings）。它們由 `report/defects.py` 從 ReviewReport 推導出來，
單一真相仍是 findings + item_assessments + checklist_roster。

為什麼要有「缺陷」這一層：一份送件包常見的情況是 15 個檢核項全部觸發同一條規則
（例如上線佐證整欄空白），逐列重複同一句話等於把一個原因講了 15 次。缺陷模型把
「同一原因」收成一列，附上筆數與命中位置，讓治理人員先掃掉機械性缺漏，再進逐項覆核。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# 缺陷所屬的檢查層：L1 規則式（確定性、秒級）、L2 檢索式（法規／佐證比對）、L3 語意判讀。
Layer = Literal["L1", "L2", "L3"]
# 產生來源，與 Layer 一對一但獨立標示：UI 用它決定標籤配色，日後新增檢查不必改 UI。
DefectSource = Literal["rule", "rag", "llm"]
# "ok" 是「查過了、本次沒有」——與「沒查」是兩件事，必須看得出差別，故不與 info 合併。
DefectSeverity = Literal["error", "warn", "info", "ok"]


class Locator(BaseModel):
    """可稽核定位：缺陷或引用回到原始檔案的哪一格／哪一頁。

    三遵要不要採信 AI 初判，關鍵在每一句判讀都能回到來源。只有欄位齊備才顯示，
    缺欄一律留 None（不猜、不補空字串）。
    """

    file: str | None = None      # "F03" / "F02" / 佐證檔顯示名
    sheet: str | None = None     # "檢核表"
    cell: str | None = None      # "J14"
    page: int | None = None      # 佐證 PDF 頁碼
    section: str | None = None   # "§3.4"

    def label(self) -> str:
        """人看的一行定位字串；無任何欄位時回 "—"。"""
        head = "·".join(part for part in (self.file, self.sheet) if part)
        tail = " ".join(
            part for part in (
                self.cell,
                f"p.{self.page}" if self.page is not None else None,
                self.section,
            ) if part
        )
        return " ".join(part for part in (head, tail) if part) or "—"


class DefectHit(BaseModel):
    """缺陷的單一命中：哪個項次／欄位，以及可核對的事實值。

    value 一律寫**可核對的事實**（「18 字」「43（各題加總 41）」），
    不寫「過於草率」這種評語——評語無法驗證，事實可以。
    """

    key: str                                # "3-4" 或 "系統名稱"
    value: str                              # "18 字" / "F01 甲 ≠ F03 乙"
    item_id: str | None = None              # F03 檢核項才有
    locator: Locator | None = None


class Defect(BaseModel):
    """一種缺陷類型在本次送件包的彙總結果。"""

    code: str                               # "TOO_SHORT" 等，見 report/defects.py 的登錄表
    layer: Layer
    source: DefectSource
    severity: DefectSeverity
    label: str                              # "佐證過短"
    count: int
    basis: str                              # 判定依據（一句話，可核對）
    confidence: str                         # "確定" / "高 · 可逐條核對" / "中 · 待確認"
    hits: list[DefectHit] = Field(default_factory=list)
    locator_label: str = "—"                # "F03·檢核表 J 欄"
    note: str = ""                          # 給治理人員的補充說明
    # 試辦中的檢查不計入「待覆核」總數，只在展開列出現。準確率達標前不進主清單，
    # 這是讓 L2／L3 逐步被信任的機制（見 docs/f03_review_v2）。
    trial: bool = False
    # 本次送件包每一個檢核項都命中 → 提升成「共通事實帶」，不再逐列重複。
    is_common: bool = False
    codes: list[str] = Field(default_factory=list)  # 對應的 finding code，供稽核回溯


class Verdict(BaseModel):
    """報告頂端的判定句。**標題就是判定**，不是「規則通過」這種只描述某一層的字樣。"""

    code: Literal["blocked", "review", "passed"]
    label: str                              # "初判：不可放行 · 需補件"
    because: str                            # 一句話講規模與主因


class LayerStatus(BaseModel):
    """單一檢查層的執行狀態；漸進出場時逐層更新。"""

    id: Layer
    label: str                              # "規則檢查"
    state: Literal["todo", "running", "done", "skipped"] = "todo"
    elapsed_ms: int | None = None


class CommonFact(BaseModel):
    """全表同值的共通事實：把 N 列同一句話收成一行。"""

    code: str                               # 對應的 Defect.code
    text: str                               # "15/15 項未填上線階段佐證"
    hint: str = "共通事實，不再逐列重複"
    count: int = 0
