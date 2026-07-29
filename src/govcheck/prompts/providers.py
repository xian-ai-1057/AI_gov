"""Provider-neutral prompt retrieval with exact-version process caching."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests
import yaml

from govcheck.paths import resolve_data_path


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


class FilePromptProvider:
    """地端檔案 Prompt 來源。

    佈局（label 子目錄優先，找不到才退回目錄根，故既有扁平佈局不受影響）：

        <dir>/<label>/<prompt_id>.yaml   例：prompts/lenient/govcheck.f03.fairness.yaml
        <dir>/<prompt_id>.yaml           預設集（label=production 時即用此份）

    正式 Prompt 內含治理辦法衍生的判準文字，依專案守則不入庫，故目錄由設定指向
    gitignored 位置。以 mtime 快取：檔案未變更時沿用已解析的 template，改檔後
    下一次 review 自動重讀，本機調 prompt 不必重啟服務。
    """

    def __init__(self, *, directory: str | Path) -> None:
        # 同 rag mapping：設定給的相對路徑錨定 repo root，否則從其他 cwd 啟動會整批降級為未判讀
        self.directory = resolve_data_path(directory)
        self._cache: dict[tuple[str, str | None], tuple[float, PromptTemplate]] = {}
        self._lock = threading.Lock()

    def resolve(
        self,
        prompt_id: str,
        *,
        label: str | None = None,
        version: str | None = None,
    ) -> PromptTemplate:
        path = self._path_for(prompt_id, label)
        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            raise PromptUnavailable(f"Prompt 檔案不存在：{prompt_id}") from exc
        # label 進入快取鍵：嚴格版與寬鬆版是不同檔案，不得互相污染
        key = (prompt_id, label)
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None and cached[0] == mtime:
            template = cached[1]
        else:
            template = self._load(prompt_id, path)
            with self._lock:
                self._cache[key] = (mtime, template)
        if version is not None and version != template.version:
            raise PromptUnavailable(f"Prompt 版本不符：{prompt_id}@{version}")
        return template

    def _path_for(self, prompt_id: str, label: str | None) -> Path:
        filename = f"{prompt_id}.yaml"
        if label:
            # label 只當單層目錄名；'..' 或路徑分隔字元不得逸出 prompt 目錄
            if "/" in label or "\\" in label or label in {".", ".."}:
                raise PromptUnavailable(f"Prompt label 不合法：{label}")
            labelled = self.directory / label / filename
            if labelled.exists():
                return labelled
        return self.directory / filename

    def _load(self, prompt_id: str, path: Path) -> PromptTemplate:
        try:
            body = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(body, dict):
                raise TypeError("prompt 檔案必須是 mapping")
            if body.get("prompt_id") != prompt_id:
                raise ValueError("prompt_id 與檔名不符")
            declared_version = body.get("version")
            if declared_version in (None, ""):
                raise ValueError("prompt 檔案必須宣告 version")
            messages = body["messages"]
            if not isinstance(messages, list):
                raise TypeError("messages 必須是 list")
            return PromptTemplate.create(prompt_id, str(declared_version), messages)
        except (OSError, yaml.YAMLError, ValueError, KeyError, TypeError) as exc:
            raise PromptUnavailable(
                f"Prompt 檔案格式不符契約：{prompt_id}（{type(exc).__name__}）"
            ) from exc


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
