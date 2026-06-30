"""審查編排器：parse → 跑已註冊的 checks → 匯總成 ReviewReport。

- review_f02：單張 F02 風險評鑑（Phase 1）。
- review_submission：整份送件包（Phase 2）= 缺件 + F01 必填 + F02 規則 + 跨表一致性。
未來新增表單/LLM 檢查只要在對應 review_* 裡多串一個 Check。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from govcheck.checks.llm import f03_evidence
from govcheck.checks.rule import cross_consistency, f01_rules, f02_rules, f03_evidence_presence, missing_docs
from govcheck.classify.classifier import FileClassification, classify_files, route_classifications
from govcheck.llm.client import ChatClient
from govcheck.llm.config import load_llm_config
from govcheck.logging_setup import audit, get_logger
from govcheck.models import FilePresence, Finding, ReviewReport, Severity, Submission
from govcheck.parsers.f01_parser import parse_f01
from govcheck.parsers.f02_parser import parse_f02
from govcheck.parsers.f03_parser import parse_f03_checklist, parse_f03_identity
from govcheck.review.config import load_review_config
from govcheck.scoring.f02_score import recompute

log = get_logger("review")

# 進度回呼：審查編排層在各階段邊界誠實回報事件（介面層據此畫進度條）。
# 事件為純 dict，至少含 stage/label/done/total；預設 None 時所有 emit 為 no-op，
# 既有呼叫者（Streamlit / CLI / 舊 API / 測試）行為完全不變。
ProgressFn = Callable[[dict], None]


def _emit(progress: ProgressFn | None, **event) -> None:
    if progress is not None:
        progress(event)


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
    enable_llm: bool = False,
    progress: ProgressFn | None = None,
) -> ReviewReport:
    """整份送件包初步審查。

    files：{"f01": path, "f02": path, "f03": path}，未上傳者不放鍵（或值為 None）。
    supporting：佐證檔名清單（Phase 2 僅看檔名做關鍵字比對，不解析內容）。
    enable_llm：是否對 F03 兩段佐證做 LLM 判讀（預設關；端點不可用時自動降級，不影響規則檢查）。
    progress：選用進度回呼；於 parse / rules / llm 各階段邊界 emit 事件（預設 None = no-op）。
    """
    t0 = time.perf_counter()
    present = [k for k in ("f01", "f02", "f03") if files.get(k) is not None]
    log.info("review_start forms=%s enable_llm=%s", present, enable_llm)

    review_cfg = cfg or load_review_config()
    findings: list[Finding] = []
    sub = _build_submission(files, supporting, review_cfg, findings, progress)

    # 規則階段總步數（依實際會跑的檢查動態計：缺件與跨表恆跑，其餘視表單是否存在）
    has_f03_checklist = sub.f03_checklist is not None and sub.f03_checklist.sheet_present
    rules_total = 2 + (sub.f01 is not None) + (sub.f02 is not None) + has_f03_checklist
    rules_done = 0

    def _rule_step(result: list[Finding]) -> list[Finding]:
        nonlocal rules_done
        rules_done += 1
        _emit(progress, stage="rules", label="規則檢查", done=rules_done, total=rules_total)
        return result

    # 檢查順序維持原狀（missing → f01 → f02 → f03 規則 → f03 LLM → 跨表），確保 findings 插入
    # 順序與既有行為一致；僅在各檢查邊界 emit 進度。跨表進度事件雖在 LLM 之後 emit，前端以
    # 單調 pct 夾制，不會回退。
    findings += _rule_step(missing_docs.run_all(sub, review_cfg))
    if sub.f01 is not None:
        findings += _rule_step(f01_rules.run_all(sub.f01, review_cfg))
    if sub.f02 is not None:
        findings += _rule_step(f02_rules.run_all(sub.f02))  # F02 規則用自己的 scoring config
    if has_f03_checklist:
        findings += _rule_step(f03_evidence_presence.run_all(sub.f03_checklist, review_cfg))  # 規則：恆跑、無網路
        if enable_llm:
            findings += _run_f03_llm(sub.f03_checklist, progress)  # LLM：啟用才跑，失敗自動降級
    findings += _rule_step(cross_consistency.run_all(sub, review_cfg))

    findings.sort(key=_severity_order)
    if not findings:
        findings.append(_submission_ok())
    report = ReviewReport(form_type="送件包", subject=sub.subject, findings=findings)

    dur_ms = round((time.perf_counter() - t0) * 1000)
    log.info("review_done error=%d warn=%d passed=%s dur=%dms",
             report.error_count, report.warn_count, report.passed, dur_ms)
    # 稽核軌跡：只記識別資訊與計數（不記檔內容/佐證全文）；任何 profile 都落檔。
    audit("review_done", subject=report.subject, filing_unit=sub.f02_filing_unit,
          form_type=report.form_type, error=report.error_count, warn=report.warn_count,
          passed=report.passed, enable_llm=enable_llm, duration_ms=dur_ms)
    return report


def _run_f03_llm(checklist, progress: ProgressFn | None = None) -> list[Finding]:
    """建立 LLM 客戶端並跑 F03 佐證判讀；任何初始化/執行失敗一律降級為 INFO，不中斷規則檢查。"""
    try:
        llm_cfg = load_llm_config()
        client = ChatClient.from_config(llm_cfg)
        return f03_evidence.run_all(
            checklist, client,
            max_items=llm_cfg["max_items"],
            batch_size=llm_cfg["batch_size"],
            progress=progress,
        )
    except Exception as exc:  # noqa: BLE001 - LLM 不可用一律降級
        log.warning("llm review skipped: %s", exc)
        return [Finding(
            severity=Severity.INFO,
            code="F03.LLM_SKIPPED",
            title="LLM 佐證審查已略過",
            message=f"無法初始化或執行 LLM 判讀（{exc}）；其餘規則檢查不受影響。",
            source="llm",
        )]


def review_routed(
    files: dict[str, str | Path],
    supporting: list[str] | None = None,
    class_findings: list[Finding] | None = None,
    cfg: dict | None = None,
    enable_llm: bool = False,
    progress: ProgressFn | None = None,
) -> ReviewReport:
    """已分類路由後的審查：跑既有 review_submission，再併入分類 Findings。

    review_submission 行為不變；此處於其上薄薄一層，把分類產生的 Findings
    （CLASSIFY.SUMMARY / 重複 / 無法辨識）併入，重排後僅在整體無 ERROR/WARN 時補 OK。
    enable_llm 透傳給 review_submission（控制 F03 佐證 LLM 判讀）。
    progress 透傳給 review_submission（介面層據此畫進度條；預設 None = no-op）。
    """
    review_cfg = cfg or load_review_config()
    report = review_submission(
        files, supporting=supporting, cfg=review_cfg, enable_llm=enable_llm, progress=progress,
    )

    merged = [f for f in report.findings if f.code != "SUBMISSION.OK"]
    merged = list(class_findings or []) + merged
    merged.sort(key=_severity_order)
    if not any(f.severity in (Severity.ERROR, Severity.WARN) for f in merged):
        merged.append(_submission_ok())
    return report.model_copy(update={"findings": merged})


def review_files(
    paths: list[str | Path],
    cfg: dict | None = None,
    enable_llm: bool = False,
) -> tuple[ReviewReport, list[FileClassification]]:
    """批次檔案自動分類 → 路由 → 審查。

    回傳 (報告, 分類結果)：分類結果是介面顯示用 metadata（檔名→判定），
    刻意不塞進 ReviewReport，以免污染跨階段契約。
    """
    review_cfg = cfg or load_review_config()
    results = classify_files(paths, review_cfg)
    files, supporting, class_findings = route_classifications(results)
    report = review_routed(files, supporting, class_findings, review_cfg, enable_llm=enable_llm)
    return report, results


def _submission_ok() -> Finding:
    """送件包規則檢查全數通過的 INFO Finding（review_submission 與 review_routed 共用）。"""
    return Finding(
        severity=Severity.INFO,
        code="SUBMISSION.OK",
        title="送件包規則檢查未發現問題",
        message="核心文件齊備、F01 必填完整、F02 規則通過、跨表一致。仍須人工覆核實質內容。",
    )


def _build_submission(
    files: dict[str, str | Path],
    supporting: list[str] | None,
    review_cfg: dict,
    findings: list[Finding],
    progress: ProgressFn | None = None,
) -> Submission:
    """逐檔解析成 Submission；每檔包 try/except，失敗加 PARSE_ERROR 並繼續（介面層不崩）。"""
    presence = FilePresence(
        f01=files.get("f01") is not None,
        f02=files.get("f02") is not None,
        f03=files.get("f03") is not None,
    )
    sub = Submission(presence=presence, supporting_docs=list(supporting or []))

    # 解析是耗時大宗（openpyxl 每檔約 1–3 秒）；每解析完一張表 emit 一次進度。
    parse_total = int(presence.f01) + int(presence.f02) + int(presence.f03)
    parse_done = 0

    def _parse_step() -> None:
        nonlocal parse_done
        parse_done += 1
        _emit(progress, stage="parse", label="解析表單", done=parse_done, total=parse_total)

    if presence.f01:
        log.debug("parsing form=F01")
        try:
            sub.f01 = parse_f01(files["f01"], review_cfg)
        except Exception as exc:  # noqa: BLE001 - 介面層需把解析錯誤友善呈現
            log.warning("parse failed form=F01 err=%s", type(exc).__name__)
            findings.append(_parse_error("F01", exc))
        _parse_step()
    if presence.f02:
        log.debug("parsing form=F02")
        try:
            sub.f02 = parse_f02(files["f02"])
        except Exception as exc:  # noqa: BLE001
            log.warning("parse failed form=F02 err=%s", type(exc).__name__)
            findings.append(_parse_error("F02", exc))
        _parse_step()
    if presence.f03:
        log.debug("parsing form=F03")
        try:
            sub.f03 = parse_f03_identity(files["f03"], review_cfg)
        except Exception as exc:  # noqa: BLE001
            log.warning("parse failed form=F03 err=%s", type(exc).__name__)
            findings.append(_parse_error("F03", exc))
        try:
            sub.f03_checklist = parse_f03_checklist(files["f03"], review_cfg)
        except Exception as exc:  # noqa: BLE001 - 檢核項解析失敗不影響識別欄與其他檢查
            log.warning("parse failed form=F03_checklist err=%s", type(exc).__name__)
            findings.append(Finding(
                severity=Severity.ERROR,
                code="F03.CHECKLIST_PARSE_ERROR",
                title="F03 檢核項解析失敗",
                message=f"F03 檢核表的檢核項無法解析（{exc}）；識別欄與其他檢查不受影響。",
                location="F03 檢核表",
                expected="可解析的官方範本",
                actual="解析失敗",
            ))
        _parse_step()

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
