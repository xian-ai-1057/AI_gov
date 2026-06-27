"""審查編排器：parse → 跑已註冊的 checks → 匯總成 ReviewReport。

- review_f02：單張 F02 風險評鑑（Phase 1）。
- review_submission：整份送件包（Phase 2）= 缺件 + F01 必填 + F02 規則 + 跨表一致性。
未來新增表單/LLM 檢查只要在對應 review_* 裡多串一個 Check。
"""

from __future__ import annotations

from pathlib import Path

from govcheck.checks.rule import cross_consistency, f01_rules, f02_rules, missing_docs
from govcheck.models import FilePresence, Finding, ReviewReport, Severity, Submission
from govcheck.parsers.f01_parser import parse_f01
from govcheck.parsers.f02_parser import parse_f02
from govcheck.parsers.f03_parser import parse_f03_identity
from govcheck.review.config import load_review_config
from govcheck.scoring.f02_score import recompute


def review_f02(path: str | Path, cfg: dict | None = None) -> ReviewReport:
    form = parse_f02(path, cfg)
    findings: list[Finding] = f02_rules.run_all(form, cfg)
    findings.sort(key=_severity_order)

    if not findings:
        findings.append(Finding(
            severity=Severity.INFO,
            code="F02.OK",
            title="規則檢查未發現問題",
            message=("F02 通過所有規則式檢查（缺漏、單複選、系列與一般題完整、計分比對、續填）。"
                     "仍須人工覆核實質內容。"),
        ))

    return ReviewReport(form_type="F02", subject=form.subject, findings=findings)


def review_submission(
    files: dict[str, str | Path],
    supporting: list[str] | None = None,
    cfg: dict | None = None,
) -> ReviewReport:
    """整份送件包初步審查。

    files：{"f01": path, "f02": path, "f03": path}，未上傳者不放鍵（或值為 None）。
    supporting：佐證檔名清單（Phase 2 僅看檔名做關鍵字比對，不解析內容）。
    """
    review_cfg = cfg or load_review_config()
    findings: list[Finding] = []
    sub = _build_submission(files, supporting, review_cfg, findings)

    findings += missing_docs.run_all(sub, review_cfg)
    if sub.f01 is not None:
        findings += f01_rules.run_all(sub.f01, review_cfg)
    if sub.f02 is not None:
        findings += f02_rules.run_all(sub.f02)  # F02 規則用自己的 scoring config
    findings += cross_consistency.run_all(sub, review_cfg)

    findings.sort(key=_severity_order)
    if not findings:
        findings.append(Finding(
            severity=Severity.INFO,
            code="SUBMISSION.OK",
            title="送件包規則檢查未發現問題",
            message="核心文件齊備、F01 必填完整、F02 規則通過、跨表一致。仍須人工覆核實質內容。",
        ))
    return ReviewReport(form_type="送件包", subject=sub.subject, findings=findings)


def _build_submission(
    files: dict[str, str | Path],
    supporting: list[str] | None,
    review_cfg: dict,
    findings: list[Finding],
) -> Submission:
    """逐檔解析成 Submission；每檔包 try/except，失敗加 PARSE_ERROR 並繼續（介面層不崩）。"""
    presence = FilePresence(
        f01=files.get("f01") is not None,
        f02=files.get("f02") is not None,
        f03=files.get("f03") is not None,
    )
    sub = Submission(presence=presence, supporting_docs=list(supporting or []))

    if presence.f01:
        try:
            sub.f01 = parse_f01(files["f01"], review_cfg)
        except Exception as exc:  # noqa: BLE001 - 介面層需把解析錯誤友善呈現
            findings.append(_parse_error("F01", exc))
    if presence.f02:
        try:
            sub.f02 = parse_f02(files["f02"])
        except Exception as exc:  # noqa: BLE001
            findings.append(_parse_error("F02", exc))
    if presence.f03:
        try:
            sub.f03 = parse_f03_identity(files["f03"], review_cfg)
        except Exception as exc:  # noqa: BLE001
            findings.append(_parse_error("F03", exc))

    if sub.f02 is not None:
        # 風險等級以答案重算為準（確定性真值），重算失敗才退回檔內快取分級；
        # 驅動條件式佐證缺件。快取與重算不符另由 f02_rules 的 F02.GRADE_MISMATCH 標示。
        sub.risk_grade = _safe_grade(sub.f02) or sub.f02.cached.grade
        sub.f02_filing_unit = sub.f02.filing_unit  # parse_f02 已讀 N2，免重複開檔

    # subject 只能來自 F01（F02 無系統名稱欄）
    sub.subject = sub.f01.subject if sub.f01 else None
    return sub


def _safe_grade(f02) -> str | None:
    try:
        return recompute(f02).grade
    except Exception:  # noqa: BLE001
        return None


def _parse_error(form_label: str, exc: Exception) -> Finding:
    return Finding(
        severity=Severity.ERROR,
        code=f"{form_label}.PARSE_ERROR",
        title=f"{form_label} 解析失敗",
        message=f"{form_label} 檔案無法解析（{exc}）。請確認檔案為官方範本且未損毀。",
        location=form_label,
        expected="可解析的官方範本",
        actual="解析失敗",
    )


def _severity_order(f: Finding) -> int:
    return {Severity.ERROR: 0, Severity.WARN: 1, Severity.INFO: 2}[f.severity]
