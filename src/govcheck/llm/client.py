"""OpenAI 相容 chat completions 客戶端（以 requests 實作）。

只負責 HTTP 與回應解析；任何失敗（連線/逾時/非 2xx/格式異常）一律轉成 LLMError，
由上層 checks/llm 接住並降級為 Finding，確保介面與規則檢查不因 LLM 端點問題中斷。
"""

from __future__ import annotations

import json

import requests

from govcheck.llm.config import load_llm_config
from govcheck.logging_setup import dev_mode, dump_llm_raw, get_logger, get_request_id

log = get_logger("llm_client")

# dev 終端截斷長度（完整內容另存 logs/llm_raw/；prod/quiet 不觸發）。
_REQ_TRUNC = 300
_RESP_TRUNC = 500


def _trunc(text: str | None, limit: int) -> str:
    """終端顯示用截斷：壓成單行、超長補「…」（僅 dev DEBUG 用，完整內容已另存檔）。"""
    s = "" if text is None else " ".join(str(text).split())
    return s if len(s) <= limit else s[:limit] + "…"


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

    def chat(self, messages: list[dict], *, temperature: float | None = None, schema: dict | None = None) -> str:
        """送出 chat completions 請求，回傳 assistant 訊息 content；失敗丟 LLMError。

        schema：提供時以 OpenAI `json_schema` 結構化輸出模式約束回應（strict，欄位/型別在解碼階段
        即強制符合，較舊的 json_object 更穩定）；不支援的端點通常忽略此鍵（仍靠 prompt 約束輸出）。
        """
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
        }
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "govcheck_result", "schema": schema, "strict": True},
            }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            resp = requests.post(self.endpoint, json=payload, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            # 只記端點與例外型別；不記 payload（含佐證內容）
            log.warning("llm request failed endpoint=%s err=%s", self.endpoint, type(exc).__name__)
            if dev_mode():  # dev-only：連線失敗無 response，仍存 request 全文供除錯
                path = dump_llm_raw({
                    "status": "connection_error", "endpoint": self.endpoint,
                    "model": self.model, "error": str(exc), "request_messages": messages,
                })
                log.debug("llm conn error request_id=%s endpoint=%s req=%s raw=%s",
                          get_request_id(), self.endpoint, _trunc(str(messages), _REQ_TRUNC), path)
            raise LLMError(f"無法連線 LLM 端點（{self.endpoint}）：{exc}") from exc

        if resp.status_code >= 400:
            # 只記狀態碼；不記 resp.text（可能含佐證內容或 prompt 片段）
            log.warning("llm endpoint http %d endpoint=%s", resp.status_code, self.endpoint)
            if dev_mode():  # dev-only：存 request + HTTP 回應本文全文，終端印截斷
                path = dump_llm_raw({
                    "status": f"http_{resp.status_code}", "endpoint": self.endpoint,
                    "model": self.model, "request_messages": messages, "http_body": resp.text,
                })
                log.debug("llm http %d request_id=%s body=%s raw=%s",
                          resp.status_code, get_request_id(), _trunc(resp.text, _RESP_TRUNC), path)
            raise LLMError(f"LLM 端點回應 HTTP {resp.status_code}")

        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM 回應格式非預期：{exc}") from exc

        if dev_mode():  # dev-only：每次呼叫都存完整 request/response，終端印截斷版
            path = dump_llm_raw({
                "status": "ok", "endpoint": self.endpoint, "model": self.model,
                "request_messages": messages, "response_text": content,
            })
            log.debug("llm call ok request_id=%s endpoint=%s req=%s resp=%s raw=%s",
                      get_request_id(), self.endpoint,
                      _trunc(str(messages), _REQ_TRUNC), _trunc(content, _RESP_TRUNC), path)
        return content


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
            if dev_mode():  # dev-only：終端直接點出實際回了什麼（完整內容由 chat() 存 llm_raw/）
                log.debug("llm json no-brace request_id=%s excerpt=%s",
                          get_request_id(), _trunc(content, _RESP_TRUNC))
            raise LLMError("LLM 回應非 JSON（無 `{` 起始字元）") from None
        try:
            obj, _ = json.JSONDecoder().raw_decode(text, start)
        except json.JSONDecodeError as exc:
            if dev_mode():  # dev-only：帶 request_id 可回查 logs/llm_raw/ 的完整回應
                log.debug("llm json parse failed request_id=%s err=%s excerpt=%s",
                          get_request_id(), exc, _trunc(content, _RESP_TRUNC))
            raise LLMError(f"LLM 回應 JSON 解析失敗：{exc}") from exc
    if not isinstance(obj, dict):
        raise LLMError("LLM 回應 JSON 不是物件")
    return obj
