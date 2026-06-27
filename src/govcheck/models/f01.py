"""F01 系統資訊表的領域模型。

F01 主表「AI系統業務與利害關係人」是橫向表：每一資料列代表一個 AI 應用。
parser 把每列攤平成 F01ApplicationRow（語意欄位 + raw 欄字母字典，後者驅動必填檢查），
附屬表（資料/模型/平台）的「對應專案/服務名稱」欄收進 F01SubRef，供跨表內部一致性比對。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class F01ApplicationRow(BaseModel):
    """F01 主表一筆 AI 應用資料列。"""

    row_index: int                           # Excel 主表列號（定位用）
    filing_unit: str | None = None           # A 填表單位
    filer: str | None = None                 # B 填表人
    filing_date: str | None = None           # C 填表日期
    project_name: str | None = None          # D 專案/服務名稱（系統識別鍵）
    ai_usage: str | None = None              # E AI系統運用
    affected_system_name: str | None = None  # F 影響系統名稱
    affected_system_code: str | None = None  # G 影響系統代碼
    system_owner: str | None = None          # H System Owner
    ap_owner: str | None = None              # I AP Owner
    ai_app_type: str | None = None           # J AI應用類型
    business_goal: str | None = None         # K 業務目標與功能說明
    tech_type: str | None = None             # L AI技術類型
    raw: dict[str, str | None] = Field(default_factory=dict)  # 欄字母 → 值，驅動必填檢查


class F01SubRef(BaseModel):
    """附屬表（資料/模型/平台）一筆「對應專案/服務名稱」。"""

    sheet: str
    row_index: int
    corr_project_name: str | None = None


class F01Form(BaseModel):
    """攤平後的 F01 內容。"""

    subject: str | None = None  # = rows[0].project_name，供報告標題
    rows: list[F01ApplicationRow] = Field(default_factory=list)
    sub_refs: list[F01SubRef] = Field(default_factory=list)
