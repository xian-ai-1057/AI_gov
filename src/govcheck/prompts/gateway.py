"""Resolve, pin, validate, render and execute centrally managed prompts."""

from __future__ import annotations

import os
import re
import hashlib
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TypeVar

from govcheck import __version__
from govcheck.llm.client import (
    DEFAULT_CONTEXT_SAFETY_MARGIN_TOKENS,
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    ChatClient,
    ChatResult,
    estimate_chat_input_tokens,
)
from govcheck.models import PromptRunRef, UsageSummary
from govcheck.prompts.catalog import PromptCatalog
from govcheck.prompts.providers import (
    FilePromptProvider,
    LiteLLMPromptProvider,
    NullPromptProvider,
    PromptProvider,
    PromptTemplate,
    PromptUnavailable,
)


_T = TypeVar("_T")


@dataclass(frozen=True)
class PromptRun:
    templates: dict[str, PromptTemplate]
    refs: tuple[PromptRunRef, ...]


def pack_batches(
    rows: list[_T],
    *,
    max_items: int,
    fits: Callable[[list[_T]], bool],
) -> list[list[_T]]:
    """Greedily pack rows using the exact rendered prompt budget.

    A single row that does not fit is still returned as its own batch. The ChatClient preflight will
    fail it closed without contacting the endpoint, while other rows can continue independently.
    """
    batches: list[list[_T]] = []
    current: list[_T] = []
    item_limit = max(1, max_items)
    for row in rows:
        candidate = [*current, row]
        if current and (len(candidate) > item_limit or not fits(candidate)):
            batches.append(current)
            current = [row]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


class PromptGateway:
    def __init__(
        self,
        *,
        provider: PromptProvider,
        client: ChatClient,
        catalog: PromptCatalog | None = None,
        label: str = "production",
        cache_namespace: str | None = None,
    ) -> None:
        self.provider = provider
        self.client = client
        self.catalog = catalog or PromptCatalog()
        self.label = label
        self.cache_namespace = cache_namespace

    def begin_run(self, prompt_ids: set[str]) -> PromptRun:
        templates = {}
        refs = []
        for prompt_id in sorted(prompt_ids):
            contract = self.catalog.get(prompt_id)
            if _version_tuple(__version__) < _version_tuple(contract.minimum_app_version):
                raise PromptUnavailable(
                    f"應用程式版本不相容：{prompt_id} 需要 {contract.minimum_app_version}"
                )
            template = self.provider.resolve(prompt_id, label=self.label)
            template_variables = _template_variables(template)
            missing = contract.required_variables - template_variables
            if missing:
                raise PromptUnavailable(
                    f"Prompt template 變數契約缺漏：{prompt_id} ({', '.join(sorted(missing))})"
                )
            template_text = "\n".join(row["content"] for row in template.messages)
            missing_markers = contract.required_content_markers - {
                marker
                for marker in contract.required_content_markers
                if marker in template_text
            }
            if missing_markers:
                raise PromptUnavailable(
                    f"Prompt template 內容契約缺漏：{prompt_id} "
                    f"({', '.join(sorted(missing_markers))})"
                )
            templates[prompt_id] = template
            refs.append(PromptRunRef(
                prompt_id=prompt_id,
                prompt_version=template.version,
                content_hash=template.content_hash,
                schema_version=contract.schema_version,
            ))
        return PromptRun(templates=templates, refs=tuple(refs))

    def execute(
        self,
        run: PromptRun,
        prompt_id: str,
        variables: dict[str, object],
        *,
        schema: dict,
        call_kind: str = "primary",
    ) -> tuple[ChatResult, UsageSummary]:
        messages = self.render_messages(run, prompt_id, variables)
        result = self.client.chat_result(messages, schema=schema)
        usage = UsageSummary(
            review_llm_primary_calls=int(call_kind == "primary"),
            review_llm_escalation_calls=int(call_kind == "escalation"),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            latency_ms=result.latency_ms,
        )
        return result, usage

    def render_messages(
        self,
        run: PromptRun,
        prompt_id: str,
        variables: dict[str, object],
    ) -> list[dict]:
        """Render the exact messages that will be sent, without executing a model call."""
        contract = self.catalog.get(prompt_id)
        missing = contract.required_variables - variables.keys()
        if missing:
            raise ValueError(f"Prompt 變數缺漏：{', '.join(sorted(missing))}")
        template = run.templates[prompt_id]
        return [
            {"role": row["role"], "content": _render(row["content"], variables)}
            for row in template.messages
        ]

    @property
    def input_token_budget(self) -> int:
        configured = getattr(self.client, "input_token_budget", None)
        if configured is not None:
            return max(0, int(configured))
        return (
            DEFAULT_CONTEXT_WINDOW_TOKENS
            - 4096
            - DEFAULT_CONTEXT_SAFETY_MARGIN_TOKENS
        )

    @property
    def max_output_tokens(self) -> int:
        return max(1, int(getattr(self.client, "max_output_tokens", 4096)))

    def estimate_input_tokens(
        self,
        run: PromptRun,
        prompt_id: str,
        variables: dict[str, object],
        *,
        schema: dict | None = None,
    ) -> int:
        messages = self.render_messages(run, prompt_id, variables)
        estimator = getattr(self.client, "estimate_input_tokens", None)
        if callable(estimator):
            return int(estimator(messages, schema=schema))
        return estimate_chat_input_tokens(messages, schema)

    def fits_context(
        self,
        run: PromptRun,
        prompt_id: str,
        variables: dict[str, object],
        *,
        schema: dict | None = None,
    ) -> bool:
        return self.estimate_input_tokens(
            run, prompt_id, variables, schema=schema
        ) <= self.input_token_budget

    def readiness(self, prompt_ids: Iterable[str] | None = None) -> dict[str, str]:
        """逐一解析 prompt；預設檢查目錄內全部，呼叫端可傳子集（例如只看 F03 或只看協填）。"""
        result = {}
        targets = (
            self.catalog.required_prompt_ids() if prompt_ids is None else tuple(prompt_ids)
        )
        for prompt_id in targets:
            try:
                run = self.begin_run({prompt_id})
                result[prompt_id] = run.templates[prompt_id].version
            except Exception as exc:  # noqa: BLE001 - readiness reports unavailable providers
                result[prompt_id] = f"unavailable:{type(exc).__name__}"
        return result


