"""審查編排器：parse → 跑已註冊的 checks → 匯總成 ReviewReport。

目前只接 F02 規則檢查；未來新增表單/LLM 檢查只要在對應 review_* 裡多串一個 Check。
"""

from __future__ import annotations

from pathlib import Path

from govcheck.checks.rule import f02_rules
from govcheck.models import Finding, ReviewReport, Severity
from govcheck.parsers.f02_parser import parse_f02


def review_f02(path: str | Path, cfg: dict | None = None) -> ReviewReport:
    form = parse_f02(path, cfg)
    findings: list[Finding] = f02_rules.run_all(form, cfg)
    findings.sort(key=_severity_order)

    if not findings:
        findings.append(Finding(
            severity=Severity.INFO,
            code="F02.OK",
            title="規則檢查未發現問題",
            message="F02 通過所有規則式檢查（單複選、系列完整、計分一致、續填）。仍須人工覆核實質內容。",
        ))

    return ReviewReport(form_type="F02", subject=form.subject, findings=findings)


def _severity_order(f: Finding) -> int:
    return {Severity.ERROR: 0, Severity.WARN: 1, Severity.INFO: 2}[f.severity]
