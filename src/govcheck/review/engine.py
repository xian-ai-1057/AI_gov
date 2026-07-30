"""審查編排器：parse → 跑已註冊的 checks → 匯總成 ReviewReport。

- review_f02：單張 F02 風險評鑑。
- review_submission：整份送件包 = 缺件 + F01 必填 + F02 規則 + F03 佐證缺漏 +
  跨表一致性 +（啟用 LLM 時）F03 語意判讀（內容品質 + 佐證支持 + 法規符合性一次判完）。

新增檢查 = 新增 Check + 在此註冊，不動既有程式。
"""

from __future__ import annotations

import time
from pathlib import Path

from govcheck.checks.llm import f03_assessment
from govcheck.checks.rule import (
    cross_consistency,
    f01_rules,
    f02_reg_refs,
    f02_rules,
    f03_evidence_presence,
    missing_docs,
)
from govcheck.classify import (
    FileClassification,
    FileKind,
    classify_files,
    route_classifications,
)
from govcheck.evidence import EvidenceRetriever
from govcheck.llm.client import ChatClient
from govcheck.llm.config import load_llm_config
from govcheck.llm.embedding import EmbeddingClient
from govcheck.logging_setup import audit, get_logger
from govcheck.models import (
    DecisionStatus,
    DocumentParseStatus,
    FilePresence,
    Finding,
    ReviewReport,
    ReviewRunMeta,
    Severity,
    Submission,
    UsageSummary,
    WorkflowStage,
)
from govcheck.parsers.f01_parser import parse_f01
from govcheck.parsers.f02_parser import parse_f02
from govcheck.parsers.f03_parser import parse_f03_checklist, parse_f03_identity
from govcheck.parsers.supporting import PARSER_SOURCE_REF, TsParserAdapter
from govcheck.prompts import build_prompt_gateway
from govcheck.regulations.config import load_regulations_config
from govcheck.regulations.mapping import load_retrieval_map
from govcheck.regulations.models import RetrievalMap
from govcheck.review.config import load_review_config
from govcheck.scoring.f02_score import load_config as load_scoring_config
from govcheck.scoring.f02_score import recompute

log = get_logger("review")


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

    score, grade = _f02_risk(form, cfg)
    return ReviewReport(
        form_type="F02", subject=form.subject, findings=findings,
        risk_score=score, risk_grade=grade,
        decision_status=_decision_status(findings),
    )


