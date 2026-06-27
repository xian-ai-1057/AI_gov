"""跨表一致性檢查（F01/F02/F03，確定性字串比對 → WARN）。

R02 要求三表的 System Owner、單位等資訊一致。但 F02 與 F03 皆無系統名稱欄
（F03 的 B1 為 System Owner），故 Phase 2 做：
  - System Owner：F01.H ↔ F03.B1
  - 填表單位：    F01.A ↔ F02 風險處理計畫表 N2（僅中/高風險已填時存在）
  - F01 內部：    主表「專案/服務名稱」↔ 附屬表「對應專案/服務名稱」
任一側為空一律 skip（避開模板示範列、低風險 F02 無單位欄造成的誤報）。比對前正規化
（去空白、casefold），不一致一律 WARN 交人工。比對映射全在 review_config.yaml。
"""

from __future__ import annotations

from govcheck.models import Finding, Severity, Submission
from govcheck.review.config import load_review_config

_SEVERITY = {"error": Severity.ERROR, "warn": Severity.WARN, "info": Severity.INFO}


def run_all(sub: Submission, cfg: dict | None = None) -> list[Finding]:
    cfg = cfg or load_review_config()
    findings: list[Finding] = []
    findings += check_comparisons(sub, cfg)
    findings += check_f01_internal(sub, cfg)
    return findings


def check_comparisons(sub: Submission, cfg: dict) -> list[Finding]:
    """config 驅動的兩兩欄位比對（owner / 單位）。

    F01 為多應用橫向表，左側取「所有列」的該欄值集合；右側（F02/F03）為單值。
    兩側正規化集合皆非空且無交集才算不一致——右側值須對得上 F01 任一列，否則 WARN。
    """
    findings = []
    for spec in cfg["cross_consistency"].get("comparisons", []):
        lefts = _get_values(sub, spec["left"]["form"], spec["left"]["field"])
        rights = _get_values(sub, spec["right"]["form"], spec["right"]["field"])
        if not _sets_mismatch(lefts, rights):
            continue
        findings.append(Finding(
            severity=_SEVERITY[spec.get("severity", "warn")],
            code=spec["code"],
            title=f"跨表不一致：{spec['label']}",
            message=(f"{spec['label']} 在兩表對不上，請人工確認是否為同一系統／單位"
                     f"（也可能是簡稱或全半形差異）。"),
            location=spec["label"],
            expected="、".join(lefts),
            actual="、".join(rights),
        ))
    return findings


def check_f01_internal(sub: Submission, cfg: dict) -> list[Finding]:
    """F01 各附屬表對應欄 ↔ 主表專案名稱集合：對應欄須對得上任一主表應用。"""
    findings = []
    if not sub.f01 or not sub.f01.rows:
        return findings
    spec = cfg["cross_consistency"]["f01_internal"]
    main_names = {_norm(r.project_name) for r in sub.f01.rows if _norm(r.project_name)}
    if not main_names:
        return findings  # 主表無任何名稱 → 無基準可比
    main_display = "、".join(dict.fromkeys(r.project_name for r in sub.f01.rows if _norm(r.project_name)))
    for ref in sub.f01.sub_refs:
        cn = _norm(ref.corr_project_name)
        if cn and cn not in main_names:  # 對應欄非空且對不上任何主表應用
            findings.append(Finding(
                severity=_SEVERITY[spec.get("severity", "warn")],
                code=spec["code"],
                title=f"F01 內部專案名稱對不上（{ref.sheet}）",
                message=(f"「{ref.sheet}」第 {ref.row_index} 列對應欄「{ref.corr_project_name}」"
                         f"與主表任一專案/服務名稱（{main_display}）皆不符，請人工確認。"),
                location=f"主表 / {ref.sheet}第{ref.row_index}列",
                expected=main_display,
                actual=ref.corr_project_name,
            ))
    return findings


def _get_values(sub: Submission, form: str, field: str) -> list[str]:
    """解析 (form, field) → 非空原始值清單。F01 取所有列的該欄；F02 僅支援 filing_unit。"""
    if form == "f01":
        if not sub.f01:
            return []
        return [v for r in sub.f01.rows if (v := getattr(r, field, None))]
    if form == "f02":
        v = sub.f02_filing_unit if field == "filing_unit" else None
        return [v] if v else []
    if form == "f03":
        v = getattr(sub.f03, field, None) if sub.f03 else None
        return [v] if v else []
    return []


def _norm(s: str | None) -> str | None:
    """正規化：去所有空白 + casefold；空回 None。"""
    if s is None:
        return None
    return "".join(s.split()).casefold() or None


def _sets_mismatch(lefts: list[str], rights: list[str]) -> bool:
    """兩側正規化集合皆非空且無交集才算「需回報的不一致」；任一側空則 skip。"""
    left_set = {_norm(x) for x in lefts if _norm(x)}
    right_set = {_norm(x) for x in rights if _norm(x)}
    if not left_set or not right_set:
        return False
    return left_set.isdisjoint(right_set)
