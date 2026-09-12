"""協填引擎：佐證解析 → 每欄位檢索 → （可選）模型建議 → allow-list 驗證。

設計原則與 F03 判讀（`checks/llm/f03_assessment.py`）一致：
- 模型只能引用 `allowed_citations` 內的 `(document_id, unit_id, excerpt_id)`，對不上就丟；
- `covered=True` 但沒有存活引用 → 降為未涵蓋（**不憑空生成**）；
- 任何一層失敗都降級（retrieval_only / unavailable），`suggest_prefill` **絕不 raise**。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from govcheck.assist.fields import FORMS, FieldSpec, load_form_fields, query_text
from govcheck.assist.models import AssistResult, Suggestion
from govcheck.evidence import EvidenceHit, EvidenceRetriever, excerpt_id
from govcheck.llm.client import (
    ChatClient,
    LLMContextLengthError,
    LLMError,
    LLMOutputTruncatedError,
    parse_json_object,
)
from govcheck.llm.config import load_llm_config
from govcheck.llm.embedding import EmbeddingClient
from govcheck.logging_setup import audit, get_logger
from govcheck.models import (
    Citation,
    DocumentArtifact,
    DocumentParseStatus,
    Finding,
    PromptRunRef,
    Severity,
    UsageSummary,
)
from govcheck.parsers.supporting import TsParserAdapter
from govcheck.prompts import PromptGateway, PromptUnavailable, build_prompt_gateway, pack_batches

log = get_logger("assist")

PROMPT_ID = "govcheck.assist.prefill"
_OUTPUT_SCHEMA_VERSION = "1"
_MAX_SUGGESTION_CHARS = 500
_MAX_RATIONALE_CHARS = 300
_HEAD_POOL_MAX_DOCS = 6
_CANDIDATE_CITATIONS = 2
_ESTIMATED_OUTPUT_TOKENS_PER_FIELD = 256
_DEFAULT_MAX_FIELDS = 80
_DEFAULT_TOP_K = 4

ProgressFn = Callable[[dict], None]


class _CitationVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    unit_id: str
    excerpt_id: str


class _FieldVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: str
    covered: bool
    suggestion: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    citations: list[_CitationVerdict] = Field(default_factory=list)
    rationale: str = ""


class _AssistBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[_FieldVerdict]


_BATCH_SCHEMA = _AssistBatch.model_json_schema()


@dataclass
class _Row:
    field: FieldSpec
    hits: list[EvidenceHit]        # 檢索命中（依相關性排序）
    allowed: list[EvidenceHit]     # allow-list = 命中 + 文件開頭池


@dataclass
class _LLMOutcome:
    suggestions: list[Suggestion]
    mode: str
    usage: UsageSummary
    prompt_refs: list[PromptRunRef]
    notices: list[Finding]


def suggest_prefill(
    supporting_paths: list[str | Path],
    *,
    forms: Sequence[str] = ("f01", "f02", "f03"),
    enable_llm: bool,
    progress: ProgressFn | None = None,
) -> AssistResult:
    """依佐證文件為指定表單產生填寫建議；任何失敗一律降級，**絕不 raise**。"""
    t0 = time.perf_counter()
    assist_id = uuid.uuid4().hex
    wanted = _normalize_forms(forms)
    try:
        return _suggest(assist_id, supporting_paths, wanted, enable_llm, progress, t0)
    except Exception as exc:  # noqa: BLE001 - 介面層不需為協填故障設防
        log.exception("assist failed err=%s", type(exc).__name__)
        return AssistResult(
            assist_id=assist_id,
            forms=wanted,
            suggestions=[],
            document_statuses=[],
            notices=[_notice(
                "ASSIST.INTERNAL_ERROR",
                "協填流程發生未預期錯誤",
                "協填流程中斷，未產生任何建議；請稍後重試或改以人工填寫。",
            )],
            usage=UsageSummary(),
            prompt_refs=[],
            mode="unavailable",
        )


def _suggest(
    assist_id: str,
    supporting_paths: list[str | Path],
    wanted: list[str],
    enable_llm: bool,
    progress: ProgressFn | None,
    t0: float,
) -> AssistResult:
    notices: list[Finding] = []
    usage = UsageSummary()
    cfg = _safe_llm_config()
    max_fields = max(1, int(cfg.get("assist_max_fields", _DEFAULT_MAX_FIELDS)))
    top_k = max(1, int(cfg.get("assist_top_k", _DEFAULT_TOP_K)))
    log.info("assist_start forms=%s documents=%d enable_llm=%s",
             ",".join(wanted), len(supporting_paths), enable_llm)

    _emit(progress, {"kind": "stage", "id": "parse", "state": "running"})
    artifacts, parse_usage = _parse_documents(supporting_paths)
    usage.add(parse_usage)
    parsed = [artifact for artifact in artifacts if _readable(artifact)]
    _emit(progress, {"kind": "stage", "id": "parse", "state": "done",
                     "documents": len(artifacts), "parsed": len(parsed)})

    all_fields = load_form_fields()
    if all_fields is None:
        notices.append(_notice(
            "ASSIST.FIELDS_UNAVAILABLE",
            "表單欄位目錄不可用",
            "找不到或無法讀取 data/rag/form_fields.json（由 scripts/extract_form_fields.py 產出）；"
            "本次無法產生填寫建議，佐證文件解析狀態仍列於下方。",
        ))
        _emit_skipped(progress, "retrieve", "suggest")
        return _finish(assist_id, wanted, [], [], artifacts, notices, usage, [],
                       "unavailable", enable_llm, t0)

    fields = [field for field in all_fields if field.form in wanted]
    if len(fields) > max_fields:
        notices.append(_notice(
            "ASSIST.FIELDS_TRUNCATED",
            "欄位數超過單次上限，部分欄位未處理",
            f"本次只處理前 {max_fields} 個欄位（共 {len(fields)} 個）；"
            "未處理的欄位不會出現在建議書中，請縮小表單範圍或分次執行。",
        ))
        fields = fields[:max_fields]
    if not parsed:
        notices.append(_notice(
            "ASSIST.NO_READABLE_DOCUMENTS",
            "沒有可讀取的佐證文件",
            "上傳的佐證文件均無法解析或內容為空；所有欄位標示為未涵蓋，請自行填寫。",
        ))

    _emit(progress, {"kind": "stage", "id": "retrieve", "state": "running"})
    t_retrieve = time.perf_counter()
    retriever = _build_retriever(cfg, enable_llm)
    rows = _prepare_rows(fields, artifacts, retriever, top_k)
    usage.evidence_embedding_calls += retriever.embedding_calls
    _emit(progress, {"kind": "stage", "id": "retrieve", "state": "done",
                     "elapsed_ms": round((time.perf_counter() - t_retrieve) * 1000)})

    prompt_refs: list[PromptRunRef] = []
    if not enable_llm:
        notices.append(_notice(
            "ASSIST.LLM_DISABLED",
            "未啟用 AI 建議",
            "本次只列出各欄位可能相關的候選出處，未產生填寫建議；啟用 LLM 後可再執行。",
        ))
        suggestions = [_uncovered(row) for row in rows]
        mode = "retrieval_only"
        _emit_skipped(progress, "suggest")
    elif not parsed or not rows:
        suggestions = [_uncovered(row) for row in rows]
        mode = "retrieval_only"
        _emit_skipped(progress, "suggest")
    else:
        _emit(progress, {"kind": "stage", "id": "suggest", "state": "running"})
        t_suggest = time.perf_counter()
        outcome = _run_llm(rows, cfg)
        usage.add(outcome.usage)
        notices.extend(outcome.notices)
        prompt_refs = outcome.prompt_refs
        suggestions = outcome.suggestions
        mode = outcome.mode
        _emit(progress, {"kind": "stage", "id": "suggest", "state": "done",
                         "elapsed_ms": round((time.perf_counter() - t_suggest) * 1000)})

    suggestions = _enforce_single_choice(suggestions, fields)
    return _finish(assist_id, wanted, fields, suggestions, artifacts, notices, usage,
                   prompt_refs, mode, enable_llm, t0)


# ── 各階段 ──────────────────────────────────────────────────────────────────

def _normalize_forms(forms: Sequence[str]) -> list[str]:
    wanted: list[str] = []
    for form in forms:
        key = str(form).strip().casefold()
        if key in FORMS and key not in wanted:
            wanted.append(key)
    return wanted


def _safe_llm_config() -> dict:
    try:
        return load_llm_config()
    except Exception as exc:  # noqa: BLE001 - 設定壞掉也要能跑 retrieval_only
        log.warning("assist llm config unavailable err=%s", type(exc).__name__)
        return {}


def _parse_documents(paths: list[str | Path]) -> tuple[list[DocumentArtifact], UsageSummary]:
    if not paths:
        return [], UsageSummary()
    try:
        return TsParserAdapter().parse_many([Path(path) for path in paths])
    except Exception as exc:  # noqa: BLE001 - 解析層故障降級為「無可讀文件」
        log.warning("assist parse failed err=%s", type(exc).__name__)
        return [], UsageSummary()


def _readable(artifact: DocumentArtifact) -> bool:
    return artifact.status is DocumentParseStatus.PARSED and any(
        unit.content.strip() for unit in artifact.source_units
    )


def _build_retriever(cfg: dict, enable_llm: bool) -> EvidenceRetriever:
    embedding_client = None
    # retrieval_only 模式刻意不打 embedding 端點：LLM 關閉時整個流程應完全離線。
    if enable_llm and cfg.get("evidence_embedding_enabled"):
        try:
            embedding_client = EmbeddingClient.from_config(cfg)
        except Exception as exc:  # noqa: BLE001 - 語意檢索不可用退回詞面檢索
            log.warning("assist embedding client unavailable err=%s", type(exc).__name__)
    return EvidenceRetriever(
        embedding_client=embedding_client,
        lexical_weight=cfg.get("evidence_lexical_weight", 0.7),
        max_content_chars=cfg.get("evidence_excerpt_chars", 600),
        max_embedding_chunks=cfg.get("embedding_max_chunks", 512),
    )


def _prepare_rows(
    fields: list[FieldSpec],
    artifacts: list[DocumentArtifact],
    retriever: EvidenceRetriever,
    top_k: int,
) -> list[_Row]:
    queries = [query_text(field) for field in fields]
    try:
        hits_per_field = retriever.retrieve_many(queries, artifacts, top_k=top_k)
    except Exception as exc:  # noqa: BLE001 - 檢索故障 = 沒有命中，不中斷
        log.warning("assist retrieval failed err=%s", type(exc).__name__)
        hits_per_field = [[] for _ in fields]
    heads = _document_heads(artifacts, retriever.max_content_chars)
    rows: list[_Row] = []
    for field, hits in zip(fields, hits_per_field):
        allowed = list(hits)
        seen = {(hit.document_id, hit.unit_id, hit.content) for hit in hits}
        # 文件開頭池：簽呈／簡報的首段通常就有單位、專案名、日期這類 F01 欄位，
        # 但這些詞跟欄位標籤沒有詞面重疊，純靠檢索會漏；併入 allow-list 讓模型可引用。
        for head in heads:
            key = (head.document_id, head.unit_id, head.content)
            if key not in seen:
                seen.add(key)
                allowed.append(head)
        rows.append(_Row(field=field, hits=hits, allowed=allowed))
    return rows


def _document_heads(artifacts: list[DocumentArtifact], max_chars: int) -> list[EvidenceHit]:
    heads: list[EvidenceHit] = []
    for artifact in artifacts:
        if not _readable(artifact):
            continue
        unit = next(unit for unit in artifact.source_units if unit.content.strip())
        content = unit.content.strip()[:max(100, max_chars)]
        heads.append(EvidenceHit(
            document_id=artifact.document_id,
            unit_id=unit.unit_id,
            locator_type=unit.locator_type,
            locator_value=unit.locator_value,
            content=content,
            score=0.0,
            quote=_quote(content),
        ))
        if len(heads) >= _HEAD_POOL_MAX_DOCS:
            break
    return heads


def _build_gateway(cfg: dict) -> PromptGateway:
    """建立 prompt gateway；測試以 monkeypatch 換成假 gateway。"""
    return build_prompt_gateway(ChatClient.from_config(cfg), cfg)


def _run_llm(rows: list[_Row], cfg: dict) -> _LLMOutcome:
    try:
        gateway = _build_gateway(cfg)
    except Exception as exc:  # noqa: BLE001 - 端點設定壞掉 = 模型不可用
        log.warning("assist gateway unavailable err=%s", type(exc).__name__)
        return _LLMOutcome(
            [_uncovered(row) for row in rows], "retrieval_only", UsageSummary(), [],
            [_llm_failed_notice(len(rows))],
        )
    try:
        run = gateway.begin_run({PROMPT_ID})
    except (PromptUnavailable, KeyError, ValueError) as exc:
        log.warning("assist prompt unavailable err=%s", type(exc).__name__)
        return _LLMOutcome(
            [_uncovered(row) for row in rows], "retrieval_only", UsageSummary(), [],
            [_notice(
                "ASSIST.PROMPT_UNAVAILABLE",
                "協填 prompt 未就緒",
                "找不到 govcheck.assist.prefill 或其內容不符契約；本次只列候選出處，未產生建議。",
            )],
        )
    usage = UsageSummary()
    output_limit = max(1, gateway.max_output_tokens // _ESTIMATED_OUTPUT_TOKENS_PER_FIELD)
    batch_limit = min(max(1, int(cfg.get("batch_size", 8))), output_limit)
    batches = pack_batches(
        rows,
        max_items=batch_limit,
        fits=lambda candidate: gateway.fits_context(
            run, PROMPT_ID, _variables(candidate), schema=_BATCH_SCHEMA
        ),
    )
    suggestions: list[Suggestion] = []
    succeeded = 0
    failed_fields = 0
    pending = list(batches)
    while pending:
        batch = pending.pop(0)
        try:
            result, call_usage = gateway.execute(
                run, PROMPT_ID, _variables(batch), schema=_BATCH_SCHEMA
            )
            usage.add(call_usage)
            verdict = _AssistBatch.model_validate(parse_json_object(result.content))
            suggestions.extend(_validate_batch(batch, verdict))
            succeeded += 1
        except (LLMContextLengthError, LLMOutputTruncatedError) as exc:
            _add_truncated_usage(usage, exc)
            if len(batch) > 1:
                midpoint = len(batch) // 2
                pending[0:0] = [batch[:midpoint], batch[midpoint:]]
                log.info("assist size retry split fields=%d err=%s",
                         len(batch), type(exc).__name__)
                continue
            log.warning("assist field exceeds model size limit err=%s", type(exc).__name__)
            suggestions.extend(_uncovered(row, rationale="模型輸入超過長度上限，未產生建議。")
                               for row in batch)
            failed_fields += len(batch)
        except (LLMError, ValidationError, ValueError, KeyError) as exc:
            log.warning("assist batch failed fields=%d err=%s", len(batch), type(exc).__name__)
            suggestions.extend(_uncovered(row, rationale="模型回應無效或呼叫失敗，未產生建議。")
                               for row in batch)
            failed_fields += len(batch)
    notices = [_llm_failed_notice(failed_fields)] if failed_fields else []
    mode = "llm" if succeeded else "retrieval_only"
    log.info("assist llm done batches=%d succeeded=%d failed_fields=%d",
             len(batches), succeeded, failed_fields)
    return _LLMOutcome(suggestions, mode, usage, list(run.refs), notices)


def _add_truncated_usage(
    usage: UsageSummary, exc: LLMContextLengthError | LLMOutputTruncatedError
) -> None:
    """finish_reason=length 的回應仍消耗了 token，重試前先計入；context 400 沒有 usage。"""
    if not isinstance(exc, LLMOutputTruncatedError):
        return
    usage.review_llm_primary_calls += 1
    usage.input_tokens += exc.input_tokens
    usage.output_tokens += exc.output_tokens
    usage.latency_ms += exc.latency_ms


# ── Prompt payload ──────────────────────────────────────────────────────────

def _variables(batch: list[_Row]) -> dict[str, object]:
    return {
        "fields_json": json.dumps([_field_payload(row) for row in batch], ensure_ascii=False),
        "evidence_json": json.dumps(_unique_evidence(batch), ensure_ascii=False),
        "output_schema_version": _OUTPUT_SCHEMA_VERSION,
    }


def _field_payload(row: _Row) -> dict:
    field = row.field
    return {
        "field_id": field.field_id,
        "form": field.form,
        "label": field.label,
        "description": field.description,
        "kind": field.kind,
        "options": ["Y", "N"] if field.kind == "yes_no" else [],
        "allowed_citations": [
            {
                "document_id": hit.document_id,
                "unit_id": hit.unit_id,
                "excerpt_id": excerpt_id(hit),
            }
            for hit in row.allowed
        ],
    }


def _unique_evidence(batch: list[_Row]) -> list[dict]:
    """同一原文視窗在批次內只送一次，並保留它屬於哪些欄位。"""
    output: list[dict] = []
    index_by_window: dict[tuple[str, str, str], int] = {}
    for row in batch:
        for hit in row.allowed:
            key = (hit.document_id, hit.unit_id, hit.content)
            existing = index_by_window.get(key)
            if existing is not None:
                if row.field.field_id not in output[existing]["field_ids"]:
                    output[existing]["field_ids"].append(row.field.field_id)
                continue
            index_by_window[key] = len(output)
            output.append({
                "field_ids": [row.field.field_id],
                "document_id": hit.document_id,
                "unit_id": hit.unit_id,
                "excerpt_id": excerpt_id(hit),
                "locator_type": hit.locator_type,
                "locator_value": hit.locator_value,
                "content": hit.content,
                "untrusted_data": True,
            })
    return output


# ── 驗證與收斂 ──────────────────────────────────────────────────────────────

def _validate_batch(batch: list[_Row], response: _AssistBatch) -> list[Suggestion]:
    verdicts = {row.field_id: row for row in response.results}
    output: list[Suggestion] = []
    for row in batch:
        verdict = verdicts.get(row.field.field_id)
        if verdict is None:
            output.append(_uncovered(row, rationale="模型未回覆此欄位。"))
            continue
        allowed = {
            (hit.document_id, hit.unit_id, excerpt_id(hit)): hit for hit in row.allowed
        }
        citations: list[Citation] = []
        seen: set[tuple[str, str, str]] = set()
        for citation in verdict.citations:
            key = (citation.document_id, citation.unit_id, citation.excerpt_id)
            hit = allowed.get(key)
            if hit is None or key in seen:
                continue
            seen.add(key)
            citations.append(_citation(hit))
        rationale = _clip(verdict.rationale, _MAX_RATIONALE_CHARS)
        suggestion = _normalize_suggestion(verdict.suggestion, row.field.kind)
        covered = bool(verdict.covered) and bool(citations) and suggestion is not None
        if verdict.covered and not covered:
            # 模型宣稱涵蓋但引用對不上 allow-list／答案格式不合：不憑空生成，降為未涵蓋。
            rationale = _clip(
                f"{rationale}（引用未通過驗證或答案格式不合，已降為未涵蓋）".strip(),
                _MAX_RATIONALE_CHARS,
            )
        confidence = min(1.0, max(0.0, float(verdict.confidence))) if covered else 0.0
        output.append(Suggestion(
            form=row.field.form,
            field_id=row.field.field_id,
            label=row.field.label,
            cell=row.field.cell,
            kind=row.field.kind,
            covered=covered,
            suggestion=suggestion if covered else None,
            confidence=confidence,
            citations=citations if covered else _candidate_citations(row),
            rationale=rationale,
        ))
    return output


def _normalize_suggestion(text: str | None, kind: str) -> str | None:
    if text is None:
        return None
    value = text.strip()
    if not value:
        return None
    if kind == "yes_no":
        answer = value.upper()
        return answer if answer in {"Y", "N"} else None
    return _clip(value, _MAX_SUGGESTION_CHARS)


def _enforce_single_choice(
    suggestions: list[Suggestion], fields: list[FieldSpec]
) -> list[Suggestion]:
    """F02 單選題組只能有一個 Y：多個 Y 時留信心最高者，其餘改 N（留引用與說明供覆核）。"""
    group_of = {
        (field.form, field.field_id): field.group
        for field in fields
        if field.group and field.group_kind == "single_choice"
    }
    yes_by_group: dict[tuple[str, str], list[Suggestion]] = {}
    for suggestion in suggestions:
        group = group_of.get((suggestion.form, suggestion.field_id))
        if group and suggestion.covered and suggestion.suggestion == "Y":
            yes_by_group.setdefault((suggestion.form, group), []).append(suggestion)
    for members in yes_by_group.values():
        if len(members) < 2:
            continue
        keep = max(members, key=lambda item: item.confidence)
        for member in members:
            if member is keep:
                continue
            member.suggestion = "N"
            member.rationale = _clip(
                f"{member.rationale}（單選題組：另一選項信心較高，已改為 N）".strip(),
                _MAX_RATIONALE_CHARS,
            )
    return suggestions


def _uncovered(row: _Row, *, rationale: str = "") -> Suggestion:
    return Suggestion(
        form=row.field.form,
        field_id=row.field.field_id,
        label=row.field.label,
        cell=row.field.cell,
        kind=row.field.kind,
        covered=False,
        suggestion=None,
        confidence=0.0,
        citations=_candidate_citations(row),
        rationale=rationale,
    )


def _candidate_citations(row: _Row) -> list[Citation]:
    """未涵蓋欄位附前幾名檢索命中當「候選出處」，讓使用者自己翻文件確認。"""
    return [_citation(hit) for hit in row.hits[:_CANDIDATE_CITATIONS]]


def _citation(hit: EvidenceHit) -> Citation:
    return Citation(
        document_id=hit.document_id,
        unit_id=hit.unit_id,
        excerpt_id=excerpt_id(hit),
        locator_type=hit.locator_type,
        locator_value=hit.locator_value,
        quote=hit.quote or _quote(hit.content),
    )


def _quote(content: str) -> str:
    return " ".join(content.split())[:180]


def _clip(text: str, limit: int) -> str:
    value = text.strip()
    return value if len(value) <= limit else value[:limit].rstrip() + "…"


# ── 收尾 ────────────────────────────────────────────────────────────────────

def _finish(
    assist_id: str,
    wanted: list[str],
    fields: list[FieldSpec],
    suggestions: list[Suggestion],
    artifacts: list[DocumentArtifact],
    notices: list[Finding],
    usage: UsageSummary,
    prompt_refs: list[PromptRunRef],
    mode: str,
    enable_llm: bool,
    t0: float,
) -> AssistResult:
    covered = sum(1 for suggestion in suggestions if suggestion.covered)
    parsed = sum(1 for artifact in artifacts if _readable(artifact))
    duration_ms = round((time.perf_counter() - t0) * 1000)
    log.info("assist_done mode=%s fields=%d covered=%d documents=%d dur=%dms",
             mode, len(fields), covered, len(artifacts), duration_ms)
    # 稽核軌跡只記識別資訊與計數：不記欄位標籤、建議內容、佐證文字。
    audit(
        "assist_done",
        assist_id=assist_id,
        forms=",".join(wanted),
        mode=mode,
        enable_llm=enable_llm,
        fields=len(fields),
        covered=covered,
        documents=len(artifacts),
        parsed_documents=parsed,
        notice_codes=[notice.code for notice in notices],
        assist_llm_calls=usage.review_llm_primary_calls,
        duration_ms=duration_ms,
    )
    return AssistResult(
        assist_id=assist_id,
        forms=wanted,
        suggestions=suggestions,
        document_statuses=artifacts,
        notices=notices,
        usage=usage,
        prompt_refs=prompt_refs,
        mode=mode,  # type: ignore[arg-type]
    )


def _notice(code: str, title: str, message: str) -> Finding:
    return Finding(
        severity=Severity.INFO, code=code, title=title, message=message, source="llm"
    )


def _llm_failed_notice(fields: int) -> Finding:
    return _notice(
        "ASSIST.LLM_FAILED",
        "部分欄位的模型建議失敗",
        f"有 {fields} 個欄位因模型呼叫失敗或回應無效而未產生建議，已標示為未涵蓋；"
        "可稍後重試。",
    )


def _emit(progress: ProgressFn | None, event: dict) -> None:
    if progress is None:
        return
    try:
        progress(event)
    except Exception as exc:  # noqa: BLE001 - 進度通知失敗不得中斷協填
        log.warning("progress callback failed err=%s", type(exc).__name__)


def _emit_skipped(progress: ProgressFn | None, *stage_ids: str) -> None:
    for stage_id in stage_ids:
        _emit(progress, {"kind": "stage", "id": stage_id, "state": "skipped"})
