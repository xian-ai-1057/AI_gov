from __future__ import annotations

import json

import pytest

from govcheck.checks.llm import f03_assessment
from govcheck.llm.client import ChatResult, LLMError
from govcheck.review import engine
from govcheck.models import (
    ConflictStatus,
    DecisionStatus,
    DocumentArtifact,
    DocumentParseStatus,
    EvidenceStatus,
    F03Checklist,
    F03ChecklistItem,
    ReviewReport,
    Severity,
    SourceUnit,
    WorkflowStage,
)
from govcheck.prompts import PromptGateway, PromptTemplate, StaticPromptProvider


class FakeClient:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.messages = []

    def chat_result(self, messages, *, schema=None, temperature=None):
        self.messages.append(messages)
        return ChatResult(self.outputs.pop(0), input_tokens=10, output_tokens=5, latency_ms=2)


def _gateway(outputs: list[str], *, cache_namespace: str | None = None) -> PromptGateway:
    templates = [
        PromptTemplate.create(prompt_id, "v1", [{
            "role": "user",
            "content": "{{workflow_stage}}{{items_json}}{{evidence_json}}"
                       "{{policy_version}}{{output_schema_version}}"
                       "{{primary_assessments_json}}",
        }])
        for prompt_id in ("govcheck.f03.fairness", "govcheck.f03.escalation")
    ]
    return PromptGateway(
        provider=StaticPromptProvider(templates),
        client=FakeClient(outputs),
        cache_namespace=cache_namespace,
    )


def _artifact(status=DocumentParseStatus.PARSED) -> DocumentArtifact:
    return DocumentArtifact(
        document_id="doc1",
        filename_hash="hash",
        media_type="text/plain",
        parser_config_hash="cfg",
        status=status,
        source_units=[SourceUnit(
            unit_id="doc1:1",
            content="公平性測試結果符合門檻，所有群組差異均已記錄。",
            locator_value="1",
        )] if status is DocumentParseStatus.PARSED else [],
    )


def _checklist() -> F03Checklist:
    return F03Checklist(items=[F03ChecklistItem(
        item_id="1-1",
        topic="公平性",
        description="請確認公平性測試結果",
        check_state="是",
        evidence_proposal="已完成公平性測試",
    )])


def _response(
    *, confidence=0.9, citation=True, evidence="supported", reasonableness="sufficient"
) -> str:
    return json.dumps({"results": [{
        "item_id": "1-1",
        "completeness": "complete",
        "reasonableness": reasonableness,
        "evidence_status": evidence,
        "conflict_status": "consistent",
        "confidence": confidence,
        "citations": [{"document_id": "doc1", "unit_id": "doc1:1"}] if citation else [],
        "recommendations": [],
    }]})


def test_unified_assessment_returns_validated_citation_and_usage():
    outcome = f03_assessment.run_all(
        _checklist(), [_artifact()], _gateway([_response()]),
        workflow_stage=WorkflowStage.PROPOSAL,
    )

    assessment = outcome.assessments[0]
    assert assessment.evidence_status is EvidenceStatus.SUPPORTED
    assert assessment.conflict_status is ConflictStatus.CONSISTENT
    assert assessment.citations[0].locator_value == "1"
    assert outcome.usage.review_llm_primary_calls == 1
    assert outcome.usage.review_llm_escalation_calls == 0


def test_supported_without_allowed_citation_is_downgraded():
    outcome = f03_assessment.run_all(
        _checklist(), [_artifact()], _gateway([_response(citation=False)]),
        workflow_stage=WorkflowStage.PROPOSAL,
    )
    assert outcome.assessments[0].evidence_status is EvidenceStatus.PARTIAL


