"""Provider-neutral prompt retrieval with exact-version process caching."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Protocol

import requests


class PromptUnavailable(RuntimeError):
    """The requested exact prompt version is not available."""


@dataclass(frozen=True)
class PromptTemplate:
    prompt_id: str
    version: str
    messages: tuple[dict[str, str], ...]
    content_hash: str

    @classmethod
    def create(
        cls,
        prompt_id: str,
        version: str,
        messages: list[dict[str, str]] | tuple[dict[str, str], ...],
    ) -> PromptTemplate:
        if not messages:
            raise ValueError("prompt messages must not be empty")
        normalized_rows = []
        for row in messages:
            role = row["role"]
            content = row["content"]
            if role not in {"system", "developer", "user", "assistant"}:
                raise ValueError(f"unsupported prompt role: {role}")
            if not isinstance(content, str) or not content:
                raise ValueError("prompt message content must be a non-empty string")
            normalized_rows.append({"role": role, "content": content})
        normalized = tuple(normalized_rows)
        digest = hashlib.sha256(
            json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        return cls(prompt_id=prompt_id, version=version, messages=normalized, content_hash=digest)


class PromptProvider(Protocol):
    def resolve(
        self,
        prompt_id: str,
        *,
        label: str | None = None,
        version: str | None = None,
    ) -> PromptTemplate: ...


class NullPromptProvider:
    def resolve(
        self,
        prompt_id: str,
        *,
        label: str | None = None,
        version: str | None = None,
    ) -> PromptTemplate:
        del label, version
        raise PromptUnavailable(f"Prompt provider 未設定：{prompt_id}")


class StaticPromptProvider:
    """Explicit test/development provider; callers inject synthetic templates."""

    def __init__(self, templates: list[PromptTemplate]) -> None:
        self._by_id = {template.prompt_id: template for template in templates}

    def resolve(
        self,
        prompt_id: str,
        *,
        label: str | None = None,
        version: str | None = None,
    ) -> PromptTemplate:
        del label
        try:
            template = self._by_id[prompt_id]
        except KeyError as exc:
            raise PromptUnavailable(f"Static prompt 不存在：{prompt_id}") from exc
        if version is not None and version != template.version:
            raise PromptUnavailable(f"Static prompt 版本不存在：{prompt_id}@{version}")
        return template


class LiteLLMPromptProvider:
    """Adapter for LiteLLM's Generic Prompt Management API (currently beta)."""

    def __init__(self, *, base_url: str, api_key: str | None, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._cache: dict[tuple[str, str], PromptTemplate] = {}
        self._lock = threading.Lock()

    def resolve(
        self,
        prompt_id: str,
        *,
        label: str | None = None,
        version: str | None = None,
    ) -> PromptTemplate:
        # Labels are mutable release pointers and therefore must be resolved for
        # every review run.  Only an immutable version may be served from the
        # process cache; this also prevents a production-label update from being
        # hidden until the application restarts.
        if version is not None:
            with self._lock:
                cached = self._cache.get((prompt_id, version))
            if cached is not None:
                return cached

        params = {"prompt_id": prompt_id}
        if version:
            params["prompt_version"] = version
        elif label:
            params["prompt_label"] = label
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = requests.get(
                f"{self.base_url}/beta/litellm_prompt_management",
                params=params,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise PromptUnavailable(f"Prompt service 連線失敗：{type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise PromptUnavailable(f"Prompt service HTTP {response.status_code}")
        try:
            body = response.json()
            if body.get("prompt_id") != prompt_id:
                raise ValueError("prompt_id mismatch")
            messages = body["prompt_template"]
            resolved_version_value = body.get("prompt_version")
            if resolved_version_value is None:
                raise ValueError("prompt_version is required for immutable runs")
            resolved_version = str(resolved_version_value)
            if not isinstance(messages, list):
                raise TypeError("prompt_template must be a list")
            template = PromptTemplate.create(prompt_id, resolved_version, messages)
        except (ValueError, KeyError, TypeError) as exc:
            raise PromptUnavailable("Prompt service 回傳格式不符契約") from exc
        with self._lock:
            self._cache[(prompt_id, template.version)] = template
        return template
