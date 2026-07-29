from __future__ import annotations

from dataclasses import dataclass

import pytest
import requests

from govcheck.llm.client import ChatResult
from govcheck.prompts import (
    LiteLLMPromptProvider,
    NullPromptProvider,
    PromptCatalog,
    PromptGateway,
    PromptTemplate,
    PromptUnavailable,
    StaticPromptProvider,
    profile_for_item,
)


class FakeClient:
    def __init__(self) -> None:
        self.messages = []
        self.schemas = []

    def chat_result(self, messages, *, schema=None, temperature=None):
        self.messages.append(messages)
        self.schemas.append(schema)
        return ChatResult('{"results":[]}', input_tokens=11, output_tokens=3, latency_ms=7)


def _template(prompt_id: str, version: str = "7") -> PromptTemplate:
    return PromptTemplate.create(prompt_id, version, [
        {"role": "system", "content": "synthetic test prompt"},
        {"role": "user", "content": (
            "{{items_json}}|{{evidence_json}}|{{workflow_stage}}|"
            "{{policy_version}}|{{output_schema_version}}"
        )},
    ])


def _variables() -> dict[str, object]:
    return {
        "workflow_stage": "proposal",
        "items_json": "[]",
        "evidence_json": "[]",
        "policy_version": "test",
        "output_schema_version": "2",
    }


def test_catalog_uses_fixed_item_routes():
    assert profile_for_item("1-1") == "fairness"
    assert profile_for_item("1-5") == "human_oversight"
    assert profile_for_item("3-3") == "privacy"
    assert profile_for_item("5-3") == "security"
    with pytest.raises(KeyError):
        profile_for_item("9-9")


def test_gateway_pins_version_renders_and_collects_usage():
    prompt_id = "govcheck.f03.fairness"
    client = FakeClient()
    gateway = PromptGateway(
        provider=StaticPromptProvider([_template(prompt_id)]),
        client=client,
    )
    run = gateway.begin_run({prompt_id})

    _, usage = gateway.execute(run, prompt_id, _variables(), schema={"type": "object"})

    assert run.refs[0].prompt_version == "7"
    assert client.messages[0][1]["content"] == "[]|[]|proposal|test|2"
    assert usage.review_llm_primary_calls == 1
    assert usage.input_tokens == 11 and usage.output_tokens == 3


def test_gateway_rejects_missing_prompt_variables():
    prompt_id = "govcheck.f03.fairness"
    gateway = PromptGateway(
        provider=StaticPromptProvider([_template(prompt_id)]),
        client=FakeClient(),
    )
    run = gateway.begin_run({prompt_id})
    with pytest.raises(ValueError, match="變數缺漏"):
        gateway.execute(run, prompt_id, {"items_json": "[]"}, schema={})


def test_readiness_rejects_template_contract_with_missing_variables():
    prompt_id = "govcheck.f03.fairness"
    template = PromptTemplate.create(prompt_id, "1", [
        {"role": "user", "content": "{{items_json}}"},
    ])
    gateway = PromptGateway(
        provider=StaticPromptProvider([template]),
        client=FakeClient(),
        catalog=PromptCatalog(),
    )

    assert gateway.readiness()[prompt_id] == "unavailable:PromptUnavailable"


@dataclass
class _Response:
    status_code: int
    body: dict

    def json(self):
        return self.body


def test_litellm_provider_refreshes_label_and_caches_exact_version(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return _Response(200, {
            "prompt_id": "govcheck.f03.fairness",
            "prompt_version": str(11 + len(calls)),
            "prompt_template": [{"role": "user", "content": "{{items_json}}"}],
            "prompt_template_model": "must-not-be-used",
            "prompt_template_optional_params": {"temperature": 1},
        })

    monkeypatch.setattr(requests, "get", fake_get)
    provider = LiteLLMPromptProvider(base_url="https://prompt.internal", api_key="secret")

    first = provider.resolve("govcheck.f03.fairness", label="production")
    second = provider.resolve("govcheck.f03.fairness", label="production")
    pinned = provider.resolve("govcheck.f03.fairness", version="12")

    assert first.version == "12"
    assert second.version == "13"
    assert pinned is first
    assert len(calls) == 2
    assert calls[0][1]["params"] == {
        "prompt_id": "govcheck.f03.fairness", "prompt_label": "production"
    }


def test_null_provider_is_explicitly_unavailable():
    with pytest.raises(PromptUnavailable):
        NullPromptProvider().resolve("govcheck.f03.fairness")


def test_catalog_contains_all_required_prompt_ids():
    ids = set(PromptCatalog().required_prompt_ids())
    assert "govcheck.f03.escalation" in ids
    assert len(ids) == 7