@pytest.mark.parametrize(
    ("reasonableness", "expected_code"),
    [
        ("unreasonable", "F03.ASSESSMENT_UNREASONABLE"),
        ("vague", "F03.ASSESSMENT_VAGUE"),
        ("sufficient", None),
        ("reasonable", None),
    ],
)
def test_reasonableness_verdicts_reach_the_report(reasonableness, expected_code):
    """狀態總覽表把 unreasonable 標成需補強，findings 就必須說得出原因，否則警示格查無理由。"""
    outcome = f03_assessment.run_all(
        _checklist(), [_artifact()],
        _gateway([_response(reasonableness=reasonableness)]),
        workflow_stage=WorkflowStage.PROPOSAL,
    )

    codes = {finding.code for finding in outcome.findings}
    problem_codes = codes & {"F03.ASSESSMENT_UNREASONABLE", "F03.ASSESSMENT_VAGUE"}
    assert problem_codes == ({expected_code} if expected_code else set())
    if expected_code:
        finding = next(f for f in outcome.findings if f.code == expected_code)
        assert finding.severity is Severity.WARN
        assert finding.location == "項次 1-1"


def test_low_confidence_runs_single_escalation():
    outcome = f03_assessment.run_all(
        _checklist(), [_artifact()],
        _gateway([_response(confidence=0.2), _response(confidence=0.95)]),
        workflow_stage=WorkflowStage.PROPOSAL,
    )
    assert outcome.assessments[0].confidence == 0.95
    assert outcome.usage.review_llm_primary_calls == 1
    assert outcome.usage.review_llm_escalation_calls == 1


def test_unreadable_document_is_not_treated_as_not_found():
    outcome = f03_assessment.run_all(
        _checklist(), [_artifact(DocumentParseStatus.UNREADABLE)],
        PromptGateway(provider=StaticPromptProvider([]), client=FakeClient([])),
        workflow_stage=WorkflowStage.PROPOSAL,
    )
    assert outcome.assessments[0].evidence_status is EvidenceStatus.UNREADABLE
    assert {finding.code for finding in outcome.findings} == {"F03.PROMPT_UNAVAILABLE"}


def test_duplicate_content_uses_one_model_item_but_returns_each_item():
    checklist = _checklist()
    checklist.items.append(checklist.items[0].model_copy(update={"item_id": "3-1"}))
    output = json.loads(_response())
    outcome = f03_assessment.run_all(
        checklist,
        [_artifact()],
        _gateway([json.dumps(output)]),
        workflow_stage=WorkflowStage.PROPOSAL,
    )

    assert [row.item_id for row in outcome.assessments] == ["1-1", "3-1"]
    assert outcome.usage.review_llm_primary_calls == 1


def test_unchanged_item_uses_versioned_assessment_cache():
    f03_assessment.clear_assessment_cache()
    gateway = _gateway([_response()], cache_namespace="same-runtime")
    first = f03_assessment.run_all(
        _checklist(), [_artifact()], gateway, workflow_stage=WorkflowStage.PROPOSAL
    )
    second = f03_assessment.run_all(
        _checklist(), [_artifact()], gateway, workflow_stage=WorkflowStage.PROPOSAL
    )

    assert first.usage.review_llm_primary_calls == 1
    assert second.usage.review_llm_primary_calls == 0
    assert second.usage.cache_hits == 1


def test_proposal_stage_does_not_send_golive_text_to_model():
    checklist = _checklist()
    checklist.items[0].evidence_golive = "FUTURE_ACCEPTANCE_SECRET"
    gateway = _gateway([_response()])

    f03_assessment.run_all(
        checklist, [_artifact()], gateway, workflow_stage=WorkflowStage.PROPOSAL
    )

    rendered = json.dumps(gateway.client.messages, ensure_ascii=False)
    assert "FUTURE_ACCEPTANCE_SECRET" not in rendered


def test_unknown_item_id_fails_closed_without_guessing_profile():
    checklist = _checklist()
    checklist.items[0].item_id = "9-9"

    outcome = f03_assessment.run_all(
        checklist, [_artifact()], _gateway([]), workflow_stage=WorkflowStage.PROPOSAL
    )

    assert outcome.assessments[0].profile == "unmapped"
    assert {finding.code for finding in outcome.findings} == {"F03.PROMPT_UNAVAILABLE"}


