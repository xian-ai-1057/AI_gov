"""OpenAI 相容 chat completions 客戶端（以 requests 實作）。

只負責 HTTP 與回應解析；任何失敗（連線/逾時/非 2xx/格式異常）一律轉成 LLMError，
由上層 checks/llm 接住並降級為 Finding，確保介面與規則檢查不因 LLM 端點問題中斷。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass

import requests

from govcheck.llm.config import load_llm_config
from govcheck.logging_setup import get_logger
from govcheck.runtime_limits import MODEL_CALL_SEMAPHORE

log = get_logger("llm_client")

class LLMError(RuntimeError):
    """LLM 端點呼叫或回應解析失敗。"""


@dataclass(frozen=True)
class ChatResult:
    content: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


class ChatClient:
    """最小 OpenAI 相容 chat completions 客戶端。"""

    def __init__(
        self,
        *,
        base_url: str,
        model: str | None,
        api_key: str | None = None,
        timeout: float = 60,
        temperature: float = 0.0,
        max_output_tokens: int = 4096,
        max_retries: int = 1,
        circuit_failures: int = 3,
        circuit_cooldown: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature
        self.max_output_tokens = max(1, max_output_tokens)
        self.max_retries = max(0, max_retries)
        self.circuit_failures = max(1, circuit_failures)
        self.circuit_cooldown = max(0.0, circuit_cooldown)
        self._failure_count = 0
        self._circuit_open_until = 0.0
        self._state_lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg: dict | None = None) -> ChatClient:
        cfg = cfg or load_llm_config()
        return cls(
            base_url=cfg["base_url"],
            model=cfg["model"],
            api_key=cfg.get("api_key"),
            timeout=cfg["timeout"],
            temperature=cfg["temperature"],
            max_output_tokens=cfg.get("max_output_tokens", 4096),
        )

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def chat(self, messages: list[dict], *, temperature: float | None = None, schema: dict | None = None) -> str:
        """送出 chat completions 請求，回傳 assistant 訊息 content；失敗丟 LLMError。

        schema：提供時以 OpenAI `json_schema` 結構化輸出模式約束回應（strict，欄位/型別在解碼階段
        即強制符合，較舊的 json_object 更穩定）；不支援的端點通常忽略此鍵（仍靠 prompt 約束輸出）。
        """
        return self.chat_result(messages, temperature=temperature, schema=schema).content

    def chat_result(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        schema: dict | None = None,
    ) -> ChatResult:
        """Return content plus endpoint usage without logging request/response bodies."""
        with self._state_lock:
            if time.monotonic() < self._circuit_open_until:
                raise LLMError("LLM circuit breaker 暫時開啟")

        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_output_tokens,
        }
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "govcheck_result", "schema": schema, "strict": True},
            }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        started = time.perf_counter()
        resp = None
        last_exc: requests.RequestException | None = None
        with MODEL_CALL_SEMAPHORE:
            for attempt in range(self.max_retries + 1):
                try:
                    resp = requests.post(
                        self.endpoint, json=payload, headers=headers, timeout=self.timeout
                    )
                    if resp.status_code < 500 or attempt >= self.max_retries:
                        break
                except requests.RequestException as exc:
                    last_exc = exc
                    if attempt >= self.max_retries:
                        break
        if resp is None:
            self._record_failure()
            err_name = type(last_exc).__name__ if last_exc else "RequestException"
            log.warning("llm request failed endpoint=%s err=%s", self.endpoint, err_name)
            raise LLMError(f"無法連線 LLM 端點（{self.endpoint}）：{err_name}") from last_exc

        if resp.status_code >= 400:
            # 只記狀態碼；不記 resp.text（可能含佐證內容或 prompt 片段）
            log.warning("llm endpoint http %d endpoint=%s", resp.status_code, self.endpoint)
            self._record_failure()
            raise LLMError(f"LLM 端點回應 HTTP {resp.status_code}")

        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            self._record_failure()
            raise LLMError(f"LLM 回應格式非預期：{exc}") from exc

        usage = data.get("usage") if isinstance(data, dict) else {}
        usage = usage if isinstance(usage, dict) else {}
        with self._state_lock:
            self._failure_count = 0
            self._circuit_open_until = 0.0
        return ChatResult(
            content=str(content),
            input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
            latency_ms=round((time.perf_counter() - started) * 1000),
        )

    def _record_failure(self) -> None:
        with self._state_lock:
            self._failure_count += 1
            if self._failure_count >= self.circuit_failures:
                self._circuit_open_until = time.monotonic() + self.circuit_cooldown


def parse_json_object(content: str | None) -> dict:
    """把（可能含 ```json 圍欄或雜訊的）模型輸出解析成 JSON 物件；失敗丟 LLMError。"""
    if not content or not content.strip():
        raise LLMError("LLM 回應為空")
    text = content.strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        # 退而求其次：從第一個 { 起用 raw_decode 解析「第一個完整物件」，
        # 容忍圍欄/前後說明文字，且不會誤把尾端文字裡的 } 一起吞進來。
        start = text.find("{")
        if start == -1:
            raise LLMError("LLM 回應非 JSON（無 `{` 起始字元）") from None
        try:
            obj, _ = json.JSONDecoder().raw_decode(text, start)
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM 回應 JSON 解析失敗：{exc}") from exc
    if not isinstance(obj, dict):
        raise LLMError("LLM 回應 JSON 不是物件")
    return obj
