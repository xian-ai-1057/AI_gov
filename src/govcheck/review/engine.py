"""審查編排器：parse → 跑已註冊的 checks → 匯總成 ReviewReport。

- review_f02：單張 F02 風險評鑑。
- review_submission：整份送件包 = 缺件 + F01 必填 + F02 規則 + F03 佐證缺漏 +
  跨表一致性 +（啟用 LLM 時）F03 語意判讀（內容品質 + 佐證支持 + 法規符合性一次判完）。

新增檢查 = 新增 Check + 在此註冊，不動既有程式。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from pathlib import Path

from govcheck.checks.llm import f03_assessment
from govcheck.checks.rule import (
    cross_consistency,
    f01_rules,
    f02_reg_refs,
    f02_rules,
    f03_evidence_presence,
    f03_evidence_quality,
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
    LayerStatus,
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

# 漸進回傳的回呼型別。engine 不知道呼叫端要拿事件做什麼（串流給瀏覽器、寫 log、丟掉），
# 只負責在每一層結束時說一聲。回呼丟出的例外一律吞掉——通知失敗不該讓審查失敗。
ProgressFn = Callable[[dict], None]

# 三層檢查的顯示名稱。L1 是確定性規則（秒級）、L2 是檢索比對、L3 是語意判讀（最慢）。
# 分層的意義在於先完成的先能用：治理人員不必為了看一條「佐證過短」等五分鐘。
_LAYER_LABELS = {
    "L1": "規則檢查",
    "L2": "法規／佐證檢索",
    "L3": "敘述充分性判讀",
}


def _notify(progress: ProgressFn | None, event: dict) -> None:
    if progress is None:
        return
    try:
        progress(event)
    except Exception as exc:  # noqa: BLE001 - 進度通知失敗不得中斷審查
        log.warning("progress callback failed err=%s", type(exc).__name__)


def _layer(layer_id: str, state: str, elapsed_ms: int | None = None) -> LayerStatus:
    return LayerStatus(
        id=layer_id, label=_LAYER_LABELS[layer_id], state=state, elapsed_ms=elapsed_ms
    )


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
    progress: ProgressFn | None = None,
) -> ReviewReport:
    """整份送件包初步審查。

    files：{"f01": path, "f02": path, "f03": path}，未上傳者不放鍵（或值為 None）。
    supporting：佐證檔名清單（供缺件檢查做關鍵字比對）。
    supporting_paths：佐證檔實際路徑（供解析內容做語意判讀的佐證來源）。
    enable_llm：是否做 F03 語意判讀（端點不可用時自動降級，不影響規則檢查）。
    initial_findings：上游分類或路由階段已產生、須一併納入判定與稽核的 findings。
    progress：分層進度回呼（見 ProgressFn）。給 None 時行為與過去完全相同。
    """
    t0 = time.perf_counter()
    present = [k for k in ("f01", "f02", "f03") if files.get(k) is not None]
    log.info("review_start forms=%s enable_llm=%s", present, enable_llm)

    review_cfg = cfg or load_review_config()
    workflow_stage = WorkflowStage(workflow_stage)
    parse_findings = list(initial_findings or [])
    sub = _build_submission(files, supporting, supporting_paths, review_cfg, parse_findings)
    assessments = []
    prompt_refs = []
    usage = sub.parser_usage.model_copy(deep=True)
    review_id = uuid.uuid4().hex

    has_f03_checklist = sub.f03_checklist is not None and sub.f03_checklist.sheet_present
    reg_cfg = load_regulations_config()
    retrieval_map = _load_regulation_map(reg_cfg)

    # 檢查順序：缺件 → F01 → F02 → F03 規則（缺漏＋品質）→ F03 語意判讀
    #           → F02 法規查表 → 跨表一致性。
    # 規則式檢查拆成 llm 前／後兩段，是為了讓 L1 一結束就能組出可用的中間報告
    # （語意判讀要跑數分鐘，治理人員不該為了看一條「佐證過短」等那麼久）。
    # 最終 findings 仍依原順序拼回，串流與非串流兩條路徑產出完全相同的報告。
    _notify(progress, {"kind": "layer", "id": "L1", "state": "running"})
    pre_llm: list[Finding] = list(missing_docs.run_all(sub, review_cfg))
    if sub.f01 is not None:
        pre_llm += f01_rules.run_all(sub.f01, review_cfg)
    if sub.f02 is not None:
        pre_llm += f02_rules.run_all(sub.f02)  # F02 規則用自己的 scoring config
    if has_f03_checklist:
        pre_llm += f03_evidence_presence.run_all(
            sub.f03_checklist, review_cfg, workflow_stage
        )
        pre_llm += f03_evidence_quality.run_all(
            sub.f03_checklist, review_cfg, workflow_stage
        )

    post_llm: list[Finding] = []
    if sub.f02 is not None:
        # 純查表、零 LLM：F02-only 送件包也會列出觸發題對應的法規條文。
        post_llm += f02_reg_refs.run_all(
            sub.f02, retrieval_map, load_scoring_config(), reg_cfg["max_items"]
        )
    if retrieval_map is None and (sub.f02 is not None or has_f03_checklist):
        # 缺對照表 = 需求「專案內容是否合規」失去審查基準，必須明示，不可靜默通過。
        post_llm.append(_regulation_map_unavailable())
    post_llm += cross_consistency.run_all(sub, review_cfg)

    rule_ms = round((time.perf_counter() - t0) * 1000)
    layers = [_layer("L1", "done", rule_ms)]
    llm_runs = has_f03_checklist and enable_llm
    if not llm_runs:
        # 沒跑就標「已略過」，不可標「已完成」——那會讓使用者以為判讀過了。
        layers += [_layer("L2", "skipped"), _layer("L3", "skipped")]
    _notify(progress, {"kind": "layer", "id": "L1", "state": "done", "elapsed_ms": rule_ms})
    if not llm_runs:
        _notify(progress, {"kind": "layer", "id": "L2", "state": "skipped"})
        _notify(progress, {"kind": "layer", "id": "L3", "state": "skipped"})
    if progress is not None:
        _notify(progress, {
            "kind": "partial",
            "report": _assemble(
                sub, parse_findings + pre_llm + post_llm, [], [], usage,
                workflow_stage, has_f03_checklist,
                llm_completed=False, review_id=review_id,
                layers=layers if not llm_runs else [
                    _layer("L1", "done", rule_ms),
                    _layer("L2", "running"),
                    _layer("L3", "todo"),
                ],
            ),
        })

    llm_findings: list[Finding] = []
    if llm_runs:
        outcome = _run_f03_assessment(
            sub.f03_checklist,
            sub.document_artifacts,
            workflow_stage,
            retrieval_map,
            reg_cfg,
            progress=progress,
        )
        llm_findings = outcome.findings
        assessments = outcome.assessments
        prompt_refs = outcome.prompt_refs
        usage.add(outcome.usage)
        layers = [_layer("L1", "done", rule_ms), *outcome.layers]

    report = _assemble(
        sub, parse_findings + pre_llm + llm_findings + post_llm,
        assessments, prompt_refs, usage, workflow_stage, has_f03_checklist,
        llm_completed=bool(assessments) if enable_llm else False,
        review_id=review_id, layers=layers, enable_llm=enable_llm,
    )
    _notify(progress, {"kind": "final", "report": report})

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
        review_id=review_id,
        # 識別資訊是稽核軌跡的核心：沒有它就無法回溯這筆記錄對應哪一份送件。
        subject=report.subject,
        filing_unit=sub.f02_filing_unit,
        form_type=report.form_type,
        # 流程階段與固有風險分級是「這筆審查在治理流程哪一步、風險多高」的最小識別資訊，
        # 能力目錄把專案的送件／審查／核准串起來時靠它們，缺了只能回頭翻報告。
        workflow_stage=workflow_stage.value,
        risk_grade=report.risk_grade,
        risk_score=report.risk_score,
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


def _assemble(
    sub: Submission,
    findings: list[Finding],
    assessments: list,
    prompt_refs: list,
    usage: UsageSummary,
    workflow_stage: WorkflowStage,
    has_f03_checklist: bool,
    *,
    llm_completed: bool,
    review_id: str,
    layers: list[LayerStatus],
    enable_llm: bool = False,
) -> ReviewReport:
    """把 findings 與旁掛資料組成 ReviewReport。

    中間報告（L1 完成）與最終報告走同一條組裝路徑，兩者的排序、通過紀錄補寫、
    decision_status 推導規則都必然一致——分開寫遲早會漂移成「中間說通過、最終說退件」。
    """
    findings = sorted(findings, key=_severity_order)
    # 只要沒有 ERROR/WARN 就補一筆通過紀錄——僅有 INFO（法規引用、判讀摘要）時，
    # 報告不該讓治理人員自己推論「沒看到問題就是沒問題」。
    if not any(f.severity in (Severity.ERROR, Severity.WARN) for f in findings):
        findings.append(_submission_ok())
    return ReviewReport(
        form_type="送件包", subject=sub.subject, findings=findings,
        risk_score=sub.risk_score, risk_grade=sub.risk_grade,
        decision_status=_decision_status(
            findings,
            documents=sub.document_artifacts,
            llm_required=has_f03_checklist,
            llm_completed=llm_completed,
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
        layers=layers,
        review_id=review_id,
    )


def review_routed(
    files: dict[str, str | Path],
    supporting: list[str] | None = None,
    class_findings: list[Finding] | None = None,
    cfg: dict | None = None,
    enable_llm: bool = False,
    supporting_paths: list[str | Path] | None = None,
    workflow_stage: WorkflowStage | str = WorkflowStage.PROPOSAL,
    progress: ProgressFn | None = None,
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
        progress=progress,
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
    progress: ProgressFn | None = None,
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
            max_content_chars=llm_cfg.get("evidence_excerpt_chars", 600),
            max_embedding_chunks=llm_cfg.get("embedding_max_chunks", 512),
        )
        return f03_assessment.run_all(
            checklist,
            artifacts,
            gateway,
            workflow_stage=workflow_stage,
            max_items=llm_cfg["max_items"],
            max_batch_items=llm_cfg.get("batch_size", 8),
            top_k=llm_cfg.get("evidence_top_k", 4),
            retriever=retriever,
            retrieval_map=retrieval_map,
            max_sections_per_item=reg_cfg["max_sections_per_item"],
            max_excerpt_chars=reg_cfg["max_excerpt_chars"],
            progress=progress,
        )
    except Exception as exc:  # noqa: BLE001 - LLM 不可用一律降級
        log.warning("llm review skipped err=%s", type(exc).__name__)
        # 初始化就失敗 = L2/L3 兩層都沒跑到，必須標「已略過」而非「已完成」。
        skipped = [_layer("L2", "skipped"), _layer("L3", "skipped")]
        for layer in skipped:
            _notify(progress, {"kind": "layer", "id": layer.id, "state": "skipped"})
        return f03_assessment.AssessmentOutcome([], [Finding(
            severity=Severity.INFO,
            code="F03.LLM_SKIPPED",
            title="LLM 佐證審查已略過",
            message="無法初始化或執行 LLM 判讀；其餘規則檢查不受影響。",
            source="llm",
        )], UsageSummary(), [], skipped)


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
        "F03.EVIDENCE_RETRIEVAL_INCOMPLETE",
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
