"""協填引擎：LLM 全程 mock（FakeClient + StaticPromptProvider）、佐證解析以 monkeypatch 取代。

每個 case 都有 ground truth：正例（合法引用 → covered）與反例（引用不在 allow-list、yes_no 非法、
覆蓋卻無引用、模型失敗、prompt 缺檔、欄位目錄缺檔）各自的預期輸出。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from govcheck.assist import AssistResult, suggest_prefill
from govcheck.assist import engine
from govcheck.evidence import EvidenceHit, excerpt_id
from govcheck.llm.client import (
    ChatResult,
    LLMContextLengthError,
    LLMError,
    LLMOutputTruncatedError,
)
from govcheck.logging_setup import load_log_config, setup_logging
from govcheck.models import DocumentArtifact, DocumentParseStatus, SourceUnit, UsageSummary
from govcheck.prompts import PromptGateway, PromptTemplate, StaticPromptProvider

FIXTURE = Path(__file__).parent / "fixtures" / "form_fields_minimal.json"
DOC_TEXT = "本案由甲方單位提出，系統代碼 SYS-001。決策結果一律經人工覆核後才生效。"


class FakeClient:
    def __init__(self, outputs: list) -> None:
        self.outputs = list(outputs)
        self.messages = []

    def chat_result(self, messages, *, schema=None, temperature=None):
        self.messages.append(messages)
        output = self.outputs.pop(0)
        if isinstance(output, BaseException):
            raise output
        return ChatResult(output, input_tokens=10, output_tokens=5, latency_ms=2)


def _gateway(outputs: list, *, prompt_id: str = engine.PROMPT_ID) -> PromptGateway:
    template = PromptTemplate.create(prompt_id, "v1", [{
        "role": "user",
        "content": "{{fields_json}}{{evidence_json}}{{output_schema_version}}"
                   "excerpt_id untrusted_data",
    }])
    return PromptGateway(provider=StaticPromptProvider([template]), client=FakeClient(outputs))


def _artifact(
    document_id: str = "doc1",
    text: str = DOC_TEXT,
    status: DocumentParseStatus = DocumentParseStatus.PARSED,
) -> DocumentArtifact:
    return DocumentArtifact(
        document_id=document_id,
        filename_hash="hash",
        media_type="text/plain",
        parser_config_hash="cfg",
        status=status,
        source_units=[SourceUnit(unit_id=f"{document_id}:1", content=text, locator_value="1")]
        if status is DocumentParseStatus.PARSED else [],
    )


def _hit(artifact: DocumentArtifact) -> EvidenceHit:
    unit = artifact.source_units[0]
    return EvidenceHit(
        document_id=artifact.document_id,
        unit_id=unit.unit_id,
        locator_type=unit.locator_type,
        locator_value=unit.locator_value,
        content=unit.content,
        score=1.0,
    )


def _citation(artifact: DocumentArtifact, *, excerpt: str | None = None) -> dict:
    hit = _hit(artifact)
    return {
        "document_id": hit.document_id,
        "unit_id": hit.unit_id,
        "excerpt_id": excerpt or excerpt_id(hit),
    }


def _verdict(field_id: str, *, covered=True, suggestion="甲方單位", confidence=0.9,
             citations=None, rationale="片段直接寫明") -> dict:
    return {
        "field_id": field_id,
        "covered": covered,
        "suggestion": suggestion,
        "confidence": confidence,
        "citations": citations if citations is not None else [_citation(_artifact())],
        "rationale": rationale,
    }


def _response(*verdicts: dict) -> str:
    return json.dumps({"results": list(verdicts)})


@pytest.fixture
def stub_parser(monkeypatch):
    """以預建 DocumentArtifact 取代佐證解析（不打 FileProcess）。可透過 .artifacts 改內容。"""
    state = {"artifacts": [_artifact()], "usage": UsageSummary(), "raise": None}

    def parse_many(self, paths):
        if state["raise"] is not None:
            raise state["raise"]
        return list(state["artifacts"]), state["usage"]

    monkeypatch.setattr(engine.TsParserAdapter, "parse_many", parse_many)
    return state


@pytest.fixture
def fields_env(monkeypatch):
    monkeypatch.setenv("GOVCHECK_FORM_FIELDS_PATH", str(FIXTURE))


@pytest.fixture
def gateway_factory(monkeypatch):
    holder = {"gateway": None}

    def build(cfg):
        if holder["gateway"] is None:
            raise RuntimeError("gateway not configured")
        return holder["gateway"]

    monkeypatch.setattr(engine, "_build_gateway", build)

    def configure(outputs: list, **kwargs) -> PromptGateway:
        holder["gateway"] = _gateway(outputs, **kwargs)
        return holder["gateway"]

    return configure


def _by_id(result: AssistResult) -> dict:
    return {row.field_id: row for row in result.suggestions}


def _audit_lines() -> list[str]:
    path = Path(load_log_config()["dir"]) / "audit.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


# ── 正例 ────────────────────────────────────────────────────────────────────

def test_llm_mode_returns_validated_suggestion_with_citation(
    stub_parser, fields_env, gateway_factory
):
    gateway_factory([_response(
        _verdict("test_a"),
        _verdict("test_b", suggestion="SYS-001", confidence=0.8),
        _verdict("Q-01", suggestion="y", confidence=0.7),
        _verdict("G-01-01", suggestion="Y", confidence=0.6),
        _verdict("G-01-02", covered=False, suggestion=None, confidence=0.1, citations=[]),
        _verdict("1-1", covered=False, suggestion=None, confidence=0.0, citations=[]),
    )])

    result = suggest_prefill(["a.txt"], enable_llm=True)

    assert result.mode == "llm"
    assert result.forms == ["f01", "f02", "f03"]
    rows = _by_id(result)
    assert rows["test_a"].covered is True
    assert rows["test_a"].suggestion == "甲方單位"
    assert rows["test_a"].cell == "A4"
    assert rows["test_a"].confidence == 0.9
    assert rows["test_a"].citations[0].document_id == "doc1"
    assert rows["test_a"].citations[0].locator_value == "1"
    assert rows["test_a"].citations[0].excerpt_id == excerpt_id(_hit(_artifact()))
    assert rows["test_a"].citations[0].quote  # 報告用的摘錄不可為空
    assert rows["Q-01"].suggestion == "Y"  # yes_no 大小寫正規化
    assert rows["1-1"].covered is False and rows["1-1"].suggestion is None
    assert result.usage.review_llm_primary_calls == 1
    assert result.usage.input_tokens == 10
    assert [ref.prompt_id for ref in result.prompt_refs] == [engine.PROMPT_ID]
    assert result.prompt_refs[0].schema_version == "assist-prefill-v1"
    assert result.document_statuses[0].document_id == "doc1"
    assert not [n for n in result.notices if n.code == "ASSIST.LLM_FAILED"]


def test_prompt_payload_carries_allow_list_and_untrusted_marker(
    stub_parser, fields_env, gateway_factory
):
    gateway = gateway_factory([_response(_verdict("test_a"))])

    suggest_prefill(["a.txt"], forms=["f01"], enable_llm=True)

    rendered = gateway.client.messages[0][0]["content"]
    assert '"untrusted_data": true' in rendered
    assert '"allowed_citations"' in rendered
    assert '"options": []' in rendered
    assert excerpt_id(_hit(_artifact())) in rendered
    assert '"1-1"' not in rendered  # forms 過濾生效：F03 欄位沒送進 prompt


def test_document_head_pool_is_citable_even_without_lexical_overlap(
    stub_parser, fields_env, gateway_factory
):
    """文件開頭池：首段跟欄位標籤沒有詞面重疊，仍在 allow-list 內可被引用。"""
    head_doc = _artifact("doc2", text="ZZZ-9 openings without overlap")
    stub_parser["artifacts"] = [head_doc]
    gateway_factory([_response(
        _verdict("test_b", suggestion="ZZZ-9", citations=[_citation(head_doc)]),
    )])

    result = suggest_prefill(["a.txt"], forms=["f01"], enable_llm=True)

    rows = _by_id(result)
    assert rows["test_b"].covered is True
    assert rows["test_b"].citations[0].document_id == "doc2"


# ── 反例：驗證層 ────────────────────────────────────────────────────────────

def test_citation_outside_allow_list_is_dropped_and_field_uncovered(
    stub_parser, fields_env, gateway_factory
):
    gateway_factory([_response(
        _verdict("test_a", citations=[_citation(_artifact(), excerpt="deadbeefdeadbeef")]),
        _verdict("test_b", citations=[{
            "document_id": "ghost", "unit_id": "ghost:1", "excerpt_id": "0000000000000000",
        }]),
    )])

    result = suggest_prefill(["a.txt"], forms=["f01"], enable_llm=True)

    rows = _by_id(result)
    for field_id in ("test_a", "test_b"):
        assert rows[field_id].covered is False
        assert rows[field_id].suggestion is None
        assert rows[field_id].confidence == 0.0
        assert "降為未涵蓋" in rows[field_id].rationale
    assert result.mode == "llm"


def test_covered_without_any_citation_is_downgraded(stub_parser, fields_env, gateway_factory):
    gateway_factory([_response(_verdict("test_a", citations=[]))])

    result = suggest_prefill(["a.txt"], forms=["f01"], enable_llm=True)

    assert _by_id(result)["test_a"].covered is False
    assert _by_id(result)["test_a"].suggestion is None


def test_yes_no_rejects_values_other_than_y_or_n(stub_parser, fields_env, gateway_factory):
    gateway_factory([_response(
        _verdict("Q-01", suggestion="是"),
        _verdict("G-01-01", suggestion="N"),
        _verdict("G-01-02", suggestion="n"),
    )])

    result = suggest_prefill(["a.txt"], forms=["f02"], enable_llm=True)

    rows = _by_id(result)
    assert rows["Q-01"].covered is False and rows["Q-01"].suggestion is None
    assert rows["G-01-01"].suggestion == "N"
    assert rows["G-01-02"].suggestion == "N"


def test_text_suggestion_is_clipped_and_confidence_clamped(
    stub_parser, fields_env, gateway_factory
):
    long_text = "說" * 800
    gateway_factory([_response(_verdict("test_a", suggestion=long_text, confidence=1.0))])

    result = suggest_prefill(["a.txt"], forms=["f01"], enable_llm=True)

    row = _by_id(result)["test_a"]
    assert row.covered is True
    assert len(row.suggestion) <= 501  # 500 字 + 省略號
    assert row.suggestion.endswith("…")
    assert 0.0 <= row.confidence <= 1.0


def test_single_choice_group_keeps_only_highest_confidence_yes(
    stub_parser, fields_env, gateway_factory
):
    gateway_factory([_response(
        _verdict("G-01-01", suggestion="Y", confidence=0.55),
        _verdict("G-01-02", suggestion="Y", confidence=0.9),
        _verdict("Q-01", suggestion="Y", confidence=0.4),
    )])

    result = suggest_prefill(["a.txt"], forms=["f02"], enable_llm=True)

    rows = _by_id(result)
    assert rows["G-01-02"].suggestion == "Y"
    assert rows["G-01-01"].suggestion == "N"
    assert rows["G-01-01"].covered is True  # 仍是有引用的判讀，只是被題組規則改答案
    assert "單選題組" in rows["G-01-01"].rationale
    assert rows["Q-01"].suggestion == "Y"  # 非題組成員不受影響


def test_unanswered_field_is_uncovered(stub_parser, fields_env, gateway_factory):
    gateway_factory([_response(_verdict("test_a"))])  # 模型漏了 test_b

    result = suggest_prefill(["a.txt"], forms=["f01"], enable_llm=True)

    rows = _by_id(result)
    assert rows["test_a"].covered is True
    assert rows["test_b"].covered is False
    assert "未回覆" in rows["test_b"].rationale


# ── 降級模式 ────────────────────────────────────────────────────────────────

def test_llm_disabled_gives_retrieval_only_with_candidate_citations(stub_parser, fields_env):
    result = suggest_prefill(["a.txt"], enable_llm=False)

    assert result.mode == "retrieval_only"
    assert {n.code for n in result.notices} == {"ASSIST.LLM_DISABLED"}
    assert all(not row.covered and row.suggestion is None for row in result.suggestions)
    rows = _by_id(result)
    # 「甲方單位」與文件詞面重疊 → 候選出處；每欄最多 2 筆
    assert rows["test_a"].citations and rows["test_a"].citations[0].document_id == "doc1"
    assert all(len(row.citations) <= 2 for row in result.suggestions)
    assert result.usage.review_llm_primary_calls == 0
    assert result.prompt_refs == []


def test_prompt_unavailable_degrades_to_retrieval_only(stub_parser, fields_env, gateway_factory):
    gateway_factory([], prompt_id="govcheck.f03.fairness")  # 沒有 assist prompt

    result = suggest_prefill(["a.txt"], enable_llm=True)

    assert result.mode == "retrieval_only"
    assert {n.code for n in result.notices} == {"ASSIST.PROMPT_UNAVAILABLE"}
    assert all(not row.covered for row in result.suggestions)
    assert result.prompt_refs == []


def test_llm_error_marks_batch_uncovered_with_failed_notice(
    stub_parser, fields_env, gateway_factory
):
    gateway_factory([LLMError("synthetic")])

    result = suggest_prefill(["a.txt"], forms=["f01"], enable_llm=True)

    assert result.mode == "retrieval_only"  # 沒有任何批次成功
    codes = [n.code for n in result.notices]
    assert codes == ["ASSIST.LLM_FAILED"]
    assert "2" in next(n.message for n in result.notices)
    assert all(not row.covered for row in result.suggestions)


def test_invalid_json_or_schema_violation_is_llm_failed(
    stub_parser, fields_env, gateway_factory
):
    gateway_factory([json.dumps({"results": [{"field_id": "test_a", "bogus": 1}]})])

    result = suggest_prefill(["a.txt"], forms=["f01"], enable_llm=True)

    assert [n.code for n in result.notices] == ["ASSIST.LLM_FAILED"]
    assert all(not row.covered for row in result.suggestions)


def test_gateway_build_failure_never_raises(stub_parser, fields_env, gateway_factory):
    # gateway_factory 未 configure → _build_gateway 會 raise RuntimeError
    result = suggest_prefill(["a.txt"], forms=["f01"], enable_llm=True)

    assert result.mode == "retrieval_only"
    assert [n.code for n in result.notices] == ["ASSIST.LLM_FAILED"]


def test_context_length_error_splits_batch_then_isolates_single_field(
    stub_parser, fields_env, gateway_factory
):
    gateway_factory([
        LLMContextLengthError("too long"),                  # 2 欄 → 二分
        _response(_verdict("test_a")),                      # 第一半成功
        LLMOutputTruncatedError("cut", input_tokens=7, output_tokens=3, latency_ms=1),
    ])

    result = suggest_prefill(["a.txt"], forms=["f01"], enable_llm=True)

    rows = _by_id(result)
    assert rows["test_a"].covered is True
    assert rows["test_b"].covered is False
    assert "長度上限" in rows["test_b"].rationale
    assert result.mode == "llm"
    assert result.usage.review_llm_primary_calls == 2  # 成功 1 次 + length 截斷 1 次
    assert result.usage.input_tokens == 17


def test_fields_unavailable_reports_document_statuses(stub_parser, monkeypatch, tmp_path):
    monkeypatch.setenv("GOVCHECK_FORM_FIELDS_PATH", str(tmp_path / "absent.json"))

    result = suggest_prefill(["a.txt"], enable_llm=True)

    assert result.mode == "unavailable"
    assert [n.code for n in result.notices] == ["ASSIST.FIELDS_UNAVAILABLE"]
    assert result.suggestions == []
    assert len(result.document_statuses) == 1


def test_no_readable_documents_is_flagged_and_nothing_sent_to_model(
    stub_parser, fields_env, gateway_factory
):
    stub_parser["artifacts"] = [_artifact(status=DocumentParseStatus.UNREADABLE)]
    gateway = gateway_factory([_response()])

    result = suggest_prefill(["a.txt"], enable_llm=True)

    assert "ASSIST.NO_READABLE_DOCUMENTS" in {n.code for n in result.notices}
    assert result.mode == "retrieval_only"
    assert all(not row.covered and not row.citations for row in result.suggestions)
    assert gateway.client.messages == []
    assert len(result.document_statuses) == 1


def test_parser_failure_never_raises(stub_parser, fields_env):
    stub_parser["raise"] = RuntimeError("parser exploded")

    result = suggest_prefill(["a.txt"], enable_llm=False)

    assert result.mode == "retrieval_only"
    assert result.document_statuses == []
    assert "ASSIST.NO_READABLE_DOCUMENTS" in {n.code for n in result.notices}


def test_field_limit_truncates_with_notice(stub_parser, fields_env, monkeypatch):
    monkeypatch.setenv("GOVCHECK_ASSIST_MAX_FIELDS", "2")

    result = suggest_prefill(["a.txt"], enable_llm=False)

    assert len(result.suggestions) == 2
    codes = {n.code for n in result.notices}
    assert "ASSIST.FIELDS_TRUNCATED" in codes
    assert "6" in next(n.message for n in result.notices if n.code == "ASSIST.FIELDS_TRUNCATED")


def test_unknown_forms_are_ignored(stub_parser, fields_env):
    result = suggest_prefill(["a.txt"], forms=["F02", "bogus", "f02"], enable_llm=False)

    assert result.forms == ["f02"]
    assert {row.form for row in result.suggestions} == {"f02"}


def test_internal_error_is_contained(stub_parser, fields_env, monkeypatch):
    monkeypatch.setattr(engine, "_prepare_rows", lambda *args, **kwargs: 1 / 0)

    result = suggest_prefill(["a.txt"], enable_llm=False)

    assert result.mode == "unavailable"
    assert result.notices[0].code == "ASSIST.INTERNAL_ERROR"


# ── 稽核與進度 ──────────────────────────────────────────────────────────────

def test_audit_records_counts_only(stub_parser, fields_env, gateway_factory):
    setup_logging()
    gateway_factory([_response(_verdict("test_a"))])

    result = suggest_prefill(["a.txt"], forms=["f01"], enable_llm=True)

    lines = [line for line in _audit_lines() if "assist_done" in line]
    assert len(lines) == 1
    record = json.loads(lines[-1])
    assert record["assist_id"] == result.assist_id
    assert record["mode"] == "llm"
    assert record["fields"] == 2
    assert record["covered"] == 1
    assert record["documents"] == 1
    assert record["assist_llm_calls"] == 1
    text = "\n".join(_audit_lines())
    assert "甲方單位" not in text
    assert "測試欄位" not in text
    assert DOC_TEXT not in text


def test_progress_callback_receives_stage_events_and_failures_are_ignored(
    stub_parser, fields_env
):
    events = []

    def progress(event):
        events.append(event)
        raise RuntimeError("listener bug")

    result = suggest_prefill(["a.txt"], enable_llm=False, progress=progress)

    assert result.mode == "retrieval_only"
    stages = [(event["id"], event["state"]) for event in events if event["kind"] == "stage"]
    assert ("parse", "done") in stages
    assert ("retrieve", "done") in stages
    assert ("suggest", "skipped") in stages
