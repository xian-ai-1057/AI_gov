"""Unified, stage-aware F03 content and supporting-evidence assessment."""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from govcheck.evidence import EvidenceHit, EvidenceRetriever
from govcheck.llm.client import LLMError, parse_json_object
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
    PromptRunRef,
    Severity,
    UsageSummary,
    WorkflowStage,
)
from govcheck.prompts import PromptGateway, PromptUnavailable, profile_for_item

log = get_logger("f03_assessment")

_SCHEMA_VERSION = "2"
_POLICY_VERSION = "f03-policy-v1"
_MAX_PROMPT_TOKENS = 6000
_CACHE_MAX_ITEMS = 512
_ASSESSMENT_CACHE: OrderedDict[str, F03ItemAssessment] = OrderedDict()
_CACHE_LOCK = threading.Lock()


class _CitationVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    unit_id: str


class _ItemVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    completeness: Literal["complete", "incomplete", "undetermined"]
    reasonableness: Literal[
        "sufficient", "reasonable", "vague", "unreasonable", "undetermined"
    ]
    evidence_status: EvidenceStatus
    conflict_status: ConflictStatus
    confidence: float = Field(ge=0.0, le=1.0)
    citations: list[_CitationVerdict] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


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


def run_all(
    checklist: F03Checklist,
    artifacts: list[DocumentArtifact],
    gateway: PromptGateway,
    *,
    workflow_stage: WorkflowStage,
    max_items: int = 30,
    top_k: int = 4,
    progress=None,
    retriever: EvidenceRetriever | None = None,
) -> AssessmentOutcome:
    all_targets = _targets(checklist, workflow_stage)
    targets = all_targets[:max_items]
    # 截斷必須留痕：超出上限的檢核項不會出現在任何 assessment/finding，
    # 若不提示，治理人員會誤以為整份檢核表都已判讀。
    truncated = _truncation_findings(len(all_targets) - len(targets), max_items)
    if not targets:
        return AssessmentOutcome([], truncated, UsageSummary(), [])
    try:
        unique_targets, duplicate_of = _deduplicate_targets(targets, workflow_stage)
        prompt_ids = {
            f"govcheck.f03.{profile_for_item(item.item_id)}" for item in unique_targets
        }
    except KeyError as exc:
        log.warning("f03 prompt route unavailable err=%s", type(exc).__name__)
        return AssessmentOutcome(
            [_undetermined(item, artifacts) for item in targets],
            [_unavailable_finding(), *truncated],
            UsageSummary(),
            [],
        )
    prompt_ids.add("govcheck.f03.escalation")
    try:
        prompt_run = gateway.begin_run(prompt_ids)
    except (PromptUnavailable, KeyError, ValueError) as exc:
        log.warning("f03 prompt run unavailable err=%s", type(exc).__name__)
        assessments = [_undetermined(item, artifacts) for item in targets]
        return AssessmentOutcome(
            assessments,
            [_unavailable_finding(), *truncated],
            UsageSummary(),
            [],
        )

    # Prompt readiness precedes retrieval so a prompt outage cannot waste an
    # embedding call or send document chunks to an otherwise unusable pipeline.
    retriever = retriever or EvidenceRetriever()
    prepared = _prepare_many(
        unique_targets, artifacts, retriever, top_k, workflow_stage
    )
    usage = UsageSummary(evidence_embedding_calls=retriever.embedding_calls)
    assessments: list[F03ItemAssessment] = []
    cached_item_ids: set[str] = set()
    cache_keys: dict[str, str] = {}
    misses = []
    for item, hits in prepared:
        key = _assessment_cache_key(item, hits, workflow_stage, gateway, prompt_run)
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
    batches = [
        (profile, batch)
        for profile, rows in grouped.items()
        for batch in _pack(rows)
    ]
    for done, (profile, batch) in enumerate(batches, start=1):
        prompt_id = f"govcheck.f03.{profile}"
        try:
            result, call_usage = gateway.execute(
                prompt_run,
                prompt_id,
                _variables(batch, workflow_stage),
                schema=_BATCH_SCHEMA,
            )
            usage.add(call_usage)
            verdict = _BatchVerdict.model_validate(parse_json_object(result.content))
            assessments.extend(_validate_results(
                batch, verdict, artifacts, prompt_id, prompt_run.templates[prompt_id].version
            ))
        except (LLMError, ValidationError, ValueError, KeyError) as exc:
            log.warning("f03 assessment batch failed profile=%s err=%s", profile, type(exc).__name__)
            assessments.extend(_undetermined(item, artifacts, profile=profile) for item, _ in batch)
        if progress is not None:
            progress({"stage": "llm", "label": "F03 結構化判讀", "done": done, "total": len(batches)})

    escalated = [
        assessment for assessment in assessments
        if assessment.item_id not in cached_item_ids
        and (
            assessment.confidence < 0.6
            or assessment.conflict_status is ConflictStatus.CONFLICTING_SOURCES
        )
    ]
    if escalated:
        try:
            result, call_usage = gateway.execute(
                prompt_run,
                "govcheck.f03.escalation",
                _escalation_variables(escalated, prepared, workflow_stage),
                schema=_BATCH_SCHEMA,
                call_kind="escalation",
            )
            usage.add(call_usage)
            verdict = _BatchVerdict.model_validate(parse_json_object(result.content))
            # 只替換「升級判讀確實回覆」的項目。否則 _validate_results 會為缺漏的
            # item_id 生出 _undetermined，反而把可用的初判覆寫成未判讀。
            escalated_ids = {row.item_id for row in escalated}
            answered_ids = {row.item_id for row in verdict.results} & escalated_ids
            if len(answered_ids) < len(escalated_ids):
                log.warning("f03 escalation partial answered=%d expected=%d",
                            len(answered_ids), len(escalated_ids))
            replacements = {
                row.item_id: row for row in _validate_results(
                    [(item, hits) for item, hits in prepared if item.item_id in answered_ids],
                    verdict,
                    artifacts,
                    "govcheck.f03.escalation",
                    prompt_run.templates["govcheck.f03.escalation"].version,
                )
            }
            assessments = [replacements.get(row.item_id, row) for row in assessments]
        except (LLMError, ValidationError, ValueError, KeyError) as exc:
            log.warning("f03 escalation failed err=%s", type(exc).__name__)

    for assessment in assessments:
        key = cache_keys.get(assessment.item_id)
        if (
            key
            and assessment.item_id not in cached_item_ids
            and assessment.confidence >= 0.6
            and assessment.conflict_status is not ConflictStatus.CONFLICTING_SOURCES
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
    findings.append(Finding(
        severity=Severity.INFO,
        code="F03.ASSESSMENT_SUMMARY",
        title="F03 結構化審查摘要",
        message=(f"共評估 {len(assessments)} 項；需補正或人工確認 "
                 f"{sum(bool(rows) for rows in per_item)} 項。"),
        source="llm",
    ))
    findings += truncated
    return AssessmentOutcome(assessments, findings, usage, list(prompt_run.refs))


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
) -> tuple[list[F03ChecklistItem], dict[str, str]]:
    unique = []
    canonical_by_signature: dict[str, str] = {}
    duplicate_of: dict[str, str] = {}
    for item in targets:
        signature = hashlib.sha256(
            json.dumps([
                profile_for_item(item.item_id),
                item.description,
                item.evidence_proposal,
                item.evidence_golive if stage is not WorkflowStage.PROPOSAL else None,
            ], ensure_ascii=False).encode()
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
) -> list[tuple[F03ChecklistItem, list[EvidenceHit]]]:
    queries = [
        " ".join(filter(None, [
            item.description,
            item.evidence_proposal,
            item.evidence_golive if stage is not WorkflowStage.PROPOSAL else None,
        ]))
        for item in items
    ]
    hits = retriever.retrieve_many(queries, artifacts, top_k=top_k)
    return list(zip(items, hits))