def _render(text: str, variables: dict[str, object]) -> str:
    rendered = text
    for key, value in variables.items():
        rendered = rendered.replace("{{" + key + "}}", str(value))
        rendered = rendered.replace("{" + key + "}", str(value))
    return rendered


def _template_variables(template: PromptTemplate) -> set[str]:
    variables = set()
    for message in template.messages:
        variables.update(re.findall(r"\{\{?([A-Za-z_][A-Za-z0-9_]*)\}?\}", message["content"]))
    return variables


def _version_tuple(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value)
    return tuple(int(number) for number in numbers[:3]) or (0,)


def build_prompt_gateway(client: ChatClient, cfg: dict | None = None) -> PromptGateway:
    cfg = cfg or {}
    provider_name = os.environ.get("GOVCHECK_PROMPT_PROVIDER", cfg.get("prompt_provider", "disabled"))
    label = os.environ.get("GOVCHECK_PROMPT_LABEL", cfg.get("prompt_label", "production"))
    provider: PromptProvider = NullPromptProvider()
    # 進入 cache namespace 的 prompt 來源識別；換來源即換 namespace，避免沿用舊判讀
    provider_source = "disabled"
    if provider_name == "litellm":
        base_url = os.environ.get("GOVCHECK_PROMPT_BASE_URL") or cfg.get("prompt_base_url")
        if base_url:
            api_key = os.environ.get("GOVCHECK_PROMPT_API_KEY")
            timeout = float(os.environ.get("GOVCHECK_PROMPT_TIMEOUT", "10"))
            provider = _shared_litellm_provider(
                base_url=base_url, api_key=api_key, timeout=timeout
            )
            provider_source = base_url
    elif provider_name == "file":
        directory = os.environ.get("GOVCHECK_PROMPT_DIR") or cfg.get("prompt_dir") or "prompts"
        provider = FilePromptProvider(directory=directory)
        provider_source = str(directory)
    namespace_source = "|".join([
        provider_name,
        str(cfg.get("base_url") or ""),
        str(cfg.get("model") or ""),
        provider_source,
        str(label),
    ])
    namespace = hashlib.sha256(namespace_source.encode()).hexdigest()[:16]
    return PromptGateway(
        provider=provider,
        client=client,
        label=label,
        cache_namespace=namespace,
    )


_PROVIDER_CACHE: dict[tuple[str, str, float], LiteLLMPromptProvider] = {}
_PROVIDER_CACHE_LOCK = threading.Lock()


def _shared_litellm_provider(
    *, base_url: str, api_key: str | None, timeout: float
) -> LiteLLMPromptProvider:
    key_hash = hashlib.sha256((api_key or "").encode()).hexdigest()
    key = (base_url.rstrip("/"), key_hash, timeout)
    with _PROVIDER_CACHE_LOCK:
        provider = _PROVIDER_CACHE.get(key)
        if provider is None:
            provider = LiteLLMPromptProvider(base_url=base_url, api_key=api_key, timeout=timeout)
            _PROVIDER_CACHE[key] = provider
    return provider
