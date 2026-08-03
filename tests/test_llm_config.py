"""LLM 設定載入：環境變數覆寫、無效值退回預設、api_key 僅走環境變數。"""

from __future__ import annotations

import pytest

from govcheck.llm.config import load_llm_config

_ENV_KEYS = [
    "GOVCHECK_LLM_ENABLED", "GOVCHECK_LLM_BASE_URL", "GOVCHECK_LLM_MODEL",
    "GOVCHECK_LLM_API_KEY", "GOVCHECK_LLM_TIMEOUT", "GOVCHECK_LLM_TEMPERATURE",
    "GOVCHECK_LLM_MAX_ITEMS", "GOVCHECK_LLM_BATCH_SIZE",
    "GOVCHECK_LLM_MAX_OUTPUT_TOKENS",
    "GOVCHECK_LLM_CONTEXT_WINDOW_TOKENS", "GOVCHECK_LLM_CONTEXT_SAFETY_MARGIN_TOKENS",
    "GOVCHECK_EVIDENCE_EMBEDDING_ENABLED", "GOVCHECK_EVIDENCE_LEXICAL_WEIGHT",
    "GOVCHECK_EVIDENCE_TOP_K", "GOVCHECK_EVIDENCE_EXCERPT_CHARS",
    "GOVCHECK_EMBEDDING_BATCH_SIZE", "GOVCHECK_EMBEDDING_MAX_INPUT_CHARS",
    "GOVCHECK_EMBEDDING_MAX_CHUNKS",
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
    assert cfg["max_output_tokens"] == 4096
    assert cfg["context_window_tokens"] == 30_000
    assert cfg["context_safety_margin_tokens"] == 1_024
    assert cfg["evidence_embedding_enabled"] is False
    assert cfg["evidence_lexical_weight"] == 0.7
    assert cfg["evidence_top_k"] == 4
    assert cfg["evidence_excerpt_chars"] == 600
    assert cfg["embedding_batch_size"] == 16
    assert cfg["embedding_max_input_chars"] == 2000
    assert cfg["embedding_max_chunks"] == 512
    assert cfg["batch_size"] == 8
    assert cfg["enabled"] is False  # Prompt provider 與核准模型就緒前預設關閉
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


def test_batch_size_env_override_and_fallback(monkeypatch):
    monkeypatch.setenv("GOVCHECK_LLM_BATCH_SIZE", "4")
    assert load_llm_config()["batch_size"] == 4
    monkeypatch.setenv("GOVCHECK_LLM_BATCH_SIZE", "")          # 無效 → 退回預設
    assert load_llm_config()["batch_size"] == 8
    monkeypatch.setenv("GOVCHECK_LLM_BATCH_SIZE", "not-a-number")
    assert load_llm_config()["batch_size"] == 8


def test_valid_env_overrides():
    import os
    os.environ["GOVCHECK_LLM_TIMEOUT"] = "5"
    os.environ["GOVCHECK_LLM_MAX_ITEMS"] = "10"
    os.environ["GOVCHECK_LLM_ENABLED"] = "1"
    os.environ["GOVCHECK_LLM_CONTEXT_WINDOW_TOKENS"] = "30000"
    os.environ["GOVCHECK_LLM_CONTEXT_SAFETY_MARGIN_TOKENS"] = "1500"
    os.environ["GOVCHECK_EVIDENCE_EXCERPT_CHARS"] = "700"
    os.environ["GOVCHECK_EMBEDDING_BATCH_SIZE"] = "8"
    os.environ["GOVCHECK_EMBEDDING_MAX_INPUT_CHARS"] = "1500"
    os.environ["GOVCHECK_EMBEDDING_MAX_CHUNKS"] = "128"
    os.environ["GOVCHECK_LLM_BASE_URL"] = "http://internal:8000/v1/"  # 尾斜線應被去除
    cfg = load_llm_config()
    assert cfg["timeout"] == 5.0
    assert cfg["max_items"] == 10
    assert cfg["enabled"] is True
    assert cfg["context_window_tokens"] == 30_000
    assert cfg["context_safety_margin_tokens"] == 1_500
    assert cfg["evidence_excerpt_chars"] == 700
    assert cfg["embedding_batch_size"] == 8
    assert cfg["embedding_max_input_chars"] == 1500
    assert cfg["embedding_max_chunks"] == 128
    assert cfg["base_url"] == "http://internal:8000/v1"


def test_api_key_only_from_env(monkeypatch):
    monkeypatch.setenv("GOVCHECK_LLM_API_KEY", "sk-secret")
    assert load_llm_config()["api_key"] == "sk-secret"
