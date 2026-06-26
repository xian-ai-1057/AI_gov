"""F02 風險評鑑的領域模型。

F02Form 是 parser 的輸出、scoring 與 checks 的輸入：把官方 .xlsm 攤平成乾淨的
答案字典 + 加成因子 + 檔內快取分數 + 兩張續填表的狀態，後續邏輯一律對著它跑。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# 答案值；None 代表未填
Answer = str  # "Y" | "N"


class DomainScores(BaseModel):
    """四個風險域的分數（百分比或原始分皆用此結構）。"""

    finance: float = 0.0
    operation: float = 0.0
    reputation: float = 0.0
    compliance: float = 0.0

    def max_value(self) -> float:
        return max(self.finance, self.operation, self.reputation, self.compliance)


class CachedScores(BaseModel):
    """從 .xlsm 直接讀回的「Excel 已算好」的分數，作為比對基準。"""

    percentages: DomainScores | None = None  # E46:H46 加成後風險百分比
    overall: float | None = None             # I2 = MAX(百分比)
    grade: str | None = None                 # I4 分級：低/中/高


class F02Form(BaseModel):
    """攤平後的 F02 內容。"""

    subject: str | None = None
    # 題號（含子項，如 "UC-04-02"）→ 答案；未填則不在 dict 或值為 None
    answers: dict[str, Answer | None] = Field(default_factory=dict)
    # 加成因子 factor1/2/3 → "Y"(有)/"N"(無)/None
    uplift: dict[str, Answer | None] = Field(default_factory=dict)
    cached: CachedScores = Field(default_factory=CachedScores)
    # 續填表狀態
    residual_filled: bool = False
    residual_max_score: float | None = None  # 剩餘風險評鑑分數欄的最大值
    treatment_filled: bool = False
