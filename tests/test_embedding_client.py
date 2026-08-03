from __future__ import annotations

import requests

from govcheck.llm.embedding import EmbeddingClient, EmbeddingError


class _Resp:
    status_code = 200

    def __init__(self, count: int) -> None:
        self.count = count

    def json(self):
        return {"data": [{"embedding": [float(index)]} for index in range(self.count)]}


class _ContextResp:
    status_code = 400

    def json(self):
        return {
            "error": {
                "message": "maximum context length exceeded; reduce input tokens",
                "param": "input_tokens",
                "code": 400,
            }
        }


class _Semaphore:
    def __init__(self) -> None:
        self.entries = 0

    def __enter__(self):
        self.entries += 1

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_embedding_client_bounds_inputs_and_splits_http_batches(monkeypatch):
    payloads = []

    def fake_post(url, *, json, headers, timeout):
        payloads.append(json)
        return _Resp(len(json["input"]))

    monkeypatch.setattr(requests, "post", fake_post)
    client = EmbeddingClient(
        base_url="http://h/v1",
        model="embed",
        batch_size=2,
        max_input_chars=100,
    )

    vectors = client.embed(["短文", "中" * 1000, "第三筆", "第四筆", "第五筆"])

    assert len(payloads) == 3
    assert client.last_request_count == 3
    assert len(vectors) == 5
    assert all(len(text) <= 100 for payload in payloads for text in payload["input"])
    bounded_long = payloads[0]["input"][1]
    assert bounded_long.startswith("中") and "…" in bounded_long


def test_embedding_client_reports_requests_before_later_batch_failure(monkeypatch):
    calls = 0

    def fake_post(url, *, json, headers, timeout):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise requests.ConnectionError("down")
        return _Resp(len(json["input"]))

    monkeypatch.setattr(requests, "post", fake_post)
    client = EmbeddingClient(
        base_url="http://h/v1", model="embed", batch_size=2
    )

    try:
        client.embed(["a", "b", "c"])
    except EmbeddingError:
        pass
    else:
        raise AssertionError("expected EmbeddingError")

    assert client.last_request_count == 2


def test_each_embedding_http_batch_uses_shared_model_call_limit(monkeypatch):
    limiter = _Semaphore()

    def fake_post(url, *, json, headers, timeout):
        return _Resp(len(json["input"]))

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr("govcheck.llm.embedding.MODEL_CALL_SEMAPHORE", limiter)
    client = EmbeddingClient(
        base_url="http://h/v1", model="embed", batch_size=2
    )

    client.embed(["a", "b", "c", "d", "e"])

    assert limiter.entries == 3


def test_embedding_context_400_recursively_splits_batch(monkeypatch):
    sizes = []

    def fake_post(url, *, json, headers, timeout):
        size = len(json["input"])
        sizes.append(size)
        return _ContextResp() if size > 1 else _Resp(size)

    monkeypatch.setattr(requests, "post", fake_post)
    client = EmbeddingClient(
        base_url="http://h/v1", model="embed", batch_size=4
    )

    vectors = client.embed(["a", "b", "c", "d"])

    assert len(vectors) == 4
    assert sizes == [4, 2, 1, 1, 2, 1, 1]
    assert client.last_request_count == 7
