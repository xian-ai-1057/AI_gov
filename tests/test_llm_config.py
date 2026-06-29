"""LLM 設定載入：環境變數覆寫、無效值退回預設、api_key 僅走環境變數。"""

from __future__ import annotations

import pytest

from govcheck.llm.config import load_llm_config

_ENV_KEYS = [
    "GOVCHECK_LLM_ENABLED", "GOVCHECK_LLM_BASE_URL", "GOVCHECK_LLM_MODEL",
    "GOVCHECK_LLM_API_KEY", "GOVCHECK_LLM_TIMEOUT", "GOVCHECK_LLM_TEMPERATURE",
    "GOVCHECK_LLM_MAX_ITEMS",
]


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def test_defaults_from_yaml():
    cfg = load_llm_config()
    assert cfg["base_url"] == "http://localhost:11434/v1"
    assert cfg["timeout"] == 60.0
    assert cfg["temperature"] == 0.0
    assert cfg["max_items"] == 30
    assert cfg["enabled"] is False
    assert cfg["api_key"] is None


def test_invalid_timeout_env_falls_back_to_default(monkeypatch):
    # 無效值不可塌縮成 0（timeout=0 會讓 requests 變非阻塞、每次呼叫立即失敗）
    monkeypatch.setenv("GOVCHECK_LLM_TIMEOUT", "")
    assert load_llm_config()["timeout"] == 60.0
    monkeypatch.setenv("GOVCHECK_LLM_TIMEOUT", "abc")
    assert load_llm_config()["timeout"] == 60.0


def test_invalid_max_items_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("GOVCHECK_LLM_MAX_ITEMS", "")
    assert load_llm_config()["max_items"] == 30
    monkeypatch.setenv("GOVCHECK_LLM_MAX_ITEMS", "not-a-number")
    assert load_llm_config()["max_items"] == 30


def test_valid_env_overrides():
    import os
    os.environ["GOVCHECK_LLM_TIMEOUT"] = "5"
    os.environ["GOVCHECK_LLM_MAX_ITEMS"] = "10"
    os.environ["GOVCHECK_LLM_ENABLED"] = "1"
    os.environ["GOVCHECK_LLM_BASE_URL"] = "http://internal:8000/v1/"  # 尾斜線應被去除
    cfg = load_llm_config()
    assert cfg["timeout"] == 5.0
    assert cfg["max_items"] == 10
    assert cfg["enabled"] is True
    assert cfg["base_url"] == "http://internal:8000/v1"


def test_api_key_only_from_env(monkeypatch):
    monkeypatch.setenv("GOVCHECK_LLM_API_KEY", "sk-secret")
    assert load_llm_config()["api_key"] == "sk-secret"
