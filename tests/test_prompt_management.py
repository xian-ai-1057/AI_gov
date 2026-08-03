from __future__ import annotations

import os
from dataclasses import dataclass

import pytest
import requests

from govcheck.llm.client import ChatResult
from govcheck.prompts import (
    FilePromptProvider,
    LiteLLMPromptProvider,
    NullPromptProvider,
    PromptCatalog,
    PromptGateway,
    PromptTemplate,
    PromptUnavailable,
    StaticPromptProvider,
    build_prompt_gateway,
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
        {"role": "system", "content": "synthetic test prompt excerpt_id"},
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
        "output_schema_version": "3",
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
    assert client.messages[0][1]["content"] == "[]|[]|proposal|test|3"
    assert usage.review_llm_primary_calls == 1
    assert usage.input_tokens == 11 and usage.output_tokens == 3


def test_gateway_budget_uses_the_exact_rendered_messages_and_schema():
    prompt_id = "govcheck.f03.fairness"
    client = FakeClient()
    gateway = PromptGateway(
        provider=StaticPromptProvider([_template(prompt_id)]),
        client=client,
    )
    run = gateway.begin_run({prompt_id})

    rendered = gateway.render_messages(run, prompt_id, _variables())
    estimate = gateway.estimate_input_tokens(
        run, prompt_id, _variables(), schema={"type": "object"}
    )
    gateway.execute(run, prompt_id, _variables(), schema={"type": "object"})

    assert client.messages[0] == rendered
    assert estimate > len(rendered[-1]["content"])


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


def test_readiness_rejects_schema_v3_prompt_without_excerpt_id_contract():
    prompt_id = "govcheck.f03.fairness"
    template = PromptTemplate.create(prompt_id, "legacy", [{
        "role": "user",
        "content": (
            "{{items_json}}{{evidence_json}}{{workflow_stage}}"
            "{{policy_version}}{{output_schema_version}}"
        ),
    }])
    gateway = PromptGateway(
        provider=StaticPromptProvider([template]),
        client=FakeClient(),
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


def _write_prompt_file(directory, prompt_id: str, *, version: str = "1", body: str = "hi") -> None:
    (directory / f"{prompt_id}.yaml").write_text(
        "prompt_id: {}\nversion: \"{}\"\nmessages:\n"
        "  - role: system\n    content: |\n      {} excerpt_id\n"
        "  - role: user\n    content: |\n"
        "      {{{{items_json}}}}|{{{{evidence_json}}}}|{{{{workflow_stage}}}}|"
        "{{{{policy_version}}}}|{{{{output_schema_version}}}}\n".format(
            prompt_id, version, body
        ),
        encoding="utf-8",
    )


def test_file_provider_pins_declared_version_and_satisfies_contract(tmp_path):
    prompt_id = "govcheck.f03.privacy"
    _write_prompt_file(tmp_path, prompt_id, version="3")
    gateway = PromptGateway(
        provider=FilePromptProvider(directory=tmp_path), client=FakeClient()
    )

    run = gateway.begin_run({prompt_id})

    assert run.refs[0].prompt_version == "3"
    assert run.refs[0].schema_version == "f03-assessment-v3"


def test_file_provider_reloads_after_edit(tmp_path):
    """本機調 prompt 不必重啟；改檔後下一次 review 必須拿到新版本與新雜湊。"""
    prompt_id = "govcheck.f03.security"
    _write_prompt_file(tmp_path, prompt_id, version="1", body="first")
    provider = FilePromptProvider(directory=tmp_path)
    first = provider.resolve(prompt_id)
    assert provider.resolve(prompt_id) is first  # 未改檔沿用快取

    _write_prompt_file(tmp_path, prompt_id, version="2", body="second")
    path = tmp_path / f"{prompt_id}.yaml"
    os.utime(path, (0, path.stat().st_mtime + 10))

    reloaded = provider.resolve(prompt_id)
    assert reloaded.version == "2"
    assert reloaded.content_hash != first.content_hash


@pytest.mark.parametrize(
    "content",
    [
        "prompt_id: govcheck.f03.privacy\nmessages:\n  - role: user\n    content: x\n",  # 缺 version
        "prompt_id: wrong.id\nversion: \"1\"\nmessages:\n  - role: user\n    content: x\n",
        "prompt_id: govcheck.f03.privacy\nversion: \"1\"\nmessages: not-a-list\n",
        "- just\n- a\n- list\n",
    ],
    ids=["no_version", "id_mismatch", "messages_not_list", "not_a_mapping"],
)
def test_file_provider_rejects_malformed_files(tmp_path, content):
    (tmp_path / "govcheck.f03.privacy.yaml").write_text(content, encoding="utf-8")
    provider = FilePromptProvider(directory=tmp_path)
    with pytest.raises(PromptUnavailable):
        provider.resolve("govcheck.f03.privacy")


def test_file_provider_missing_file_is_unavailable(tmp_path):
    with pytest.raises(PromptUnavailable, match="不存在"):
        FilePromptProvider(directory=tmp_path).resolve("govcheck.f03.fairness")


def test_build_gateway_wires_file_provider(tmp_path, monkeypatch):
    monkeypatch.delenv("GOVCHECK_PROMPT_PROVIDER", raising=False)
    monkeypatch.delenv("GOVCHECK_PROMPT_DIR", raising=False)
    gateway = build_prompt_gateway(
        FakeClient(), {"prompt_provider": "file", "prompt_dir": str(tmp_path)}
    )

    assert isinstance(gateway.provider, FilePromptProvider)
    assert gateway.cache_namespace  # 目錄進入 namespace，換來源不會沿用舊判讀快取


def test_catalog_contains_all_required_prompt_ids():
    ids = set(PromptCatalog().required_prompt_ids())
    assert "govcheck.f03.escalation" in ids
    assert len(ids) == 7


def test_relative_prompt_dir_survives_other_working_directory(tmp_path, monkeypatch):
    """從 repo root 以外啟動（IDE launch config 未指定 cwd）不得讓 prompt 整批降級為未判讀。"""
    monkeypatch.chdir(tmp_path)

    provider = FilePromptProvider(directory="prompts")

    assert provider.directory.is_absolute()
    assert provider.directory.name == "prompts"


def test_label_subdirectory_overrides_and_falls_back(tmp_path):
    """label 子目錄優先、退回目錄根；嚴格版與寬鬆版共用 prompt_id 靠 label 區分。"""
    prompt_id = "govcheck.f03.fairness"
    _write_prompt_file(tmp_path, prompt_id, version="strict-1")
    (tmp_path / "lenient").mkdir()
    _write_prompt_file(tmp_path / "lenient", prompt_id, version="lenient-1")
    provider = FilePromptProvider(directory=tmp_path)

    assert provider.resolve(prompt_id, label="lenient").version == "lenient-1"
    assert provider.resolve(prompt_id, label="production").version == "strict-1"  # 無子目錄 → 退回
    assert provider.resolve(prompt_id).version == "strict-1"


def test_label_sets_do_not_share_cache(tmp_path):
    """兩套 prompt 是不同檔案，快取必須以 label 區隔，否則會拿到另一套的判準。"""
    prompt_id = "govcheck.f03.privacy"
    _write_prompt_file(tmp_path, prompt_id, version="strict-1", body="strict")
    (tmp_path / "lenient").mkdir()
    _write_prompt_file(tmp_path / "lenient", prompt_id, version="lenient-1", body="lenient")
    provider = FilePromptProvider(directory=tmp_path)

    strict = provider.resolve(prompt_id, label="production")
    lenient = provider.resolve(prompt_id, label="lenient")

    assert strict.content_hash != lenient.content_hash
    assert provider.resolve(prompt_id, label="production").content_hash == strict.content_hash


@pytest.mark.parametrize("label", ["..", "../..", "a/b", "a\\b"])
def test_label_cannot_escape_prompt_directory(tmp_path, label):
    with pytest.raises(PromptUnavailable, match="不合法|不存在"):
        FilePromptProvider(directory=tmp_path).resolve("govcheck.f03.fairness", label=label)
