"""F01 系統資訊表規則檢查（確定性、零 LLM）。

依附件四填寫說明：主表 A~I 欄為必填。逐資料列檢查，每列把缺漏欄彙整成一筆 Finding
（避免欄數爆量）。所有座標／必填清單來自 review_config.yaml。
"""

from __future__ import annotations

from govcheck.models import F01Form, Finding, Severity
from govcheck.review.config import load_review_config


def run_all(form: F01Form, cfg: dict | None = None) -> list[Finding]:
    cfg = cfg or load_review_config()
    findings: list[Finding] = []
    findings += check_has_data_row(form, cfg)
    findings += check_required_fields(form, cfg)
    return findings


def check_has_data_row(form: F01Form, cfg: dict) -> list[Finding]:
    """主表至少要有一筆 AI 應用資料列。"""
    if form.rows:
        return []
    return [Finding(
        severity=Severity.ERROR,
        code="F01.NO_DATA_ROW",
        title="F01 主表無任何填寫資料",
        message="F01 系統資訊表主表未偵測到任何 AI 應用資料列（A~L 欄皆空），請確認已填寫。",
        location="主表",
        expected="至少一筆 AI 應用",
        actual="0 筆",
    )]


def check_required_fields(form: F01Form, cfg: dict) -> list[Finding]:
    """逐列檢查必填欄（A~I）；每列把缺漏欄彙整成一筆 Finding。"""
    findings = []
    required = cfg["f01"]["required_columns"]  # [{col, label}, ...]
    for row in form.rows:
        missing = [rc["label"] for rc in required if not row.raw.get(rc["col"])]
        if missing:
            findings.append(Finding(
                severity=Severity.ERROR,
                code="F01.REQUIRED_MISSING",
                title=f"主表第 {row.row_index} 列有必填欄未填",
                message=f"主表第 {row.row_index} 列必填欄未填：{', '.join(missing)}。",
                location=f"主表第 {row.row_index} 列",
                expected="必填欄皆已填",
                actual=f"缺 {len(missing)} 欄：{', '.join(missing)}",
            ))
    return findings
