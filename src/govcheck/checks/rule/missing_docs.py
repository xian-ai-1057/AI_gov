"""缺件檢查（送件包層級，確定性 + 檔名啟發式）。

核心三表：F01/F03 一律必備（缺 → ERROR）、F02 中/高風險必備、低風險可免（缺 → WARN）。
中/高風險另需 R05 委外 / R06 可解釋性 / R07 公平性 佐證，以「佐證檔名關鍵字」比對；
未命中 → WARN（檔名比對屬啟發式，標示請人工確認）。設定全在 review_config.yaml。
"""

from __future__ import annotations

from govcheck.models import Finding, Severity, Submission
from govcheck.review.config import load_review_config

_SEVERITY = {"error": Severity.ERROR, "warn": Severity.WARN, "info": Severity.INFO}


def run_all(sub: Submission, cfg: dict | None = None) -> list[Finding]:
    cfg = cfg or load_review_config()
    findings: list[Finding] = []
    findings += check_core_forms(sub, cfg)
    findings += check_conditional_docs(sub, cfg)
    return findings


def check_core_forms(sub: Submission, cfg: dict) -> list[Finding]:
    """三份核心表是否齊備。"""
    findings = []
    docs = cfg["documents"]
    present = {"f01": sub.presence.f01, "f02": sub.presence.f02, "f03": sub.presence.f03}

    for spec in docs["core"]:  # F01 / F03，一律必備
        if not present.get(spec["role"], False):
            findings.append(Finding(
                severity=_SEVERITY[spec["severity"]],
                code=spec["code"],
                title=f"缺件：{spec['label']}",
                message=f"送件包未包含「{spec['label']}」，依規定為必備文件。",
                location="送件包",
                expected=f"已附 {spec['label']}",
                actual="未上傳",
            ))

    if not sub.presence.f02:  # F02：中/高風險必備，低風險可免
        f02spec = docs["f02_rule"]
        findings.append(Finding(
            severity=_SEVERITY[f02spec["severity"]],
            code=f02spec["code"],
            title=f"缺件：{f02spec['label']}",
            message=("送件包未包含「F02 AI風險評鑑」。低風險系統可免；若本案為中/高風險則為必備，"
                     "且缺 F02 無法判定風險分級，請人工確認。"),
            location="送件包",
            expected="中/高風險需附 F02",
            actual="未上傳",
        ))
    return findings


def check_conditional_docs(sub: Submission, cfg: dict) -> list[Finding]:
    """中/高風險條件式佐證；以佐證檔名關鍵字比對，未命中 → WARN。"""
    findings = []
    names = " ".join(sub.supporting_docs).casefold()
    for spec in cfg["documents"].get("conditional", []):
        if not _condition_met(sub, spec.get("when", {})):
            continue
        keywords = spec.get("keywords", [])
        if any(kw.casefold() in names for kw in keywords):
            continue  # 佐證檔名已命中關鍵字 → 視為已附
        findings.append(Finding(
            severity=Severity.WARN,
            code=spec["code"],
            title=f"可能缺件：{spec['label']}",
            message=(f"本案風險為「{sub.risk_grade}」，依規定可能需附「{spec['label']}」；"
                     f"佐證清單未見相關關鍵字（{', '.join(keywords)}），請人工確認是否已提供。"),
            location="佐證文件",
            expected=f"附 {spec['label']}",
            actual="佐證清單未見關鍵字",
        ))
    return findings


def _condition_met(sub: Submission, when: dict) -> bool:
    risk_in = when.get("risk_in")
    if risk_in is not None and sub.risk_grade not in risk_in:
        return False
    return True
