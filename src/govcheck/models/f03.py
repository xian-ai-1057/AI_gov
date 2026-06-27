"""F03 上線檢核表的識別模型（Phase 2 僅取識別欄）。

F03「檢核表」的生命週期檢核項屬判讀性質，留待 Phase 3 LLM 兩段式判讀。
Phase 2 只解析識別欄（System Owner，B1）供跨表一致性比對，與未來完整解析相容。
"""

from __future__ import annotations

from pydantic import BaseModel


class F03Identity(BaseModel):
    """F03 識別欄（最小解析）。"""

    system_owner: str | None = None  # B1（標籤 A1="系統擁有者"）
    system_name: str | None = None   # Phase 2 無可靠來源；保留給 Phase 3
    sheet_present: bool = True