def _pack(
    rows: list[tuple[F03ChecklistItem, list[EvidenceHit]]],
) -> list[list[tuple[F03ChecklistItem, list[EvidenceHit]]]]:
    batches = []
    current = []
    tokens = 0
    for row in rows:
        estimate = len(json.dumps(_item_payload(*row), ensure_ascii=False)) // 2 + 1
        if current and tokens + estimate > _MAX_PROMPT_TOKENS:
            batches.append(current)
            current = []
            tokens = 0
        current.append(row)
        tokens += estimate
    if current:
        batches.append(current)
    return batches


def _variables(
    batch: list[tuple[F03ChecklistItem, list[EvidenceHit]]],
    stage: WorkflowStage,
) -> dict[str, object]:
    return {
        "workflow_stage": stage.value,
        "items_json": json.dumps(
            [_item_payload(item, hits, stage) for item, hits in batch], ensure_ascii=False
        ),
        "evidence_json": json.dumps([_evidence_payload(hit) for _, hits in batch for hit in hits], ensure_ascii=False),
        "policy_version": _POLICY_VERSION,
        "output_schema_version": _SCHEMA_VERSION,
    }


def _escalation_variables(
    assessments: list[F03ItemAssessment],
    prepared: list[tuple[F03ChecklistItem, list[EvidenceHit]]],
    stage: WorkflowStage,
) -> dict[str, object]:
    variables = _variables(
        [(item, hits) for item, hits in prepared if item.item_id in {a.item_id for a in assessments}],
        stage,
    )
    variables["primary_assessments_json"] = json.dumps(
        [assessment.model_dump(mode="json") for assessment in assessments], ensure_ascii=False
    )
    return variables


