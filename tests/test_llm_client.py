"""LLM 客戶端測試：全程 mock requests.post，不打真端點。"""

from __future__ import annotations

from pathlib import Path

import pytest
import requests

from govcheck.llm.client import (
    ChatClient,
    LLMContextLengthError,
    LLMError,
    LLMOutputTruncatedError,
    estimate_chat_input_tokens,
    parse_json_object,
)
from govcheck.logging_setup import load_log_config, set_request_id, setup_logging


class _Resp:
    def __init__(self, status: int, payload: dict | None = None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def _ok(content: str) -> _Resp:
    return _Resp(200, {"choices": [{"message": {"content": content}}]})


def _ops_log_text() -> str:
    p = Path(load_log_config()["dir"]) / "govcheck.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _llm_raw_files() -> list[Path]:
    d = Path(load_log_config()["dir"]) / "llm_raw"
    return sorted(d.glob("*.json")) if d.exists() else []


def test_chat_posts_openai_compatible_payload(monkeypatch):
    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return _ok('{"ok":true}')

    monkeypatch.setattr(requests, "post", fake_post)
    c = ChatClient(base_url="http://host:11434/v1", model="m1", api_key="secret", timeout=30, temperature=0)
    out = c.chat([{"role": "user", "content": "hi"}], schema={"type": "object"})

    assert out == '{"ok":true}'
    assert captured["url"] == "http://host:11434/v1/chat/completions"
    assert captured["json"]["model"] == "m1"
    assert captured["json"]["messages"][0]["content"] == "hi"
    assert captured["json"]["max_tokens"] == 4096
    assert captured["json"]["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "govcheck_result", "schema": {"type": "object"}, "strict": True},
    }
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["timeout"] == 30


def test_chat_without_schema_omits_response_format(monkeypatch):
    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(json=json)
        return _ok('{"ok":true}')

    monkeypatch.setattr(requests, "post", fake_post)
    ChatClient(base_url="http://h/v1", model="m").chat([{"role": "user", "content": "hi"}])
    assert "response_format" not in captured["json"]


def test_chat_omits_auth_header_without_key(monkeypatch):
    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(headers=headers)
        return _ok("x")

    monkeypatch.setattr(requests, "post", fake_post)
    ChatClient(base_url="http://h/v1", model="m").chat([{"role": "user", "content": "hi"}])
    assert "Authorization" not in captured["headers"]


def test_chat_http_error_raises_llmerror(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp(500, None, "boom"))
    with pytest.raises(LLMError):
        ChatClient(base_url="http://h/v1", model="m").chat([{"role": "user", "content": "x"}])


def test_chat_connection_error_raises_llmerror(monkeypatch):
    def boom(url, **kw):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(LLMError):
        ChatClient(base_url="http://h/v1", model="m").chat([{"role": "user", "content": "x"}])


def test_chat_malformed_body_raises_llmerror(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp(200, {"unexpected": 1}))
    with pytest.raises(LLMError):
        ChatClient(base_url="http://h/v1", model="m").chat([{"role": "user", "content": "x"}])


def test_from_config_builds_client():
    c = ChatClient.from_config({
        "base_url": "http://h/v1", "model": "m", "api_key": None, "timeout": 5, "temperature": 0,
    })
    assert c.endpoint == "http://h/v1/chat/completions"


def test_local_context_preflight_blocks_oversized_request_without_http(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: calls.append((args, kwargs)))
    client = ChatClient(
        base_url="http://h/v1",
        model="m",
        max_output_tokens=20,
        context_window_tokens=100,
        context_safety_margin_tokens=10,
    )

    with pytest.raises(LLMContextLengthError, match="context 預算"):
        client.chat([{"role": "user", "content": "超長內容" * 100}])

    assert calls == []


def test_endpoint_context_error_is_typed_and_does_not_open_circuit(monkeypatch):
    calls = 0

    def context_error(url, **kwargs):
        nonlocal calls
        calls += 1
        return _Resp(400, {"error": {
            "message": "This model's maximum context length is 100 tokens; reduce input tokens",
            "type": "BadRequestError",
            "param": "input_tokens",
            "code": 400,
        }})

    monkeypatch.setattr(requests, "post", context_error)
    client = ChatClient(
        base_url="http://h/v1", model="m", circuit_failures=1, max_retries=0
    )

    for _ in range(2):
        with pytest.raises(LLMContextLengthError):
            client.chat([{"role": "user", "content": "small"}])

    assert calls == 2


def test_input_estimate_counts_multibyte_text_and_schema():
    plain = estimate_chat_input_tokens([{"role": "user", "content": "abc"}])
    chinese = estimate_chat_input_tokens([{"role": "user", "content": "治理治理治理"}])
    structured = estimate_chat_input_tokens(
        [{"role": "user", "content": "abc"}], {"type": "object"}
    )

    assert chinese > plain
    assert structured > plain


def test_finish_reason_length_raises_typed_truncation(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, **kwargs: _Resp(200, {
        "choices": [{"message": {"content": '{"partial":'}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 123, "completion_tokens": 45},
    }))

    with pytest.raises(LLMOutputTruncatedError) as raised:
        ChatClient(base_url="http://h/v1", model="m").chat([
            {"role": "user", "content": "small"}
        ])

    assert raised.value.input_tokens == 123
    assert raised.value.output_tokens == 45
    assert raised.value.latency_ms >= 0


# --- parse_json_object ---

def test_parse_plain_json():
    assert parse_json_object('{"a": 1}') == {"a": 1}


def test_parse_fenced_json():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_with_surrounding_noise():
    assert parse_json_object('好的，結果如下：{"a": 1, "b": "中文"} 以上。') == {"a": 1, "b": "中文"}


def test_parse_first_object_ignores_trailing_object():
    # 尾端若有第二段（含 }），raw_decode 只取第一個完整物件，不會吞到尾端
    assert parse_json_object('{"a": 1} 備註：{"b": 2}') == {"a": 1}


def test_parse_non_json_raises():
    with pytest.raises(LLMError):
        parse_json_object("沒有大括號的回應")


def test_parse_empty_raises():
    with pytest.raises(LLMError):
        parse_json_object("")


# --- dev-only 原文落檔 / 終端截斷（隱私守線：prod 完全不外洩） ---

def test_dev_chat_never_persists_or_logs_raw_content(monkeypatch):
    monkeypatch.setenv("GOVCHECK_LOG_PROFILE", "dev")
    setup_logging()
    set_request_id("devrid01")
    long_resp = '{"x":"' + "y" * 2000 + '"}'  # 遠超終端截斷長度
    monkeypatch.setattr(requests, "post", lambda url, **kw: _ok(long_resp))

    out = ChatClient(base_url="http://h/v1", model="m").chat([{"role": "user", "content": "hi-req"}])
    assert out == long_resp

    assert _llm_raw_files() == []
    ops = _ops_log_text()
    assert long_resp not in ops
    assert "hi-req" not in ops


def test_prod_chat_does_not_dump_or_log_body(monkeypatch):
    monkeypatch.setenv("GOVCHECK_LOG_PROFILE", "prod")
    setup_logging()
    secret = "SECRET_BODY_zzz"
    monkeypatch.setattr(requests, "post", lambda url, **kw: _ok(f'{{"x":"{secret}"}}'))

    ChatClient(base_url="http://h/v1", model="m").chat([{"role": "user", "content": "hi"}])
    assert _llm_raw_files() == []                            # prod 不寫原文檔
    ops = _ops_log_text()
    assert secret not in ops                                 # 回應內容不落 log
    assert "llm call ok" not in ops                          # DEBUG 在 prod 不落檔


def test_dev_chat_does_not_invoke_raw_dump(monkeypatch):
    monkeypatch.setenv("GOVCHECK_LOG_PROFILE", "dev")
    setup_logging()
    monkeypatch.setattr(requests, "post", lambda url, **kw: _ok('{"ok":true}'))

    ChatClient(base_url="http://h/v1", model="m").chat([{"role": "user", "content": "hi"}])
    assert _llm_raw_files() == []


def test_dev_parse_failure_does_not_log_excerpt(monkeypatch):
    monkeypatch.setenv("GOVCHECK_LOG_PROFILE", "dev")
    setup_logging()
    set_request_id("prid0001")
    # 使用者實際遇到的情境：有 { 但屬性名沒加引號 → raw_decode 分支
    with pytest.raises(LLMError):
        parse_json_object("{\n:bad}")
    ops = _ops_log_text()
    assert ":bad" not in ops


def test_dev_parse_failure_no_brace_does_not_log_body(monkeypatch):
    # 覆蓋 no-brace 分支（回應完全沒有 { → start == -1），與 raw_decode 分支不同支
    monkeypatch.setenv("GOVCHECK_LOG_PROFILE", "dev")
    setup_logging()
    set_request_id("nbrid001")
    with pytest.raises(LLMError):
        parse_json_object("這是一段完全沒有大括號的模型回覆")
    ops = _ops_log_text()
    assert "完全沒有大括號" not in ops


def test_prod_parse_failure_no_excerpt(monkeypatch):
    monkeypatch.setenv("GOVCHECK_LOG_PROFILE", "prod")
    setup_logging()
    with pytest.raises(LLMError):
        parse_json_object("{\n:bad}")
    assert "llm json parse failed" not in _ops_log_text()
