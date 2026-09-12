"""F03 檢核項的統一語意判讀：內容品質 + 佐證支持 + 法規符合性，一次呼叫判完。

三個面向刻意合在同一次模型呼叫，而非分成兩套 check：判斷「敘述是否具體」與
「敘述是否涵蓋條文要求」看的是同一段文字，拆開會讓同一項被判讀兩次、
等待時間加倍，且兩套結論可能互相矛盾。

法規條文摘錄來自凍結對照表（`regulations/`），是**審查基準**；
條文引用一律由 canonical section_path 決定性推導，模型回的 gap_refs 只作交集過濾，
避免模型憑空造出不存在的條號。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from govcheck.evidence import EvidenceHit, EvidenceRetriever, excerpt_id
from govcheck.llm.client import (
    LLMContextLengthError,
    LLMError,
    LLMOutputTruncatedError,
    parse_json_object,
)
from govcheck.logging_setup import get_logger
from govcheck.models import (
    Citation,
    ConflictStatus,
    DocumentArtifact,
    DocumentParseStatus,
    EvidenceStatus,
    F03Checklist,
    F03ChecklistItem,
    F03ItemAssessment,
    Finding,
    LayerStatus,
    PromptRunRef,
    RubricLine,
    Severity,
    UsageSummary,
    WorkflowStage,
)
from govcheck.prompts import (
    PromptGateway,
    PromptUnavailable,
    load_rubrics,
    pack_batches,
    profile_for_item,
)
from govcheck.regulations.mapping import format_section_ref
from govcheck.regulations.models import F03ItemRetrieval, RetrievalMap

log = get_logger("f03_assessment")

_SCHEMA_VERSION = "3"
_POLICY_VERSION = "f03-policy-v2"
_CACHE_MAX_ITEMS = 512
_ESTIMATED_OUTPUT_TOKENS_PER_ITEM = 512
_ASSESSMENT_CACHE: OrderedDict[str, F03ItemAssessment] = OrderedDict()
_CACHE_LOCK = threading.Lock()


class _CitationVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    unit_id: str
    excerpt_id: str


class _ItemVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    completeness: Literal["complete", "incomplete", "undetermined"]
    reasonableness: Literal[
        "sufficient", "reasonable", "vague", "unreasonable", "undetermined"
    ]
    # UNKNOWN is a backend-only safety state for incomplete retrieval. The model must judge only
    # the four observable evidence states; validation applies UNKNOWN when absence is not provable.
    evidence_status: Literal["supported", "partial", "not_found", "unreadable"]
    conflict_status: ConflictStatus
    # 未給條文摘錄時模型可能省略；預設 undetermined，且 _validate_results 會強制回 undetermined。
    regulation_gap: Literal["covered", "gap", "undetermined"] = "undetermined"
    gap_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    citations: list[_CitationVerdict] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    # 逐句檢核：與該項 rubric 等長、逐句對位的 true/false。沒給 rubric 的項次會是空陣列；
    # 長度對不上時 _validate_results 整筆丟棄（錯位的勾比沒有勾更誤導）。
    rubric_met: list[bool] = Field(default_factory=list)


class _BatchVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[_ItemVerdict]


_BATCH_SCHEMA = _BatchVerdict.model_json_schema()


@dataclass
class AssessmentOutcome:
    assessments: list[F03ItemAssessment]
    findings: list[Finding]
    usage: UsageSummary
    prompt_refs: list[PromptRunRef]
    # L2（檢索比對）與 L3（語意判讀）各自的狀態與耗時，供報告頁分層顯示。
    layers: list[LayerStatus] = field(default_factory=list)


def _layer(layer_id: str, label: str, state: str, elapsed_ms: int | None = None) -> LayerStatus:
    return LayerStatus(id=layer_id, label=label, state=state, elapsed_ms=elapsed_ms)


def _skipped_layers() -> list[LayerStatus]:
    """判讀沒跑到時的分層狀態。標「已略過」而非「已完成」——後者會謊稱判讀過了。"""
    return [
        _layer("L2", "法規／佐證檢索", "skipped"),
        _layer("L3", "敘述充分性判讀", "skipped"),
    ]


def _emit(progress, event: dict) -> None:
    if progress is None:
        return
    try:
        progress(event)
    except Exception as exc:  # noqa: BLE001 - 進度通知失敗不得中斷判讀
        log.warning("progress callback failed err=%s", type(exc).__name__)


def _emit_skipped_layers(progress) -> None:
    """把未執行的 L2／L3 也送成事件，避免串流端永遠停在 running/todo。"""
    for layer in _skipped_layers():
        _emit(progress, {
            "kind": "layer",
            "id": layer.id,
            "label": layer.label,
            "state": layer.state,
        })


def _add_truncated_usage(
    usage: UsageSummary,
    exc: LLMContextLengthError | LLMOutputTruncatedError,
    *,
    call_kind: Literal["primary", "escalation"],
) -> None:
    """Count a 200/length response before retrying; context rejections have no usage."""
    if not isinstance(exc, LLMOutputTruncatedError):
        return
    usage.review_llm_primary_calls += int(call_kind == "primary")
    usage.review_llm_escalation_calls += int(call_kind == "escalation")
    usage.input_tokens += exc.input_tokens
    usage.output_tokens += exc.output_tokens
    usage.latency_ms += exc.latency_ms


def run_all(
    checklist: F03Checklist,
    artifacts: list[DocumentArtifact],
    gateway: PromptGateway,
    *,
    workflow_stage: WorkflowStage,
    max_items: int = 30,
    max_batch_items: int = 8,
    top_k: int = 4,
    retriever: EvidenceRetriever | None = None,
    retrieval_map: RetrievalMap | None = None,
    max_sections_per_item: int = 3,
    max_excerpt_chars: int = 300,
    progress: Callable[[dict], None] | None = None,
) -> AssessmentOutcome:
    """對可判讀的檢核項做語意判讀；永不 raise（呼叫端不需為 LLM 故障設防）。

    retrieval_map：凍結法規對照表。缺檔或未映射的項次一律以 regulation_gap=undetermined
    收場——沒有審查基準就不宣稱符合，也不宣稱有缺口。
    progress：分層進度回呼。L2（檢索）通常一分鐘內完成、L3（模型）可能數分鐘，
    分開回報讓報告頁能先把檢索結果放出來。
    """
    all_targets = _targets(checklist, workflow_stage)
    targets = all_targets[:max_items]
    # 截斷必須留痕：超出上限的檢核項不會出現在任何 assessment/finding，
    # 若不提示，治理人員會誤以為整份檢核表都已判讀。
    truncated = _truncation_findings(len(all_targets) - len(targets), max_items)
    if not targets:
        _emit_skipped_layers(progress)
        return AssessmentOutcome([], truncated, UsageSummary(), [], _skipped_layers())

    t_l2 = time.perf_counter()
    _emit(progress, {"kind": "layer", "id": "L2", "state": "running"})
    canonical = retrieval_map.f03_items if retrieval_map is not None else {}
    regs = {
        item.item_id: _regulation_payload(
            canonical[item.item_id], max_sections_per_item, max_excerpt_chars
        )
        for item in targets
        if item.item_id in canonical
    }
    refs_by_item = {
        item.item_id: _canonical_refs(canonical[item.item_id], max_sections_per_item)
        for item in targets
        if item.item_id in canonical
    }
    # 判準會改變模型輸入與逐句結果，因此必須在內容去重與快取鍵生成之前載入。
    # 缺檔時是空 dict，既有無-rubric 路徑維持不變。
    rubrics = load_rubrics()
    # 模板竄改比對放在判讀前：即使後續模型呼叫全部失敗，這筆規則式警示仍會出現在報告裡。
    template_findings = [
        _template_modified_finding(item)
        for item in targets
        if item.item_id in canonical and _template_modified(item, canonical[item.item_id])
    ]
    try:
        unique_targets, duplicate_of = _deduplicate_targets(
            targets, workflow_stage, regs, rubrics
        )
        prompt_ids = {
            f"govcheck.f03.{profile_for_item(item.item_id)}" for item in unique_targets
        }
    except KeyError as exc:
        log.warning("f03 prompt route unavailable err=%s", type(exc).__name__)
        _emit_skipped_layers(progress)
        return AssessmentOutcome(
            [_undetermined(item, artifacts) for item in targets],
            [_unavailable_finding(), *template_findings, *truncated],
            UsageSummary(),
            [],
            _skipped_layers(),
        )
    prompt_ids.add("govcheck.f03.escalation")
    try:
        prompt_run = gateway.begin_run(prompt_ids)
    except (PromptUnavailable, KeyError, ValueError) as exc:
        log.warning("f03 prompt run unavailable err=%s", type(exc).__name__)
        assessments = [_undetermined(item, artifacts) for item in targets]
        _emit_skipped_layers(progress)
        return AssessmentOutcome(
            assessments,
            [_unavailable_finding(), *template_findings, *truncated],
            UsageSummary(),
            [],
            _skipped_layers(),
        )

    # Prompt readiness precedes retrieval so a prompt outage cannot waste an
    # embedding call or send document chunks to an otherwise unusable pipeline.
    retriever = retriever or EvidenceRetriever()
    prepared, retrieval_incomplete_item_ids = _prepare_many(
        unique_targets, artifacts, retriever, top_k, workflow_stage
    )
    usage = UsageSummary(evidence_embedding_calls=retriever.embedding_calls)
    # L2（法規條文組裝＋佐證檢索）到此為止，通常一分鐘內；接下來的模型呼叫才是慢的部分。
    l2_ms = round((time.perf_counter() - t_l2) * 1000)
    _emit(progress, {"kind": "layer", "id": "L2", "state": "done", "elapsed_ms": l2_ms})
    _emit(progress, {"kind": "layer", "id": "L3", "state": "running"})
    t_l3 = time.perf_counter()
    assessments: list[F03ItemAssessment] = []
    cached_item_ids: set[str] = set()
    cache_keys: dict[str, str] = {}
    misses = []
    for item, hits in prepared:
        key = _assessment_cache_key(
            item,
            hits,
            workflow_stage,
            gateway,
            prompt_run,
            regs,
            rubrics,
            retrieval_complete=item.item_id not in retrieval_incomplete_item_ids,
        )
        cache_keys[item.item_id] = key
        cached = _cache_get(key) if key else None
        if cached is None:
            misses.append((item, hits))
        else:
            assessments.append(cached.model_copy(deep=True))
            cached_item_ids.add(item.item_id)
            usage.cache_hits += 1
    grouped: dict[str, list[tuple[F03ChecklistItem, list[EvidenceHit]]]] = {}
    for item, hits in misses:
        grouped.setdefault(profile_for_item(item.item_id), []).append((item, hits))
    # Input and output both constrain a batch. Reserving max_tokens protects the context window,
    # while this item ceiling prevents a valid 2K response budget from being asked to hold eight
    # verbose verdict objects and returning truncated JSON.
    output_item_limit = max(1, gateway.max_output_tokens // _ESTIMATED_OUTPUT_TOKENS_PER_ITEM)
    batch_item_limit = min(max(1, max_batch_items), output_item_limit)
    batches = []
    for profile, rows in grouped.items():
        prompt_id = f"govcheck.f03.{profile}"
        profile_batches = _pack(
            rows,
            max_items=batch_item_limit,
            fits=lambda candidate, prompt_id=prompt_id: gateway.fits_context(
                prompt_run,
                prompt_id,
                _variables(candidate, workflow_stage, regs, rubrics),
                schema=_BATCH_SCHEMA,
            ),
        )
        batches.extend((profile, batch) for batch in profile_batches)
    pending_primary = list(batches)
    while pending_primary:
        profile, batch = pending_primary.pop(0)
        prompt_id = f"govcheck.f03.{profile}"
        try:
            result, call_usage = gateway.execute(
                prompt_run,
                prompt_id,
                _variables(batch, workflow_stage, regs, rubrics),
                schema=_BATCH_SCHEMA,
            )
            usage.add(call_usage)
            verdict = _BatchVerdict.model_validate(parse_json_object(result.content))
            assessments.extend(_validate_results(
                batch, verdict, artifacts, prompt_id,
                prompt_run.templates[prompt_id].version, refs_by_item, rubrics,
                retrieval_incomplete_item_ids,
            ))
        except (LLMContextLengthError, LLMOutputTruncatedError) as exc:
            _add_truncated_usage(usage, exc, call_kind="primary")
            if len(batch) > 1:
                midpoint = len(batch) // 2
                pending_primary[0:0] = [
                    (profile, batch[:midpoint]),
                    (profile, batch[midpoint:]),
                ]
                log.info(
                    "f03 primary size retry split items=%d err=%s",
                    len(batch),
                    type(exc).__name__,
                )
                continue
            log.warning(
                "f03 primary item exceeds model size limit profile=%s err=%s",
                profile,
                type(exc).__name__,
            )
            assessments.extend(
                _undetermined(item, artifacts, profile=profile) for item, _ in batch
            )
        except (LLMError, ValidationError, ValueError, KeyError) as exc:
            log.warning("f03 assessment batch failed profile=%s err=%s", profile, type(exc).__name__)
            assessments.extend(_undetermined(item, artifacts, profile=profile) for item, _ in batch)

    escalated = [
        assessment for assessment in assessments
        if assessment.item_id not in cached_item_ids
        and assessment.assessed
        and (
            assessment.confidence < 0.6
            or assessment.conflict_status is ConflictStatus.CONFLICTING_SOURCES
        )
    ]
    if escalated:
        escalation_batches = _pack(
            escalated,
            max_items=batch_item_limit,
            fits=lambda candidate: gateway.fits_context(
                prompt_run,
                "govcheck.f03.escalation",
                _escalation_variables(
                    candidate, prepared, workflow_stage, regs, rubrics
                ),
                schema=_BATCH_SCHEMA,
            ),
        )
        replacements = {}
        pending_escalation = list(escalation_batches)
        while pending_escalation:
            escalation_batch = pending_escalation.pop(0)
            try:
                result, call_usage = gateway.execute(
                    prompt_run,
                    "govcheck.f03.escalation",
                    _escalation_variables(
                        escalation_batch, prepared, workflow_stage, regs, rubrics
                    ),
                    schema=_BATCH_SCHEMA,
                    call_kind="escalation",
                )
                usage.add(call_usage)
                verdict = _BatchVerdict.model_validate(parse_json_object(result.content))
                # 只替換「升級判讀確實回覆」的項目。否則 _validate_results 會為缺漏的
                # item_id 生出 _undetermined，反而把可用的初判覆寫成未判讀。
                escalated_ids = {row.item_id for row in escalation_batch}
                answered_ids = {row.item_id for row in verdict.results} & escalated_ids
                if len(answered_ids) < len(escalated_ids):
                    log.warning(
                        "f03 escalation partial answered=%d expected=%d",
                        len(answered_ids),
                        len(escalated_ids),
                    )
                batch_prepared = [
                    (item, hits)
                    for item, hits in prepared
                    if item.item_id in answered_ids
                ]
                replacements.update({
                    row.item_id: row for row in _validate_results(
                        batch_prepared,
                        verdict,
                        artifacts,
                        "govcheck.f03.escalation",
                        prompt_run.templates["govcheck.f03.escalation"].version,
                        refs_by_item,
                        rubrics,
                        retrieval_incomplete_item_ids,
                    )
                })
            except (LLMContextLengthError, LLMOutputTruncatedError) as exc:
                _add_truncated_usage(usage, exc, call_kind="escalation")
                if len(escalation_batch) > 1:
                    midpoint = len(escalation_batch) // 2
                    pending_escalation[0:0] = [
                        escalation_batch[:midpoint],
                        escalation_batch[midpoint:],
                    ]
                    log.info(
                        "f03 escalation size retry split items=%d err=%s",
                        len(escalation_batch),
                        type(exc).__name__,
                    )
                    continue
                # Keep the usable primary assessment for this item. A context failure must never
                # replace it with an unassessed placeholder.
                log.warning(
                    "f03 escalation item exceeds model size limit err=%s",
                    type(exc).__name__,
                )
            except (LLMError, ValidationError, ValueError, KeyError) as exc:
                log.warning(
                    "f03 escalation batch failed items=%d err=%s",
                    len(escalation_batch),
                    type(exc).__name__,
                )
        assessments = [replacements.get(row.item_id, row) for row in assessments]

    for assessment in assessments:
        key = cache_keys.get(assessment.item_id)
        if (
            key
            and assessment.item_id not in cached_item_ids
            and assessment.confidence >= 0.6
            and assessment.conflict_status is not ConflictStatus.CONFLICTING_SOURCES
            and assessment.evidence_status is not EvidenceStatus.UNKNOWN
            and assessment.retrieval_complete
        ):
            _cache_put(key, assessment)

    # Exact duplicate content within one review profile consumes one model slot,
    # while the stable API still returns an assessment for every F03 item.
    by_item_id = {assessment.item_id: assessment for assessment in assessments}
    expanded = []
    for item in targets:
        canonical_id = duplicate_of.get(item.item_id, item.item_id)
        assessment = by_item_id.get(canonical_id)
        if assessment is None:
            assessment = _undetermined(item, artifacts)
        elif canonical_id != item.item_id:
            assessment = assessment.model_copy(update={"item_id": item.item_id})
        expanded.append(assessment)
    assessments = expanded

    per_item = [_findings(assessment) for assessment in assessments]
    findings = [finding for rows in per_item for finding in rows]
    gaps = sum(1 for a in assessments if a.assessed and a.regulation_gap == "gap")
    summary = (f"共評估 {len(assessments)} 項；需補正或人工確認 "
               f"{sum(bool(rows) for rows in per_item)} 項")
    summary += f"，其中法規符合性缺口 {gaps} 項。" if regs else "。（本次無法規條文對照，未做符合性判讀）"
    findings.append(Finding(
        severity=Severity.INFO,
        code="F03.ASSESSMENT_SUMMARY",
        title="F03 結構化審查摘要",
        message=summary,
        source="llm",
    ))
    findings += template_findings
    findings += truncated
    l3_ms = round((time.perf_counter() - t_l3) * 1000)
    _emit(progress, {"kind": "layer", "id": "L3", "state": "done", "elapsed_ms": l3_ms})
    layers = [
        _layer("L2", "法規／佐證檢索", "done", l2_ms),
        _layer("L3", "敘述充分性判讀", "done", l3_ms),
    ]
    return AssessmentOutcome(assessments, findings, usage, list(prompt_run.refs), layers)


def _truncation_findings(dropped: int, max_items: int) -> list[Finding]:
    """超出 max_items 的檢核項完全不會進入判讀，必須明示未送審數量。"""
    if dropped <= 0:
        return []
    log.info("f03 assessment max_items limit=%d truncated=%d", max_items, dropped)
    return [Finding(
        severity=Severity.INFO,
        code="F03.ASSESSMENT_TRUNCATED",
        title="超過單次判讀上限，部分檢核項未送審",
        message=(f"超過單次上限 {max_items} 項，本次未送審 {dropped} 項，"
                 "這些項目未經模型判讀，請人工覆核。"),
        source="llm",
    )]


def _targets(checklist: F03Checklist, stage: WorkflowStage) -> list[F03ChecklistItem]:
    if checklist is None or not checklist.sheet_present:
        return []
    # 送審條件與 F03Checklist.roster() 共用同一組判斷，避免名冊說「可判讀」卻查無 assessment。
    return [
        item for item in checklist.items
        if item.coverage_for_stage(stage).coverage == "assessable"
    ]


def _deduplicate_targets(
    targets: list[F03ChecklistItem],
    stage: WorkflowStage,
    regs: dict[str, list[dict]],
    rubrics: dict[str, list[str]] | None = None,
) -> tuple[list[F03ChecklistItem], dict[str, str]]:
    unique = []
    canonical_by_signature: dict[str, str] = {}
    duplicate_of: dict[str, str] = {}
    for item in targets:
        # 法規條文必須進簽章：兩項內容相同但對應不同條文時，判讀結果不可互相沿用，
        # 否則 A 項的條文缺口會被複製到適用其他條文的 B 項上。
        signature = hashlib.sha256(
            json.dumps([
                profile_for_item(item.item_id),
                item.description,
                item.evidence_proposal,
                item.evidence_golive if stage is not WorkflowStage.PROPOSAL else None,
                regs.get(item.item_id, []),
                (rubrics or {}).get(item.item_id, []),
            ], ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        canonical_id = canonical_by_signature.get(signature)
        if canonical_id is None:
            canonical_by_signature[signature] = item.item_id
            unique.append(item)
        else:
            duplicate_of[item.item_id] = canonical_id
    return unique, duplicate_of


def _prepare_many(
    items: list[F03ChecklistItem],
    artifacts: list[DocumentArtifact],
    retriever: EvidenceRetriever,
    top_k: int,
    stage: WorkflowStage,
) -> tuple[
    list[tuple[F03ChecklistItem, list[EvidenceHit]]],
    set[str],
]:
    queries = [
        " ".join(filter(None, [
            item.description,
            item.evidence_proposal,
            item.evidence_golive if stage is not WorkflowStage.PROPOSAL else None,
        ]))
        for item in items
    ]
    hits = retriever.retrieve_many(queries, artifacts, top_k=top_k)
    incomplete_indices = getattr(
        retriever, "last_incomplete_query_indices", frozenset()
    )
    incomplete_item_ids = {
        item.item_id
        for index, item in enumerate(items)
        if index in incomplete_indices
    }
    return list(zip(items, hits)), incomplete_item_ids


# 分批打包已上提為 prompts.gateway.pack_batches；保留舊名供既有呼叫端與測試使用。
_pack = pack_batches


def _variables(
    batch: list[tuple[F03ChecklistItem, list[EvidenceHit]]],
    stage: WorkflowStage,
    regs: dict[str, list[dict]],
    rubrics: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    return {
        "workflow_stage": stage.value,
        "items_json": json.dumps(
            [_item_payload(item, hits, stage, regs, rubrics) for item, hits in batch],
            ensure_ascii=False,
        ),
        "evidence_json": json.dumps(_unique_evidence_payloads(batch), ensure_ascii=False),
        "policy_version": _POLICY_VERSION,
        "output_schema_version": _SCHEMA_VERSION,
    }


def _escalation_variables(
    assessments: list[F03ItemAssessment],
    prepared: list[tuple[F03ChecklistItem, list[EvidenceHit]]],
    stage: WorkflowStage,
    regs: dict[str, list[dict]],
    rubrics: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    assessment_ids = {assessment.item_id for assessment in assessments}
    variables = _variables(
        [(item, hits) for item, hits in prepared if item.item_id in assessment_ids],
        stage,
        regs,
        rubrics,
    )
    variables["primary_assessments_json"] = json.dumps(
        [_primary_assessment_payload(assessment) for assessment in assessments],
        ensure_ascii=False,
    )
    return variables


def _primary_assessment_payload(assessment: F03ItemAssessment) -> dict:
    """Keep only first-pass fields that the escalation prompt actually compares."""
    return {
        "item_id": assessment.item_id,
        "completeness": assessment.completeness,
        "reasonableness": assessment.reasonableness,
        "evidence_status": assessment.evidence_status.value,
        "retrieval_complete": assessment.retrieval_complete,
        "conflict_status": assessment.conflict_status.value,
        "regulation_gap": assessment.regulation_gap,
        "regulation_refs": assessment.regulation_refs,
        "confidence": assessment.confidence,
        "citations": [
            {
                "document_id": citation.document_id,
                "unit_id": citation.unit_id,
                "excerpt_id": citation.excerpt_id,
            }
            for citation in assessment.citations
        ],
        "recommendations": assessment.recommendations,
        "rubric_met": [line.met for line in assessment.rubric],
    }


def _unique_evidence_payloads(
    batch: list[tuple[F03ChecklistItem, list[EvidenceHit]]],
) -> list[dict]:
    """Deduplicate identical windows and retain the items each window is evidence for."""
    output: list[dict] = []
    index_by_window: dict[tuple[str, str, str], int] = {}
    for item, hits in batch:
        for hit in hits:
            # Two items can legitimately select different verbatim windows from the same source
            # unit. item_ids prevents those windows from being
            # accidentally applied to the wrong checklist item in a multi-item prompt.
            key = (hit.document_id, hit.unit_id, hit.content)
            existing = index_by_window.get(key)
            if existing is not None:
                if item.item_id not in output[existing]["item_ids"]:
                    output[existing]["item_ids"].append(item.item_id)
                continue
            index_by_window[key] = len(output)
            output.append(_evidence_payload(hit, item_ids=[item.item_id]))
    return output


def _item_payload(
    item: F03ChecklistItem,
    hits: list[EvidenceHit],
    stage: WorkflowStage | None = None,
    regs: dict[str, list[dict]] | None = None,
    rubrics: dict[str, list[str]] | None = None,
) -> dict:
    return {
        "item_id": item.item_id,
        "description": item.description or "",
        "proposal": item.evidence_proposal or "",
        "golive": (
            item.evidence_golive or ""
            if stage is not WorkflowStage.PROPOSAL
            else ""
        ),
        "allowed_citations": [
            {
                "document_id": hit.document_id,
                "unit_id": hit.unit_id,
                "excerpt_id": _excerpt_id(hit),
            }
            for hit in hits
        ],
        # 法規條文摘錄＝符合性判讀的基準。空陣列代表本項無條文對照，模型不得妄下符合性結論。
        "regulations": (regs or {}).get(item.item_id, []),
        # 審查員檢核句：人工撰寫的判準，模型只逐句回「說明有沒有回應這一句」。
        # 空陣列代表本項無判準，rubric_met 也應為空。
        "rubric": (rubrics or {}).get(item.item_id, []),
    }


def _regulation_payload(
    canonical: F03ItemRetrieval,
    max_sections: int,
    max_excerpt_chars: int,
) -> list[dict]:
    """canonical 條文摘錄 → prompt payload（受段數與字數上限節流）。"""
    sections = canonical.sections[:max_sections]
    return [
        {
            "ref": format_section_ref(section.reg_code, section.section_path),
            "title": section.title,
            "excerpt": section.excerpt[:max_excerpt_chars],
        }
        for section in sections
    ]


def _canonical_refs(canonical: F03ItemRetrieval, max_sections: int) -> list[str]:
    """本項可用的條文引用顯示字串；報告只會出現這個清單裡的條號。"""
    return [
        format_section_ref(section.reg_code, section.section_path)
        for section in canonical.sections[:max_sections]
    ]


def _normalize_ref(text: str | None) -> str:
    """比對用正規化：NFKC（全形→半形）、去頭尾空白、內部連續空白壓成單一空格。"""
    if not text:
        return ""
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _display_refs(all_refs: list[str], gap_refs: list[str]) -> list[str]:
    """模型指出的缺口條號 ∩ canonical 引用；交集為空則列出全部 canonical 引用。

    顯示來源永遠是 canonical，模型只能從中挑選，不能新增——否則報告會出現不存在的條號。
    """
    if gap_refs:
        wanted = {_normalize_ref(ref) for ref in gap_refs}
        filtered = [ref for ref in all_refs if _normalize_ref(ref) in wanted]
        if filtered:
            return filtered
    return all_refs


def _template_modified(item: F03ChecklistItem, canonical: F03ItemRetrieval) -> bool:
    """上傳的 L 欄「規範參考」或檢查項目描述與官方模板不符 → True。

    上傳側為空時 skip 比對（欄位未填不等於被改）。
    """
    uploaded_ref = _normalize_ref(item.regulation_ref_raw)
    if uploaded_ref and uploaded_ref != _normalize_ref(canonical.canonical_ref_raw):
        return True
    uploaded_desc = _normalize_ref(item.description)
    return bool(uploaded_desc and uploaded_desc != _normalize_ref(canonical.canonical_description))


def _template_modified_finding(item: F03ChecklistItem) -> Finding:
    return Finding(
        severity=Severity.WARN,
        code="F03.TEMPLATE_REF_MODIFIED",
        title="F03 模板欄位疑似被修改",
        message=(f"項次 {item.item_id} 的「規範參考」或「檢查項目」與官方模板不符，"
                 "疑似被提案單位修改；判讀仍以官方模板為準，請人工核查。"),
        location=item.loc,
        source="rule",
    )


def _evidence_payload(hit: EvidenceHit, *, item_ids: list[str]) -> dict:
    return {
        "item_ids": item_ids,
        "document_id": hit.document_id,
        "unit_id": hit.unit_id,
        "excerpt_id": _excerpt_id(hit),
        "locator_type": hit.locator_type,
        "locator_value": hit.locator_value,
        "content": hit.content,
        "untrusted_data": True,
    }


# excerpt 識別已上提為 evidence.retrieval.excerpt_id；保留舊名供既有呼叫端與測試使用。
_excerpt_id = excerpt_id


def _rubric_lines(lines: list[str], met: list[bool]) -> list[RubricLine]:
    """把人工判準與模型的逐句回覆配對；長度對不上就整筆丟棄。

    錯位的勾比沒有勾更誤導——治理人員會拿它去核對原文，發現對不上就再也不會信任
    這個區塊。寧可少一個區塊，不要一個會說謊的區塊。
    """
    if not lines or len(met) != len(lines):
        if lines and met:
            log.warning("rubric length mismatch lines=%d met=%d", len(lines), len(met))
        return []
    return [RubricLine(text=text, met=bool(flag)) for text, flag in zip(lines, met)]


def _validate_results(
    batch: list[tuple[F03ChecklistItem, list[EvidenceHit]]],
    response: _BatchVerdict,
    artifacts: list[DocumentArtifact],
    prompt_id: str,
    prompt_version: str,
    refs_by_item: dict[str, list[str]],
    rubrics: dict[str, list[str]] | None = None,
    retrieval_incomplete_item_ids: set[str] | None = None,
) -> list[F03ItemAssessment]:
    verdicts = {row.item_id: row for row in response.results}
    incomplete_item_ids = retrieval_incomplete_item_ids or set()
    output = []
    for item, hits in batch:
        verdict = verdicts.get(item.item_id)
        if verdict is None:
            output.append(_undetermined(item, artifacts, profile=profile_for_item(item.item_id)))
            continue
        allowed_excerpts = {_excerpt_id(hit): hit for hit in hits}
        citations = []
        for citation in verdict.citations:
            source = (citation.document_id, citation.unit_id)
            hit = allowed_excerpts.get(citation.excerpt_id)
            if hit is not None and (hit.document_id, hit.unit_id) != source:
                hit = None
            if hit is not None:
                citations.append(Citation(
                    document_id=hit.document_id,
                    unit_id=hit.unit_id,
                    excerpt_id=_excerpt_id(hit),
                    locator_type=hit.locator_type,
                    locator_value=hit.locator_value,
                    quote=hit.quote or " ".join(hit.content.split())[:180],
                ))
        evidence_status = EvidenceStatus(verdict.evidence_status)
        conflict_status = verdict.conflict_status
        if evidence_status is EvidenceStatus.SUPPORTED and not citations:
            evidence_status = EvidenceStatus.PARTIAL if hits else _absence_status(artifacts)
        if conflict_status in {
            ConflictStatus.CONTRADICTED,
            ConflictStatus.CONFLICTING_SOURCES,
        } and not citations:
            conflict_status = ConflictStatus.UNKNOWN
        retrieval_complete = item.item_id not in incomplete_item_ids
        if not retrieval_complete:
            # A bounded or failed semantic pass can miss a relevant chunk that has no lexical
            # overlap. Do not convert that incomplete search into the stronger NOT_FOUND claim.
            if not citations:
                evidence_status = EvidenceStatus.UNKNOWN
            if conflict_status is ConflictStatus.CONSISTENT:
                conflict_status = ConflictStatus.UNKNOWN
        # 沒有條文對照就沒有審查基準：模型無論回什麼，一律降回 undetermined，
        # 避免在「不知道要比對什麼」的情況下宣稱符合或有缺口。
        canonical_refs = refs_by_item.get(item.item_id, [])
        regulation_gap = verdict.regulation_gap if canonical_refs else "undetermined"
        regulation_refs = (
            _display_refs(canonical_refs, verdict.gap_refs)
            if regulation_gap == "gap"
            else []
        )
        output.append(F03ItemAssessment(
            item_id=item.item_id,
            profile=profile_for_item(item.item_id),
            completeness=verdict.completeness,
            reasonableness=verdict.reasonableness,
            evidence_status=evidence_status,
            retrieval_complete=retrieval_complete,
            conflict_status=conflict_status,
            regulation_gap=regulation_gap,
            regulation_refs=regulation_refs,
            confidence=verdict.confidence,
            citations=citations,
            recommendations=verdict.recommendations,
            rubric=_rubric_lines((rubrics or {}).get(item.item_id, []), verdict.rubric_met),
            prompt_id=prompt_id,
            prompt_version=prompt_version,
        ))
    return output


def _absence_status(artifacts: list[DocumentArtifact]) -> EvidenceStatus:
    if artifacts and not any(a.status is DocumentParseStatus.PARSED for a in artifacts):
        return EvidenceStatus.UNREADABLE
    return EvidenceStatus.NOT_FOUND


def _undetermined(
    item: F03ChecklistItem,
    artifacts: list[DocumentArtifact],
    *,
    profile: str | None = None,
) -> F03ItemAssessment:
    return F03ItemAssessment(
        item_id=item.item_id,
        profile=profile or _safe_profile(item.item_id),
        evidence_status=_absence_status(artifacts),
        assessed=False,  # 未判讀：下游不得據此發出「佐證不足」等實質合規 finding
        recommendations=["模型判讀未完成，請人工覆核。"],
    )


def _safe_profile(item_id: str) -> str:
    try:
        return profile_for_item(item_id)
    except KeyError:
        return "unmapped"


def _assessment_cache_key(
    item: F03ChecklistItem,
    hits: list[EvidenceHit],
    stage: WorkflowStage,
    gateway: PromptGateway,
    prompt_run,
    regs: dict[str, list[dict]],
    rubrics: dict[str, list[str]] | None = None,
    *,
    retrieval_complete: bool = True,
) -> str:
    if not gateway.cache_namespace:
        return ""
    profile = profile_for_item(item.item_id)
    prompt_id = f"govcheck.f03.{profile}"
    template = prompt_run.templates[prompt_id]
    escalation = prompt_run.templates["govcheck.f03.escalation"]
    payload = {
        "namespace": gateway.cache_namespace,
        "stage": stage.value,
        "policy": _POLICY_VERSION,
        "schema": _SCHEMA_VERSION,
        "prompt_id": prompt_id,
        "prompt_version": template.version,
        "prompt_hash": template.content_hash,
        # 最終 assessment 可能來自低信心升級。升級模板改版時，即使主要模板未變，
        # 也不得命中舊的最終結果。
        "escalation_prompt_version": escalation.version,
        "escalation_prompt_hash": escalation.content_hash,
        "retrieval_complete": retrieval_complete,
        "item": _item_payload(item, hits, stage, regs, rubrics),
        "evidence_hashes": [
            hashlib.sha256(hit.content.encode()).hexdigest() for hit in hits
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _cache_get(key: str) -> F03ItemAssessment | None:
    with _CACHE_LOCK:
        value = _ASSESSMENT_CACHE.get(key)
        if value is not None:
            _ASSESSMENT_CACHE.move_to_end(key)
        return value


def _cache_put(key: str, value: F03ItemAssessment) -> None:
    with _CACHE_LOCK:
        _ASSESSMENT_CACHE[key] = value.model_copy(deep=True)
        _ASSESSMENT_CACHE.move_to_end(key)
        while len(_ASSESSMENT_CACHE) > _CACHE_MAX_ITEMS:
            _ASSESSMENT_CACHE.popitem(last=False)


def clear_assessment_cache() -> None:
    """Test/operations hook; cached entries never contain raw filenames or credentials."""
    with _CACHE_LOCK:
        _ASSESSMENT_CACHE.clear()


def _findings(assessment: F03ItemAssessment) -> list[Finding]:
    location = f"項次 {assessment.item_id}"
    output = []
    if not assessment.assessed:
        # 模型從未判讀此項（端點失敗 / 回應缺漏）。只發 INFO 降級訊號，
        # 比照 F03.LLM_SKIPPED / RAG.SKIPPED；絕不因端點故障而宣稱「佐證不足」。
        return [Finding(
            severity=Severity.INFO,
            code="F03.ASSESSMENT_UNDETERMINED",
            title="內容語意判讀未完成",
            message="此項目未取得模型判讀結果（判讀未執行或回應缺漏），請人工覆核。",
            location=location,
            source="llm",
        )]
    if (
        assessment.completeness == "undetermined"
        or assessment.reasonableness == "undetermined"
    ):
        output.append(Finding(
            severity=Severity.INFO,
            code="F03.ASSESSMENT_UNDETERMINED",
            title="內容語意判讀未完成",
            message="此項目無法完成模型判讀，請人工覆核。",
            location=location,
            source="llm",
        ))
    if assessment.confidence < 0.6:
        output.append(Finding(
            severity=Severity.INFO,
            code="F03.ASSESSMENT_LOW_CONFIDENCE",
            title="模型判讀信心偏低",
            message="此項目需人工覆核；低信心結果不得直接作為通過依據。",
            location=location,
            source="llm",
        ))
    if assessment.completeness == "incomplete":
        output.append(_finding("F03.ASSESSMENT_INCOMPLETE", "內容不完整", assessment, location))
    if assessment.reasonableness == "vague":
        output.append(_finding("F03.ASSESSMENT_VAGUE", "內容過於空泛", assessment, location))
    if assessment.reasonableness == "unreasonable":
        # 比 vague 更嚴重（敘述不相干／自相矛盾／做法無法達成控制目的）。狀態總覽表已把它
        # 標成「內容需補強」，若這裡不發 finding，治理人員會看到警示格卻查無原因。
        output.append(_finding(
            "F03.ASSESSMENT_UNREASONABLE", "內容與檢核項要求不符", assessment, location
        ))
    if not assessment.retrieval_complete:
        output.append(Finding(
            severity=Severity.INFO,
            code="F03.EVIDENCE_RETRIEVAL_INCOMPLETE",
            title="佐證語意檢索未完整",
            message=(
                "佐證量超過本次語意檢索上限或 embedding 服務未完成；"
                "空結果不代表未提供佐證，請人工覆核或使用預建向量索引。"
            ),
            location=location,
            source="llm",
        ))
    if assessment.evidence_status in {
        EvidenceStatus.PARTIAL,
        EvidenceStatus.NOT_FOUND,
        EvidenceStatus.UNREADABLE,
    }:
        output.append(_finding("F03.EVIDENCE_NOT_SUPPORTED", "佐證不足或無法讀取", assessment, location))
    if assessment.conflict_status is ConflictStatus.CONTRADICTED:
        output.append(_finding("F03.EVIDENCE_CONTRADICTED", "佐證內容可能衝突", assessment, location))
    if assessment.conflict_status is ConflictStatus.CONFLICTING_SOURCES:
        output.append(_finding("F03.EVIDENCE_SOURCE_CONFLICT", "佐證文件彼此衝突", assessment, location))
    if assessment.regulation_gap == "gap":
        refs = "、".join(assessment.regulation_refs) or "（對照表未提供條號）"
        message = f"佐證未涵蓋下列條文要求：{refs}。"
        if assessment.recommendations:
            message += "；".join(assessment.recommendations)
        output.append(Finding(
            severity=Severity.WARN,
            code="F03.REGULATION_GAP",
            title="佐證與法規條文存在符合性缺口",
            message=message,
            location=location,
            source="llm",
        ))
    return output


def _finding(code: str, title: str, assessment: F03ItemAssessment, location: str) -> Finding:
    message = "；".join(assessment.recommendations) or "請補充內容並由治理人員人工覆核。"
    return Finding(
        severity=Severity.WARN,
        code=code,
        title=title,
        message=message,
        location=location,
        source="llm",
    )


def _unavailable_finding() -> Finding:
    return Finding(
        severity=Severity.INFO,
        code="F03.PROMPT_UNAVAILABLE",
        title="F03 Prompt 尚未就緒",
        message="固定版本 Prompt 無法取得，本次語意判讀標示為未完成；規則檢查不受影響。",
        source="llm",
    )
