"""OpenAI 相容 embeddings 客戶端（以 requests 實作）。

只負責 HTTP 與回應解析；任何失敗（連線/逾時/非 2xx/格式異常/長度不符）一律轉成
EmbeddingError，由上層接住並降級，確保介面與規則檢查不因 embedding 端點問題中斷。
"""

from __future__ import annotations

import os

import requests

from govcheck.logging_setup import get_logger
from govcheck.rag.config import load_rag_config

log = get_logger("rag_embedding")


class EmbeddingError(RuntimeError):
    """Embedding 端點呼叫或回應解析失敗。"""


class EmbeddingClient:
    """最小 OpenAI 相容 embeddings 客戶端。"""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    @classmethod
    def from_config(cls, cfg: dict | None = None) -> EmbeddingClient:
        """由 RAG 設定 dict 建立客戶端。

        cfg 為 None 時呼叫 load_rag_config()。
        api_key 一律由環境變數 GOVCHECK_RAG_EMBEDDING_API_KEY 取得，
        不從 cfg dict 讀取（避免金鑰誤寫入設定檔被入庫）。
        """
        cfg = cfg or load_rag_config()
        return cls(
            base_url=cfg["embedding_base_url"],
            model=cfg["embedding_model"],
            api_key=os.environ.get("GOVCHECK_RAG_EMBEDDING_API_KEY"),
            timeout=cfg["timeout"],
        )

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/embeddings"

    def embed(self, texts: list[str]) -> list[list[float]]:
        """送出 embeddings 請求，回傳與輸入順序相同的向量列表；失敗丟 EmbeddingError。"""
        payload = {"model": self.model, "input": texts}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
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
