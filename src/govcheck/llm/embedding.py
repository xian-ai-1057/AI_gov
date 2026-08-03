"""OpenAI 相容 embeddings 客戶端（以 requests 實作）。

只負責 HTTP 與回應解析；任何失敗（連線/逾時/非 2xx/格式異常/長度不符）一律轉成
EmbeddingError，由上層接住並降級，確保介面與規則檢查不因 embedding 端點問題中斷。
"""

from __future__ import annotations

import os

import requests

from govcheck.llm.config import load_llm_config
from govcheck.logging_setup import get_logger
from govcheck.runtime_limits import MODEL_CALL_SEMAPHORE

log = get_logger("embedding")


class EmbeddingError(RuntimeError):
    """Embedding 端點呼叫或回應解析失敗。"""


class EmbeddingContextLengthError(EmbeddingError):
    """One embedding batch exceeded an endpoint-reported context/input limit."""


class EmbeddingClient:
    """最小 OpenAI 相容 embeddings 客戶端。"""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 120.0,
        batch_size: int = 16,
        max_input_chars: int = 2000,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.batch_size = max(1, batch_size)
        self.max_input_chars = max(100, max_input_chars)
        self.last_request_count = 0

    @classmethod
    def from_config(cls, cfg: dict | None = None) -> EmbeddingClient:
        """由 LLM 設定 dict 建立客戶端。

        cfg 為 None 時呼叫 load_llm_config()。
        api_key 一律由環境變數 GOVCHECK_EMBEDDING_API_KEY 取得，
        不從 cfg dict 讀取（避免金鑰誤寫入設定檔被入庫）。
        """
        cfg = cfg or load_llm_config()
        return cls(
            base_url=cfg["embedding_base_url"],
            model=cfg["embedding_model"],
            api_key=os.environ.get("GOVCHECK_EMBEDDING_API_KEY"),
            timeout=cfg["embedding_timeout"],
            batch_size=cfg.get("embedding_batch_size", 16),
            max_input_chars=cfg.get("embedding_max_input_chars", 2000),
        )

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/embeddings"

    def embed(self, texts: list[str]) -> list[list[float]]:
        """送出 embeddings 請求，回傳與輸入順序相同的向量列表；失敗丟 EmbeddingError。"""
        self.last_request_count = 0
        bounded = [_bounded_embedding_text(text, self.max_input_chars) for text in texts]
        vectors: list[list[float]] = []
        for start in range(0, len(bounded), self.batch_size):
            vectors.extend(
                self._embed_with_context_split(bounded[start:start + self.batch_size])
            )
        return vectors

    def _embed_with_context_split(self, texts: list[str]) -> list[list[float]]:
        try:
            return self._embed_batch(texts)
        except EmbeddingContextLengthError:
            if len(texts) <= 1:
                raise
            midpoint = len(texts) // 2
            log.info("embedding context retry split items=%d", len(texts))
            return [
                *self._embed_with_context_split(texts[:midpoint]),
                *self._embed_with_context_split(texts[midpoint:]),
            ]

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload = {"model": self.model, "input": texts}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            self.last_request_count += 1
            with MODEL_CALL_SEMAPHORE:
                resp = requests.post(
                    self.endpoint, json=payload, headers=headers, timeout=self.timeout
                )
        except requests.RequestException as exc:
            # 只記端點與例外型別；不記 payload（含輸入文字）
            log.warning(
                "embed request failed endpoint=%s err=%s", self.endpoint, type(exc).__name__
            )
            raise EmbeddingError(
                f"無法連線 embedding 端點（{self.endpoint}）：{exc}"
            ) from exc

        if resp.status_code >= 400:
            # 只記狀態碼；不記 resp.text
            log.warning("embed endpoint http %d endpoint=%s", resp.status_code, self.endpoint)
            if _is_context_length_error(resp):
                raise EmbeddingContextLengthError(
                    "embedding 端點拒絕超過 context/input 上限的批次"
                )
            raise EmbeddingError(f"embedding 端點回應 HTTP {resp.status_code}")

        try:
            data = resp.json()
            vectors: list[list[float]] = [item["embedding"] for item in data["data"]]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise EmbeddingError(f"embedding 回應格式非預期：{exc}") from exc

        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"embedding 回傳數量不符：預期 {len(texts)} 筆，實得 {len(vectors)} 筆"
            )

        log.debug("embed ok endpoint=%s n=%d", self.endpoint, len(texts))
        return vectors


def _bounded_embedding_text(text: str, max_chars: int) -> str:
    """Bound one embedding input while sampling the whole input deterministically."""
    if len(text) <= max_chars:
        return text
    marker = "…"
    part = max(1, (max_chars - 2 * len(marker)) // 3)
    middle = max(0, len(text) // 2 - part // 2)
    return marker.join((text[:part], text[middle:middle + part], text[-part:]))[:max_chars]


def _is_context_length_error(resp) -> bool:
    """Inspect a structured error privately; never expose response text or embedded input."""
    try:
        body = resp.json()
    except (ValueError, TypeError):
        return False
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return False
    code = str(error.get("code") or "").casefold()
    param = str(error.get("param") or "").casefold()
    message = str(error.get("message") or "").casefold()
    return (
        code in {"context_length_exceeded", "max_context_length_exceeded"}
        or "maximum context length" in message
        or "context window" in message and "token" in message
        or param in {"input", "input_tokens"} and "token" in message
    )
