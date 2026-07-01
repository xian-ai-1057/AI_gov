"""OpenAI 相容 chat completions 客戶端（以 requests 實作）。

只負責 HTTP 與回應解析；任何失敗（連線/逾時/非 2xx/格式異常）一律轉成 LLMError，
由上層 checks/llm 接住並降級為 Finding，確保介面與規則檢查不因 LLM 端點問題中斷。
"""

from __future__ import annotations

import json

import requests

from govcheck.llm.config import load_llm_config
from govcheck.logging_setup import get_logger

log = get_logger("llm_client")


class LLMError(RuntimeError):
    """LLM 端點呼叫或回應解析失敗。"""


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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature

    @classmethod
    def from_config(cls, cfg: dict | None = None) -> ChatClient:
        cfg = cfg or load_llm_config()
        return cls(
            base_url=cfg["base_url"],
            model=cfg["model"],
            api_key=cfg.get("api_key"),
            timeout=cfg["timeout"],
            temperature=cfg["temperature"],
        )

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def chat(self, messages: list[dict], *, temperature: float | None = None, want_json: bool = True) -> str:
        """送出 chat completions 請求，回傳 assistant 訊息 content；失敗丟 LLMError。"""
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
        }
        if want_json:
            # OpenAI 相容的 JSON 模式；不支援的端點通常忽略此鍵（仍靠 prompt 約束輸出）
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            resp = requests.post(self.endpoint, json=payload, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            # 只記端點與例外型別；不記 payload（含佐證內容）
            log.warning("llm request failed endpoint=%s err=%s", self.endpoint, type(exc).__name__)
            raise LLMError(f"無法連線 LLM 端點（{self.endpoint}）：{exc}") from exc

        if resp.status_code >= 400:
            # 只記狀態碼；不記 resp.text（可能含佐證內容或 prompt 片段）
            log.warning("llm endpoint http %d endpoint=%s", resp.status_code, self.endpoint)
            raise LLMError(f"LLM 端點回應 HTTP {resp.status_code}")

        try:
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM 回應格式非預期：{exc}") from exc


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