def review_submission(
    files: dict[str, str | Path],
    supporting: list[str] | None = None,
    cfg: dict | None = None,
    enable_llm: bool = False,
    supporting_paths: list[str | Path] | None = None,
    workflow_stage: WorkflowStage | str = WorkflowStage.PROPOSAL,
    initial_findings: list[Finding] | None = None,
) -> ReviewReport:
    """整份送件包初步審查。

    files：{"f01": path, "f02": path, "f03": path}，未上傳者不放鍵（或值為 None）。
    supporting：佐證檔名清單（供缺件檢查做關鍵字比對）。
    supporting_paths：佐證檔實際路徑（供解析內容做語意判讀的佐證來源）。
    enable_llm：是否做 F03 語意判讀（端點不可用時自動降級，不影響規則檢查）。
    initial_findings：上游分類或路由階段已產生、須一併納入判定與稽核的 findings。
    """
    t0 = time.perf_counter()
    present = [k for k in ("f01", "f02", "f03") if files.get(k) is not None]
    log.info("review_start forms=%s enable_llm=%s", present, enable_llm)

    review_cfg = cfg or load_review_config()
    workflow_stage = WorkflowStage(workflow_stage)
    findings = list(initial_findings or [])
    sub = _build_submission(files, supporting, supporting_paths, review_cfg, findings)
    assessments = []
    prompt_refs = []
    usage = sub.parser_usage.model_copy(deep=True)

    has_f03_checklist = sub.f03_checklist is not None and sub.f03_checklist.sheet_present
    reg_cfg = load_regulations_config()
    retrieval_map = _load_regulation_map(reg_cfg)

    # 檢查順序：缺件 → F01 → F02 → F03 規則 → F03 語意判讀 → F02 法規查表 → 跨表一致性。
    findings += missing_docs.run_all(sub, review_cfg)
    if sub.f01 is not None:
        findings += f01_rules.run_all(sub.f01, review_cfg)
    if sub.f02 is not None:
        findings += f02_rules.run_all(sub.f02)  # F02 規則用自己的 scoring config
    if has_f03_checklist:
        findings += f03_evidence_presence.run_all(
            sub.f03_checklist, review_cfg, workflow_stage
        )
        if enable_llm:
            outcome = _run_f03_assessment(
                sub.f03_checklist,
                sub.document_artifacts,
                workflow_stage,
                retrieval_map,
                reg_cfg,
            )
            findings += outcome.findings
            assessments = outcome.assessments
            prompt_refs = outcome.prompt_refs
            usage.add(outcome.usage)
    if sub.f02 is not None:
        # 純查表、零 LLM：F02-only 送件包也會列出觸發題對應的法規條文。
        findings += f02_reg_refs.run_all(
            sub.f02, retrieval_map, load_scoring_config(), reg_cfg["max_items"]
        )
    if retrieval_map is None and (sub.f02 is not None or has_f03_checklist):
        # 缺對照表 = 需求「專案內容是否合規」失去審查基準，必須明示，不可靜默通過。
        findings.append(_regulation_map_unavailable())
    findings += cross_consistency.run_all(sub, review_cfg)

    findings.sort(key=_severity_order)
    # 只要沒有 ERROR/WARN 就補一筆通過紀錄——僅有 INFO（法規引用、判讀摘要）時，
    # 報告不該讓治理人員自己推論「沒看到問題就是沒問題」。
    if not any(f.severity in (Severity.ERROR, Severity.WARN) for f in findings):
        findings.append(_submission_ok())
    report = ReviewReport(
        form_type="送件包", subject=sub.subject, findings=findings,
        risk_score=sub.risk_score, risk_grade=sub.risk_grade,
        decision_status=_decision_status(
            findings,
            documents=sub.document_artifacts,
            llm_required=has_f03_checklist,
            llm_completed=bool(assessments) if enable_llm else False,
        ),
        item_assessments=assessments,
        # 名冊獨立於 LLM 是否啟用：關掉語意判讀時，它是總覽表唯一能列出全部檢核項的來源。
        checklist_roster=(
            sub.f03_checklist.roster(workflow_stage) if has_f03_checklist else []
        ),
        document_statuses=sub.document_artifacts,
        usage_summary=usage,
        review_run_meta=ReviewRunMeta(
            workflow_stage=workflow_stage,
            prompt_refs=prompt_refs,
            parser_source_commit=PARSER_SOURCE_REF if sub.document_artifacts else None,
            model_deployment=load_llm_config().get("model") if enable_llm else None,
        ),
    )

    dur_ms = round((time.perf_counter() - t0) * 1000)
    log.info(
        "review_done error=%d warn=%d decision=%s dur=%dms",
        report.error_count,
        report.warn_count,
        report.decision_status.value,
        dur_ms,
    )
    # 稽核軌跡：只記識別資訊與計數（不記檔內容/佐證全文）；任何 profile 都落檔。
    audit(
        "review_done",
        # 識別資訊是稽核軌跡的核心：沒有它就無法回溯這筆記錄對應哪一份送件。
        subject=report.subject,
        filing_unit=sub.f02_filing_unit,
        form_type=report.form_type,
        error=report.error_count,
        warn=report.warn_count,
        decision_status=report.decision_status.value,
        enable_llm=enable_llm,
        parser_documents=len(report.document_statuses),
        review_llm_calls=(report.usage_summary.review_llm_primary_calls
                          + report.usage_summary.review_llm_escalation_calls),
        duration_ms=dur_ms,
    )
    return report


def review_routed(
    files: dict[str, str | Path],
    supporting: list[str] | None = None,
    class_findings: list[Finding] | None = None,
    cfg: dict | None = None,
    enable_llm: bool = False,
    supporting_paths: list[str | Path] | None = None,
    workflow_stage: WorkflowStage | str = WorkflowStage.PROPOSAL,
) -> ReviewReport:
    """審查已分類的送件包，並把分類摘要與警告併入最終報告。"""
    return review_submission(
        files,
        supporting=supporting,
        cfg=cfg,
        enable_llm=enable_llm,
        supporting_paths=supporting_paths,
        workflow_stage=workflow_stage,
        initial_findings=class_findings,
    )


