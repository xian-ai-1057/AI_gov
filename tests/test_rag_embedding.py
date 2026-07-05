"""EmbeddingClient 測試：全程 mock requests.post，不打真端點（p3-01 spec §4.4）。

涵蓋 AC：
- AC-11：2xx 正常回傳；向量順序與長度對齊輸入
- AC-12：4xx 丟 EmbeddingError
- AC-13：requests.RequestException 丟 EmbeddingError
- AC-14：格式異常（缺 data key / 筆數不符 / 非預期結構）丟 EmbeddingError
- AC-15：任何 log record 不含 input 文字；只記端點/狀態碼/例外型別
- AC-16：from_config 正確取 base_url/model/timeout；api_key 僅走 env，YAML 內不採用
"""

from __future__ import annotations

import pytest
import requests

from govcheck.rag.embedding import EmbeddingClient, EmbeddingError


# ── 共用 mock 工具 ────────────────────────────────────────────────────────────

class _Resp:
    """模擬 requests.Response。"""

    def __init__(self, status: int, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def _ok_resp(embeddings: list[list[float]]) -> _Resp:
    """製造 OpenAI 相容的 2xx embedding 回應。"""
    return _Resp(
        200,
        {"data": [{"embedding": v, "index": i} for i, v in enumerate(embeddings)]},
    )


def _make_client(base_url: str = "http://host/v1", model: str = "bge-m3") -> EmbeddingClient:
    return EmbeddingClient(base_url=base_url, model=model, timeout=5.0)


# ── AC-11：2xx 正常 ───────────────────────────────────────────────────────────


def test_embed_returns_vectors_in_order(monkeypatch):
    """AC-11：embed([t1,t2,t3]) 回傳與輸入同長、同順序的向量 list。"""
    vecs = [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, timeout=timeout)
        return _ok_resp(vecs)

    monkeypatch.setattr(requests, "post", fake_post)
    client = _make_client()
    result = client.embed(["a", "b", "c"])

    assert len(result) == 3
    assert result == vecs
    assert captured["url"] == "http://host/v1/embeddings"
    assert captured["json"]["model"] == "bge-m3"
    assert captured["json"]["input"] == ["a", "b", "c"]
    assert captured["timeout"] == 5.0


def test_embed_single_text(monkeypatch):
    """AC-11：embed([t]) 回傳單一向量 list（長度 1）。"""
    monkeypatch.setattr(requests, "post", lambda url, **kw: _ok_resp([[1.0, 0.0]]))
    result = _make_client().embed(["hello"])
    assert len(result) == 1
    assert result[0] == [1.0, 0.0]


def test_embed_sends_bearer_token(monkeypatch):
    """AC-11：設有 api_key 時送出 Authorization: Bearer ...。"""
    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["headers"] = headers
        return _ok_resp([[0.5]])

    monkeypatch.setattr(requests, "post", fake_post)
    client = EmbeddingClient(base_url="http://h/v1", model="m", api_key="sk-xyz", timeout=5.0)
    client.embed(["text"])
    assert captured["headers"]["Authorization"] == "Bearer sk-xyz"


def test_embed_no_auth_header_without_key(monkeypatch):
    """AC-11：無 api_key 時不傳 Authorization header。"""
    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["headers"] = headers
        return _ok_resp([[0.1]])

    monkeypatch.setattr(requests, "post", fake_post)
    _make_client().embed(["x"])
    assert "Authorization" not in captured["headers"]


def test_endpoint_property():
    """AC-11：endpoint 屬性 = base_url + '/embeddings'（尾斜線被去除）。"""
    c = EmbeddingClient(base_url="http://host:11434/v1/", model="bge-m3")
    assert c.endpoint == "http://host:11434/v1/embeddings"


# ── AC-12：4xx HTTP 錯誤 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422, 500, 503])
def test_embed_http_error_raises_embedding_error(monkeypatch, status_code):
    """AC-12：status_code >= 400 → EmbeddingError。"""
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp(status_code))
    with pytest.raises(EmbeddingError):
        _make_client().embed(["test"])


# ── AC-13：連線失敗 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("exc_cls", [
    requests.ConnectionError,
    requests.Timeout,
    requests.RequestException,
])
def test_embed_connection_exception_raises_embedding_error(monkeypatch, exc_cls):
    """AC-13：requests.RequestException 各子類 → EmbeddingError。"""
    def boom(url, **kw):
        raise exc_cls("connection refused")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(EmbeddingError):
        _make_client().embed(["text"])


# ── AC-14：格式異常 ───────────────────────────────────────────────────────────


def test_embed_missing_data_key_raises(monkeypatch):
    """AC-14：2xx 但 JSON 缺 'data' key → EmbeddingError。"""
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp(200, {"result": []}))
    with pytest.raises(EmbeddingError):
        _make_client().embed(["x"])


def test_embed_wrong_count_raises(monkeypatch):
    """AC-14：回傳筆數 ≠ 輸入筆數 → EmbeddingError。"""
    # 輸入 3 筆，回傳 1 筆
    monkeypatch.setattr(requests, "post", lambda url, **kw: _ok_resp([[0.1, 0.2]]))
    with pytest.raises(EmbeddingError):
        _make_client().embed(["a", "b", "c"])


