"""RAG config 設定載入測試（p3-01 spec §4.1；AC-14/15 的環境變數與機密語意）。

涵蓋 AC：
- 無 rag: 區段時回完整預設值（enabled=False / 各硬碼預設）
- GOVCHECK_RAG_* 環境變數覆寫（含數值型別正確轉換、空字串不塌縮成 0）
- score_threshold 特殊：空字串 env → None，有效數字轉 float，無 env 則回 None
- api_key/token 機密僅從 env，不回傳於 config dict（AC-14/15 由此統一測試）
- 測試以 monkeypatch 隔離 env；_load_yaml cache 用 tmp yaml 路徑跳過真 config
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from govcheck.rag import config as rag_config_module
from govcheck.rag.config import load_rag_config

# ── AC：清除所有 GOVCHECK_RAG_ 環境變數 ─────────────────────────────────────

_RAG_ENV_KEYS = [
    "GOVCHECK_RAG_ENABLED",
    "GOVCHECK_RAG_MAPPING_PATH",
    "GOVCHECK_RAG_BATCH_SIZE",
    "GOVCHECK_RAG_MAX_SECTIONS_PER_ITEM",
    "GOVCHECK_RAG_MAX_EXCERPT_CHARS",
    "GOVCHECK_RAG_TIMEOUT",
    "GOVCHECK_RAG_MAX_ITEMS",
    "GOVCHECK_RAG_EMBEDDING_BASE_URL",
    "GOVCHECK_RAG_EMBEDDING_MODEL",
    "GOVCHECK_RAG_EMBEDDING_DIM",
    "GOVCHECK_RAG_MILVUS_URI",
    "GOVCHECK_RAG_TOP_K",
    "GOVCHECK_RAG_SCORE_THRESHOLD",
    "GOVCHECK_RAG_EMBEDDING_API_KEY",
    "GOVCHECK_RAG_MILVUS_TOKEN",
]


@pytest.fixture(autouse=True)
def _clear_rag_env(monkeypatch):
    """每個測試前清除所有 GOVCHECK_RAG_ 環境變數並清除 lru_cache。"""
    for k in _RAG_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    rag_config_module._load_yaml.cache_clear()
    yield
    rag_config_module._load_yaml.cache_clear()


@pytest.fixture()
def empty_yaml(tmp_path) -> Path:
    """返回一個不含 rag: 區段的 yaml 路徑（測試純硬碼預設值）。"""
    p = tmp_path / "cfg_no_rag.yaml"
    p.write_text("llm:\n  enabled: false\n", encoding="utf-8")
    return p


@pytest.fixture()
def rag_yaml(tmp_path) -> Path:
    """返回含 rag: 區段（但部分設定）的 yaml 路徑。"""
    p = tmp_path / "cfg_with_rag.yaml"
    data = {
        "rag": {
            "enabled": True,
            "batch_size": 16,
            "timeout": 60,
            "embedding_model": "text-embedding-3-small",
            "milvus_uri": "/some/path/test.db",
            "top_k": 8,
            "score_threshold": 0.75,
        }
    }
    p.write_text(yaml.dump(data), encoding="utf-8")
    return p


# ── 預設值測試 ────────────────────────────────────────────────────────────────


def test_defaults_no_rag_section(empty_yaml):
    """缺 rag: 區段時回完整硬碼預設值（AC-14）。"""
    cfg = load_rag_config(path=str(empty_yaml))

    assert cfg["enabled"] is False
    assert cfg["mapping_path"] == "data/rag/retrieval_map.json"
    assert cfg["batch_size"] == 2
    assert cfg["max_sections_per_item"] == 3
    assert cfg["max_excerpt_chars"] == 300
    assert cfg["timeout"] == 120.0
    assert cfg["max_items"] == 30
    assert cfg["embedding_model"] == "bge-m3"
    assert cfg["embedding_dim"] == 1024
    assert cfg["milvus_uri"] == "data/milvus/governance.db"
    assert cfg["top_k"] == 4
    assert cfg["score_threshold"] is None


def test_defaults_nonexistent_yaml(tmp_path):
    """yaml 檔不存在時一律回硬碼預設值（AC-14）。"""
    nonexistent = str(tmp_path / "missing.yaml")
    cfg = load_rag_config(path=nonexistent)

    assert cfg["enabled"] is False
    assert cfg["batch_size"] == 2
    assert cfg["timeout"] == 120.0
    assert cfg["embedding_model"] == "bge-m3"
    assert cfg["score_threshold"] is None


def test_yaml_values_loaded(rag_yaml):
    """rag: 區段存在時正確讀取 yaml 值（AC-14）。"""
    cfg = load_rag_config(path=str(rag_yaml))

    assert cfg["enabled"] is True
    assert cfg["batch_size"] == 16
    assert cfg["timeout"] == 60.0
    assert cfg["embedding_model"] == "text-embedding-3-small"
    assert cfg["milvus_uri"] == "/some/path/test.db"
    assert cfg["top_k"] == 8
    assert cfg["score_threshold"] == pytest.approx(0.75)


# ── 環境變數覆寫測試 ──────────────────────────────────────────────────────────


def test_env_enabled_true(empty_yaml, monkeypatch):
    """GOVCHECK_RAG_ENABLED=1 覆寫為 True（AC-14）。"""
    monkeypatch.setenv("GOVCHECK_RAG_ENABLED", "1")
    assert load_rag_config(path=str(empty_yaml))["enabled"] is True


def test_env_enabled_false_strings(empty_yaml, monkeypatch):
    """GOVCHECK_RAG_ENABLED=0/false/no 回 False（AC-14）。"""
    for val in ("0", "false", "no"):
        monkeypatch.setenv("GOVCHECK_RAG_ENABLED", val)
        assert load_rag_config(path=str(empty_yaml))["enabled"] is False, f"failed for {val!r}"


def test_env_batch_size_int(empty_yaml, monkeypatch):
    """GOVCHECK_RAG_BATCH_SIZE=8 正確轉 int（AC-14）。"""
    monkeypatch.setenv("GOVCHECK_RAG_BATCH_SIZE", "8")
    cfg = load_rag_config(path=str(empty_yaml))
    assert cfg["batch_size"] == 8
    assert isinstance(cfg["batch_size"], int)


def test_env_embedding_dim_int(empty_yaml, monkeypatch):
    """GOVCHECK_RAG_EMBEDDING_DIM=512 正確轉 int（AC-14）。"""
    monkeypatch.setenv("GOVCHECK_RAG_EMBEDDING_DIM", "512")
    cfg = load_rag_config(path=str(empty_yaml))
    assert cfg["embedding_dim"] == 512
    assert isinstance(cfg["embedding_dim"], int)


def test_env_timeout_float(empty_yaml, monkeypatch):
    """GOVCHECK_RAG_TIMEOUT=30 正確轉 float（AC-14）。"""
    monkeypatch.setenv("GOVCHECK_RAG_TIMEOUT", "30")
    cfg = load_rag_config(path=str(empty_yaml))
    assert cfg["timeout"] == 30.0
    assert isinstance(cfg["timeout"], float)


def test_env_top_k_int(empty_yaml, monkeypatch):
    """GOVCHECK_RAG_TOP_K=10 正確轉 int（AC-14）。"""
    monkeypatch.setenv("GOVCHECK_RAG_TOP_K", "10")
    assert load_rag_config(path=str(empty_yaml))["top_k"] == 10


def test_env_score_threshold_float(empty_yaml, monkeypatch):
    """GOVCHECK_RAG_SCORE_THRESHOLD=0.85 正確轉 float（AC-14）。"""
    monkeypatch.setenv("GOVCHECK_RAG_SCORE_THRESHOLD", "0.85")
    cfg = load_rag_config(path=str(empty_yaml))
    assert cfg["score_threshold"] == pytest.approx(0.85)


def test_env_score_threshold_empty_string_returns_none(empty_yaml, monkeypatch):
    """GOVCHECK_RAG_SCORE_THRESHOLD='' → None，不塌縮成 0（AC-14）。"""
    monkeypatch.setenv("GOVCHECK_RAG_SCORE_THRESHOLD", "")
    cfg = load_rag_config(path=str(empty_yaml))
    assert cfg["score_threshold"] is None


def test_env_embedding_base_url_trailing_slash_stripped(empty_yaml, monkeypatch):
    """embedding_base_url 尾部斜線應被去除（AC-14）。"""
    monkeypatch.setenv("GOVCHECK_RAG_EMBEDDING_BASE_URL", "http://internal:8000/v1/")
    cfg = load_rag_config(path=str(empty_yaml))
    assert cfg["embedding_base_url"] == "http://internal:8000/v1"


def test_env_mapping_path(empty_yaml, monkeypatch):
    """GOVCHECK_RAG_MAPPING_PATH 覆寫路徑（AC-14）。"""
    monkeypatch.setenv("GOVCHECK_RAG_MAPPING_PATH", "/custom/path/map.json")
    cfg = load_rag_config(path=str(empty_yaml))
    assert cfg["mapping_path"] == "/custom/path/map.json"


def test_env_milvus_uri(empty_yaml, monkeypatch):
    """GOVCHECK_RAG_MILVUS_URI 覆寫 URI（AC-14）。"""
    monkeypatch.setenv("GOVCHECK_RAG_MILVUS_URI", "/prod/milvus/gov.db")
    cfg = load_rag_config(path=str(empty_yaml))
    assert cfg["milvus_uri"] == "/prod/milvus/gov.db"


# ── 無效數值不塌縮成 0 ────────────────────────────────────────────────────────


def test_invalid_timeout_env_does_not_collapse(empty_yaml, monkeypatch):
    """GOVCHECK_RAG_TIMEOUT='' → 退回預設 120，不塌縮成 0（AC-14）。"""
    monkeypatch.setenv("GOVCHECK_RAG_TIMEOUT", "")
    assert load_rag_config(path=str(empty_yaml))["timeout"] == 120.0


def test_invalid_timeout_nonnumber(empty_yaml, monkeypatch):
    """GOVCHECK_RAG_TIMEOUT=abc → 退回預設 120（AC-14）。"""
    monkeypatch.setenv("GOVCHECK_RAG_TIMEOUT", "abc")
    assert load_rag_config(path=str(empty_yaml))["timeout"] == 120.0


def test_invalid_batch_size_empty(empty_yaml, monkeypatch):
    """GOVCHECK_RAG_BATCH_SIZE='' → 退回預設 2（AC-14）。"""
    monkeypatch.setenv("GOVCHECK_RAG_BATCH_SIZE", "")
    assert load_rag_config(path=str(empty_yaml))["batch_size"] == 2


def test_invalid_top_k_empty(empty_yaml, monkeypatch):
    """GOVCHECK_RAG_TOP_K='' → 退回預設 4（AC-14）。"""
    monkeypatch.setenv("GOVCHECK_RAG_TOP_K", "")
    assert load_rag_config(path=str(empty_yaml))["top_k"] == 4


# ── 機密只走 env，不回傳於 dict（AC-14/AC-15） ───────────────────────────────


def test_api_key_not_in_config_dict(empty_yaml, monkeypatch):
    """EMBEDDING_API_KEY / MILVUS_TOKEN 不回傳於 load_rag_config dict（AC-14/AC-15）。"""
    monkeypatch.setenv("GOVCHECK_RAG_EMBEDDING_API_KEY", "sk-secret")
    monkeypatch.setenv("GOVCHECK_RAG_MILVUS_TOKEN", "tok-secret")
    cfg = load_rag_config(path=str(empty_yaml))
    # 機密鍵絕不出現在 config dict 中
    assert "embedding_api_key" not in cfg
    assert "milvus_token" not in cfg
    assert "api_key" not in cfg
    assert "token" not in cfg


def test_no_secret_keys_in_config(empty_yaml):
    """config dict 鍵集不含任何機密相關鍵（AC-14/AC-15）。"""
    cfg = load_rag_config(path=str(empty_yaml))
    secret_patterns = ("api_key", "token", "password", "secret", "key")
    for key in cfg.keys():
        for pat in secret_patterns:
            assert pat not in key.lower(), (
                f"config dict 不應含機密鍵 {key!r}（AC-14）"
            )


# ── env 覆寫優先於 yaml ───────────────────────────────────────────────────────


def test_env_overrides_yaml_value(rag_yaml, monkeypatch):
    """env 覆寫優先於 yaml 設定（AC-14）。"""
    # rag_yaml 設 batch_size=16；env 設 32
    monkeypatch.setenv("GOVCHECK_RAG_BATCH_SIZE", "32")
    cfg = load_rag_config(path=str(rag_yaml))
    assert cfg["batch_size"] == 32


def test_yaml_score_threshold_when_no_env(rag_yaml):
    """無 env 時以 yaml 的 score_threshold 值（AC-14）。"""
    cfg = load_rag_config(path=str(rag_yaml))
    assert cfg["score_threshold"] == pytest.approx(0.75)