def review_files(
    paths: list[str | Path],
    cfg: dict | None = None,
    enable_llm: bool = False,
    workflow_stage: WorkflowStage | str = WorkflowStage.PROPOSAL,
) -> tuple[ReviewReport, list[FileClassification]]:
    """批次分類檔案後自動路由並審查，回傳報告與逐檔分類結果。"""
    review_cfg = cfg or load_review_config()
    classifications = classify_files(paths, review_cfg)
    files, supporting, class_findings = route_classifications(classifications)
    supporting_paths = [
        result.path
        for result in classifications
        if result.kind is FileKind.SUPPORTING and result.path is not None
    ]
    report = review_routed(
        files,
        supporting=supporting,
        class_findings=class_findings,
        cfg=review_cfg,
        enable_llm=enable_llm,
        supporting_paths=supporting_paths,
        workflow_stage=workflow_stage,
    )
    return report, classifications


def _load_regulation_map(reg_cfg: dict) -> RetrievalMap | None:
    """載入凍結法規對照表；缺檔 / 版本不符 / 格式錯誤一律回 None（絕不 raise）。

    對照表是本機資產（含法規條文，不入庫），新環境常常沒有。缺檔不該讓整份審查失敗，
    但也不該靜默——呼叫端會補一筆 REGULATION.MAP_UNAVAILABLE。
    """
    try:
        return load_retrieval_map(reg_cfg["mapping_path"])
    except Exception as exc:  # noqa: BLE001 - 缺對照表一律降級，不逸出
        log.warning("regulation map unavailable err=%s", type(exc).__name__)
        return None


def _regulation_map_unavailable() -> Finding:
    return Finding(
        severity=Severity.INFO,
        code="REGULATION.MAP_UNAVAILABLE",
        title="法規對照表未就緒，本次未做條文比對",
        message=("找不到或無法讀取法規對照表，本次審查未引用任何法規條文，"
                 "亦未判讀法規符合性；其餘檢查不受影響，合規性請人工覆核。"),
        source="rule",
    )


def _run_f03_assessment(
    checklist,
    artifacts,
    workflow_stage: WorkflowStage,
    retrieval_map: RetrievalMap | None,
    reg_cfg: dict,
) -> f03_assessment.AssessmentOutcome:
    """建立 prompt gateway 與佐證檢索器，跑統一 F03 判讀；任何初始化失敗一律降級。"""
    try:
        llm_cfg = load_llm_config()
        client = ChatClient.from_config(llm_cfg)
        gateway = build_prompt_gateway(client, llm_cfg)
        embedding_client = (
            EmbeddingClient.from_config(llm_cfg)
            if llm_cfg.get("evidence_embedding_enabled")
            else None
        )
        retriever = EvidenceRetriever(
            embedding_client=embedding_client,
            lexical_weight=llm_cfg.get("evidence_lexical_weight", 0.7),
        )
        return f03_assessment.run_all(
            checklist,
            artifacts,
            gateway,
            workflow_stage=workflow_stage,
            max_items=llm_cfg["max_items"],
            top_k=llm_cfg.get("evidence_top_k", 4),
            retriever=retriever,
            retrieval_map=retrieval_map,
            max_sections_per_item=reg_cfg["max_sections_per_item"],
            max_excerpt_chars=reg_cfg["max_excerpt_chars"],
        )
    except Exception as exc:  # noqa: BLE001 - LLM 不可用一律降級
        log.warning("llm review skipped err=%s", type(exc).__name__)
        return f03_assessment.AssessmentOutcome([], [Finding(
            severity=Severity.INFO,
            code="F03.LLM_SKIPPED",
            title="LLM 佐證審查已略過",
            message="無法初始化或執行 LLM 判讀；其餘規則檢查不受影響。",
            source="llm",
        )], UsageSummary(), [])


