"""LLM 客戶端測試：全程 mock requests.post，不打真端點。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from govcheck.llm.client import ChatClient, LLMError, parse_json_object
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


# --- dev-only 原文落檔 / 終端截斷（隱私守線：prod 完全不外洩） ---

def test_dev_chat_dumps_full_response_and_truncates_terminal(monkeypatch):
    monkeypatch.setenv("GOVCHECK_LOG_PROFILE", "dev")
    setup_logging()
    set_request_id("devrid01")
    long_resp = '{"x":"' + "y" * 2000 + '"}'  # 遠超終端截斷長度
    monkeypatch.setattr(requests, "post", lambda url, **kw: _ok(long_resp))

    out = ChatClient(base_url="http://h/v1", model="m").chat([{"role": "user", "content": "hi-req"}])
    assert out == long_resp

    files = _llm_raw_files()
    assert len(files) == 1
    rec = json.loads(files[0].read_text(encoding="utf-8"))
    assert rec["response_text"] == long_resp                 # 檔案存「完整」回應
    assert rec["request_messages"][0]["content"] == "hi-req"  # 也存完整 request
    assert rec["request_id"] == "devrid01"

    ops = _ops_log_text()
    assert "llm call ok" in ops and "devrid01" in ops
    assert "…" in ops                                        # 終端只印截斷版
    assert long_resp not in ops                              # 完整回應不落 ops log


def test_prod_chat_does_not_dump_or_log_body(monkeypatch):
    monkeypatch.setenv("GOVCHECK_LOG_PROFILE", "prod")
    setup_logging()
    # spy 直接釘住 client.py 的 dev_mode() 閘門：若閘門被刪，prod 下 dump_llm_raw 會被呼叫 → 測試失敗
    # （不倚賴 dump_llm_raw 內層 backstop 或 DEBUG 過濾這兩道間接防線）。
    calls: list = []
    monkeypatch.setattr("govcheck.llm.client.dump_llm_raw", lambda rec: calls.append(rec))
    secret = "SECRET_BODY_zzz"
    monkeypatch.setattr(requests, "post", lambda url, **kw: _ok(f'{{"x":"{secret}"}}'))

    ChatClient(base_url="http://h/v1", model="m").chat([{"role": "user", "content": "hi"}])
    assert calls == []                                       # prod：client.py 閘門擋住落檔呼叫
    assert _llm_raw_files() == []                            # prod 不寫原文檔
    ops = _ops_log_text()
    assert secret not in ops                                 # 回應內容不落 log
    assert "llm call ok" not in ops                          # DEBUG 在 prod 不落檔


def test_dev_chat_actually_invokes_dump_guard(monkeypatch):
    # 對照組：dev 下 client.py 閘門必須真的呼叫 dump_llm_raw（否則落檔功能等於沒接上）。
    monkeypatch.setenv("GOVCHECK_LOG_PROFILE", "dev")
    setup_logging()
    calls: list = []
    monkeypatch.setattr("govcheck.llm.client.dump_llm_raw", lambda rec: calls.append(rec))
    monkeypatch.setattr(requests, "post", lambda url, **kw: _ok('{"ok":true}'))

    ChatClient(base_url="http://h/v1", model="m").chat([{"role": "user", "content": "hi"}])
    assert len(calls) == 1 and calls[0]["status"] == "ok"    # dev：閘門有觸發落檔


def test_dev_parse_failure_logs_excerpt(monkeypatch):
    monkeypatch.setenv("GOVCHECK_LOG_PROFILE", "dev")
    setup_logging()
    set_request_id("prid0001")
    # 使用者實際遇到的情境：有 { 但屬性名沒加引號 → raw_decode 分支
    with pytest.raises(LLMError):
        parse_json_object("{\n:bad}")
    ops = _ops_log_text()
    assert "llm json parse failed" in ops and "prid0001" in ops


def test_dev_parse_failure_no_brace_logs(monkeypatch):
    # 覆蓋 no-brace 分支（回應完全沒有 { → start == -1），與 raw_decode 分支不同支
    monkeypatch.setenv("GOVCHECK_LOG_PROFILE", "dev")
    setup_logging()
    set_request_id("nbrid001")
    with pytest.raises(LLMError):
        parse_json_object("這是一段完全沒有大括號的模型回覆")
    ops = _ops_log_text()
    assert "llm json no-brace" in ops and "nbrid001" in ops


def test_prod_parse_failure_no_excerpt(monkeypatch):
    monkeypatch.setenv("GOVCHECK_LOG_PROFILE", "prod")
    setup_logging()
    with pytest.raises(LLMError):
        parse_json_object("{\n:bad}")
    assert "llm json parse failed" not in _ops_log_text()
