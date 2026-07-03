"""LLM 客戶端測試：全程 mock requests.post，不打真端點。"""

from __future__ import annotations

import pytest
import requests

from govcheck.llm.client import ChatClient, LLMError, parse_json_object


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