def _submission_ok() -> Finding:
    """送件包規則檢查全數通過的 INFO Finding。"""
    return Finding(
        severity=Severity.INFO,
        code="SUBMISSION.OK",
        title="送件包規則檢查未發現問題",
        message="核心文件齊備、F01 必填完整、F02 規則通過、跨表一致。仍須人工覆核實質內容。",
    )


def _build_submission(
    files: dict[str, str | Path],
    supporting: list[str] | None,
    supporting_paths: list[str | Path] | None,
    review_cfg: dict,
    findings: list[Finding],
) -> Submission:
    """逐檔解析成 Submission；每檔包 try/except，失敗加 PARSE_ERROR 並繼續（介面層不崩）。"""
    presence = FilePresence(
        f01=files.get("f01") is not None,
        f02=files.get("f02") is not None,
        f03=files.get("f03") is not None,
    )
    sub = Submission(
        presence=presence,
        supporting_docs=list(supporting or []),
        supporting_paths=[str(path) for path in supporting_paths or []],
    )

    if presence.f01:
        log.debug("parsing form=F01")
        try:
            sub.f01 = parse_f01(files["f01"], review_cfg)
        except Exception as exc:  # noqa: BLE001 - 介面層需把解析錯誤友善呈現
            log.warning("parse failed form=F01 err=%s", type(exc).__name__)
            findings.append(_parse_error("F01", exc))
    if presence.f02:
        log.debug("parsing form=F02")
        try:
            sub.f02 = parse_f02(files["f02"])
        except Exception as exc:  # noqa: BLE001
            log.warning("parse failed form=F02 err=%s", type(exc).__name__)
            findings.append(_parse_error("F02", exc))
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

    if sub.supporting_paths:
        adapter = TsParserAdapter()
        sub.document_artifacts, sub.parser_usage = adapter.parse_many(sub.supporting_paths)

    if sub.f02 is not None:
        # 風險分數/等級以答案重算為準（確定性真值），重算失敗才退回檔內快取；
        # 驅動條件式佐證缺件與報告頂端風險顯示。快取與重算不符另由 F02.GRADE_MISMATCH 標示。
        sub.risk_score, sub.risk_grade = _f02_risk(sub.f02)
        sub.f02_filing_unit = sub.f02.filing_unit  # parse_f02 已讀 N2，免重複開檔

    # subject 只能來自 F01（F02 無系統名稱欄）
    sub.subject = sub.f01.subject if sub.f01 else None
    return sub


def _f02_risk(f02, cfg: dict | None = None) -> tuple[float | None, str | None]:
    """取 F02 固有風險（分數, 分級）：以答案重算為準，重算失敗才退回檔內快取，分數與分級同源。"""
    try:
        r = recompute(f02, cfg)
        return r.overall, r.grade
    except Exception:  # noqa: BLE001
        return f02.cached.overall, f02.cached.grade


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


def _decision_status(
    findings: list[Finding],
    *,
    documents=None,
    llm_required: bool = False,
    llm_completed: bool = True,
) -> DecisionStatus:
    if any(f.severity is Severity.ERROR for f in findings):
        return DecisionStatus.CHANGES_REQUIRED
    documents = documents or []
    if any(document.status in {
        DocumentParseStatus.UNSUPPORTED,
        DocumentParseStatus.UNREADABLE,
        DocumentParseStatus.PARSER_UNAVAILABLE,
    } for document in documents):
        return DecisionStatus.INCOMPLETE
    if any(f.severity is Severity.WARN for f in findings):
        return DecisionStatus.CHANGES_REQUIRED
    if any(f.code in {
        "F03.PROMPT_UNAVAILABLE",
        "F03.LLM_SKIPPED",
        "F03.ASSESSMENT_TRUNCATED",  # 有檢核項根本沒送審 → 不可判為通過
    } for f in findings):
        return DecisionStatus.MANUAL_REVIEW
    if any(f.code in {
        "F03.ASSESSMENT_UNDETERMINED",
        "F03.ASSESSMENT_LOW_CONFIDENCE",
    } for f in findings):
        return DecisionStatus.MANUAL_REVIEW
    if llm_required and not llm_completed:
        return DecisionStatus.MANUAL_REVIEW
    return DecisionStatus.APPROVED