# ── 降級路徑：端點/回應問題絕不能被寫成實質合規缺失 ─────────────────────────

class _FailingClient(FakeClient):
    def chat_result(self, messages, *, schema=None, temperature=None):
        raise LLMError("endpoint down")


def test_batch_failure_is_info_only_and_never_claims_missing_evidence():
    """LLM 端點故障 → 只發 INFO 降級訊號；不得產生 WARN「佐證不足」把故障寫成合規缺失。"""
    gateway = _gateway([])
    gateway.client = _FailingClient([])

    outcome = f03_assessment.run_all(
        _checklist(), [_artifact()], gateway, workflow_stage=WorkflowStage.PROPOSAL
    )

    assert outcome.assessments[0].assessed is False
    codes = {f.code for f in outcome.findings}
    assert "F03.EVIDENCE_NOT_SUPPORTED" not in codes
    assert "F03.ASSESSMENT_UNDETERMINED" in codes
    assert all(f.severity is Severity.INFO for f in outcome.findings)

    # 端到端：整份報告的判定必須是「待人工覆核」，不是「需補正」
    report = ReviewReport(form_type="送件包", findings=outcome.findings)
    assert engine._decision_status(outcome.findings) is DecisionStatus.MANUAL_REVIEW
    assert report.error_count == 0


def test_partial_escalation_response_keeps_primary_assessment():
    """升級判讀漏回某項 → 保留該項初判，不得被覆寫成未判讀。"""
    two_items = F03Checklist(items=[
        F03ChecklistItem(item_id="1-1", topic="公平性", description="A",
                         check_state="是", evidence_proposal="已完成公平性測試"),
        F03ChecklistItem(item_id="3-1", topic="公平性", description="B",
                         check_state="是", evidence_proposal="已完成偏差複核"),
    ])
    primary = json.dumps({"results": [
        json.loads(_response(confidence=0.2))["results"][0] | {"item_id": "1-1"},
        json.loads(_response(confidence=0.3))["results"][0] | {"item_id": "3-1"},
    ]})
    # 升級回應「只」涵蓋 1-1，刻意漏掉 3-1
    escalation = json.dumps({"results": [
        json.loads(_response(confidence=0.95))["results"][0] | {"item_id": "1-1"},
    ]})

    outcome = f03_assessment.run_all(
        two_items, [_artifact()], _gateway([primary, escalation]),
        workflow_stage=WorkflowStage.PROPOSAL,
    )

    by_id = {row.item_id: row for row in outcome.assessments}
    assert by_id["1-1"].confidence == 0.95           # 有回覆 → 用升級結果
    assert by_id["3-1"].confidence == 0.3            # 漏回 → 保留初判，不歸零
    assert by_id["3-1"].completeness == "complete"
    assert by_id["3-1"].assessed is True


def test_items_over_max_items_are_reported_as_not_submitted():
    """超過 max_items 的檢核項不會被判讀，必須留下可見的截斷提示且不可判為通過。"""
    checklist = F03Checklist(items=[
        F03ChecklistItem(item_id=item_id, topic="公平性", description="D",
                         check_state="是", evidence_proposal="佐證內容")
        for item_id in ("1-1", "3-1", "3-2")
    ])

    outcome = f03_assessment.run_all(
        checklist, [_artifact()], _gateway([_response()]),
        workflow_stage=WorkflowStage.PROPOSAL, max_items=1,
    )

    truncation = [f for f in outcome.findings if f.code == "F03.ASSESSMENT_TRUNCATED"]
    assert len(truncation) == 1
    assert "2 項" in truncation[0].message
    assert len(outcome.assessments) == 1
    # 有項目根本沒送審 → 光憑這筆提示就不得判為 approved
    assert engine._decision_status(truncation) is DecisionStatus.MANUAL_REVIEW
