"""送件包（submission）模型：把一次送審的多份表單與佐證綁在一起。

缺件檢查與跨表一致性檢查都在送件包層級進行：presence 標哪些核心表已上傳，
f01/f02/f03 為各自解析結果，supporting_docs 為佐證「檔名」清單（Phase 2 不解析內容），
risk_grade 由 F02 取得（缺 F02 則 None），驅動條件式佐證缺件判斷。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from govcheck.models.f01 import F01Form
from govcheck.models.f02 import F02Form
from govcheck.models.f03 import F03Checklist, F03Identity


class FilePresence(BaseModel):
    """三份核心表是否已上傳。"""

    f01: bool = False
    f02: bool = False
    f03: bool = False


class Submission(BaseModel):
    """一次送審的完整送件包。"""

    subject: str | None = None
    presence: FilePresence = Field(default_factory=FilePresence)
    f01: F01Form | None = None
    f02: F02Form | None = None
    f03: F03Identity | None = None
    f03_checklist: F03Checklist | None = None  # F03 完整檢核項（Phase 3 佐證審查用）
    supporting_docs: list[str] = Field(default_factory=list)  # 佐證檔名（Phase 2 僅檔名）
    risk_grade: str | None = None        # 由 F02 重算取得；缺 F02 則 None
    f02_filing_unit: str | None = None   # F02 風險處理計畫表 N2，供跨表單位比對