def _item_payload(
    item: F03ChecklistItem,
    hits: list[EvidenceHit],
    stage: WorkflowStage | None = None,
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
            {"document_id": hit.document_id, "unit_id": hit.unit_id} for hit in hits
        ],
    }


def _evidence_payload(hit: EvidenceHit) -> dict:
    return {
        "document_id": hit.document_id,
        "unit_id": hit.unit_id,
        "locator_type": hit.locator_type,
        "locator_value": hit.locator_value,
        "content": hit.content,
        "untrusted_data": True,
    }


def _validate_results(
    batch: list[tuple[F03ChecklistItem, list[EvidenceHit]]],
    response: _BatchVerdict,
    artifacts: list[DocumentArtifact],
    prompt_id: str,
    prompt_version: str,
) -> list[F03ItemAssessment]:
    verdicts = {row.item_id: row for row in response.results}
    output = []
    for item, hits in batch:
        verdict = verdicts.get(item.item_id)
        if verdict is None:
            output.append(_undetermined(item, artifacts, profile=profile_for_item(item.item_id)))
            continue
        allowed = {(hit.document_id, hit.unit_id): hit for hit in hits}
        citations = []
        for citation in verdict.citations:
            hit = allowed.get((citation.document_id, citation.unit_id))
            if hit is not None:
                citations.append(Citation(
                    document_id=hit.document_id,
                    unit_id=hit.unit_id,
                    locator_type=hit.locator_type,
                    locator_value=hit.locator_value,
                    quote=" ".join(hit.content.split())[:180],
                ))
        evidence_status = verdict.evidence_status
        conflict_status = verdict.conflict_status
        if evidence_status is EvidenceStatus.SUPPORTED and not citations:
            evidence_status = EvidenceStatus.PARTIAL if hits else _absence_status(artifacts)
        if conflict_status is ConflictStatus.CONTRADICTED and not citations:
            conflict_status = ConflictStatus.UNKNOWN
        output.append(F03ItemAssessment(
            item_id=item.item_id,
            profile=profile_for_item(item.item_id),
            completeness=verdict.completeness,
            reasonableness=verdict.reasonableness,
            evidence_status=evidence_status,
            conflict_status=conflict_status,
            confidence=verdict.confidence,
            citations=citations,
            recommendations=verdict.recommendations,
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
) -> str:
    if not gateway.cache_namespace:
        return ""
    profile = profile_for_item(item.item_id)
    prompt_id = f"govcheck.f03.{profile}"
    template = prompt_run.templates[prompt_id]
    payload = {
        "namespace": gateway.cache_namespace,
        "stage": stage.value,
        "policy": _POLICY_VERSION,
        "schema": _SCHEMA_VERSION,
        "prompt_id": prompt_id,
        "prompt_version": template.version,
        "prompt_hash": template.content_hash,
        "item": _item_payload(item, hits, stage),
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
    if assessment.evidence_status in {EvidenceStatus.PARTIAL, EvidenceStatus.NOT_FOUND, EvidenceStatus.UNREADABLE}:
        output.append(_finding("F03.EVIDENCE_NOT_SUPPORTED", "佐證不足或無法讀取", assessment, location))
    if assessment.conflict_status is ConflictStatus.CONTRADICTED:
        output.append(_finding("F03.EVIDENCE_CONTRADICTED", "佐證內容可能衝突", assessment, location))
    if assessment.conflict_status is ConflictStatus.CONFLICTING_SOURCES:
        output.append(_finding("F03.EVIDENCE_SOURCE_CONFLICT", "佐證文件彼此衝突", assessment, location))
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