def test_embed_data_not_list_raises(monkeypatch):
    """AC-14：data 欄位非 list（是 int）→ EmbeddingError。"""
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp(200, {"data": 42}))
    with pytest.raises(EmbeddingError):
        _make_client().embed(["x"])


def test_embed_missing_embedding_key_raises(monkeypatch):
    """AC-14：data[i] 缺 'embedding' key → EmbeddingError。"""
    bad = _Resp(200, {"data": [{"no_embedding": [0.1]}]})
    monkeypatch.setattr(requests, "post", lambda url, **kw: bad)
    with pytest.raises(EmbeddingError):
        _make_client().embed(["x"])


def test_embed_invalid_json_raises(monkeypatch):
    """AC-14：resp.json() 拋 ValueError（非 JSON body）→ EmbeddingError。"""
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp(200, None, "not json"))
    with pytest.raises(EmbeddingError):
        _make_client().embed(["x"])


# ── AC-15：log 不含 input 文字 ───────────────────────────────────────────────


def test_log_does_not_contain_input_text_on_success(monkeypatch, caplog):
    """AC-15（成功路徑）：log record 不含 input 文字。"""
    import logging
    input_text = "這是機密輸入文字，絕不應出現於 log"
    monkeypatch.setattr(requests, "post", lambda url, **kw: _ok_resp([[0.1]]))
    with caplog.at_level(logging.DEBUG, logger="govcheck.rag_embedding"):
        _make_client().embed([input_text])

    for record in caplog.records:
        assert input_text not in record.getMessage(), (
            f"log message 含 input 文字（AC-15 違規）: {record.getMessage()!r}"
        )


def test_log_does_not_contain_input_text_on_http_error(monkeypatch, caplog):
    """AC-15（4xx 路徑）：log record 不含 input 文字；僅記端點/狀態碼。"""
    import logging
    input_text = "confidential_query_text_abc123"
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp(503))
    with caplog.at_level(logging.WARNING, logger="govcheck.rag_embedding"):
        with pytest.raises(EmbeddingError):
            _make_client().embed([input_text])

    for record in caplog.records:
        assert input_text not in record.getMessage(), (
            f"log message 含 input 文字（AC-15 違規）: {record.getMessage()!r}"
        )


def test_log_does_not_contain_input_text_on_connection_error(monkeypatch, caplog):
    """AC-15（連線失敗路徑）：log record 不含 input 文字；僅記端點/例外型別。"""
    import logging
    input_text = "private_embedding_input_xyz"

    def boom(url, **kw):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests, "post", boom)
    with caplog.at_level(logging.WARNING, logger="govcheck.rag_embedding"):
        with pytest.raises(EmbeddingError):
            _make_client().embed([input_text])

    for record in caplog.records:
        assert input_text not in record.getMessage(), (
            f"log message 含 input 文字（AC-15 違規）: {record.getMessage()!r}"
        )


# ── AC-16：from_config + api_key 只走 env ─────────────────────────────────────


def test_from_config_reads_base_url_model_timeout():
    """AC-16：from_config(cfg) 正確取 embedding_base_url / embedding_model / timeout。"""
    cfg = {
        "embedding_base_url": "http://internal:9090/v1",
        "embedding_model": "bge-large",
        "timeout": 45.0,
        # 刻意不含 api_key
    }
    client = EmbeddingClient.from_config(cfg)
    assert client.base_url == "http://internal:9090/v1"
    assert client.model == "bge-large"
    assert client.timeout == 45.0


def test_from_config_api_key_from_env(monkeypatch):
    """AC-16：api_key 僅從 GOVCHECK_RAG_EMBEDDING_API_KEY 取得，cfg dict 中的值不採用。"""
    monkeypatch.setenv("GOVCHECK_RAG_EMBEDDING_API_KEY", "env-secret-key")
    cfg = {
        "embedding_base_url": "http://h/v1",
        "embedding_model": "bge-m3",
        "timeout": 10.0,
        # 即使 cfg 中含 api_key 也不採用（只走 env）
        "api_key": "this-should-be-ignored",
    }
    client = EmbeddingClient.from_config(cfg)
    assert client.api_key == "env-secret-key"


def test_from_config_no_api_key_without_env(monkeypatch):
    """AC-16：無 GOVCHECK_RAG_EMBEDDING_API_KEY env 時 api_key = None。"""
    monkeypatch.delenv("GOVCHECK_RAG_EMBEDDING_API_KEY", raising=False)
    cfg = {
        "embedding_base_url": "http://h/v1",
        "embedding_model": "bge-m3",
        "timeout": 10.0,
    }
    client = EmbeddingClient.from_config(cfg)
    assert client.api_key is None


def test_from_config_none_uses_load_rag_config(monkeypatch):
    """AC-16：from_config(None) 呼叫 load_rag_config() 取設定。"""
    monkeypatch.delenv("GOVCHECK_RAG_EMBEDDING_API_KEY", raising=False)
    # load_rag_config 的預設 embedding_model = 'bge-m3'
    client = EmbeddingClient.from_config(None)
    assert client.model == "bge-m3"
