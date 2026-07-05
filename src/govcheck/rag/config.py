"""RAG 端點設定載入：YAML rag: 區段 + 環境變數覆寫（機密只走環境變數）。

仿 govcheck.llm.config.load_llm_config，讀取同一份 llm_config.yaml 的 rag: 區段。
rag: 區段不存在時一律回傳硬碼預設值。機密（EMBEDDING_API_KEY / MILVUS_TOKEN）
一律由環境變數提供，不回傳於 dict 中。
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

import yaml

from govcheck.logging_setup import get_logger  # noqa: F401 — import pattern only

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "llm_config.yaml"

_TRUE = {"1", "true", "yes", "on", "y", "t"}


@functools.lru_cache(maxsize=1)
def _load_yaml(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _as_bool(env_val: str | None, default) -> bool:
    if env_val is not None:
        return env_val.strip().lower() in _TRUE
    return bool(default)


def _as_number(env_val: str | None, default, cast, fallback):
    """環境變數優先、YAML 預設次之；兩者皆無效（空字串/非數字）才用 fallback。

    刻意不讓無效輸入塌縮成 0：例如 GOVCHECK_RAG_TIMEOUT='' 應退回預設 120，而非 0
    （timeout=0 會讓 requests 變非阻塞，使每次呼叫立即失敗）。
    """
    for candidate in (env_val, default):
        if candidate is None or candidate == "":
            continue
        try:
            return cast(candidate)
        except (TypeError, ValueError):
            continue
    return fallback


def load_rag_config(path: str | None = None) -> dict:
    """回傳正規化後的 RAG 設定 dict。

    環境變數覆寫鍵（GOVCHECK_RAG_ 前綴）：
        ENABLED / MAPPING_PATH / BATCH_SIZE / MAX_SECTIONS_PER_ITEM /
        MAX_EXCERPT_CHARS / TIMEOUT / MAX_ITEMS /
        EMBEDDING_BASE_URL / EMBEDDING_MODEL / EMBEDDING_DIM /
        MILVUS_URI / TOP_K / SCORE_THRESHOLD

    機密（EMBEDDING_API_KEY / MILVUS_TOKEN）一律由環境變數提供，不回傳於 dict 中。
    函式不快取（env vars 可動態切換）；只有 _load_yaml 快取檔案讀取。
    """
    raw_all = _load_yaml(str(Path(path) if path else CONFIG_PATH))
    raw = raw_all.get("rag") or {}

    # ── 字串欄位：env or yaml or 硬碼預設；空字串視為未設 ──────────────────────
    mapping_path: str = (
        os.environ.get("GOVCHECK_RAG_MAPPING_PATH")
        or raw.get("mapping_path")
        or "data/rag/retrieval_map.json"
    )
    embedding_base_url: str = (
        os.environ.get("GOVCHECK_RAG_EMBEDDING_BASE_URL")
        or raw.get("embedding_base_url")
        or "http://localhost:11434/v1"
    )
    embedding_model: str = (
        os.environ.get("GOVCHECK_RAG_EMBEDDING_MODEL")
        or raw.get("embedding_model")
        or "bge-m3"
    )
    milvus_uri: str = (
        os.environ.get("GOVCHECK_RAG_MILVUS_URI")
        or raw.get("milvus_uri")
        or "data/milvus/governance.db"
    )

    # ── score_threshold：float | None；空字串 env → None ───────────────────────
    _st_env = os.environ.get("GOVCHECK_RAG_SCORE_THRESHOLD")
    if _st_env is not None:
        if _st_env.strip() == "":
            score_threshold: float | None = None
        else:
            try:
                score_threshold = float(_st_env)
            except (TypeError, ValueError):
                _raw_st = raw.get("score_threshold")
                score_threshold = float(_raw_st) if _raw_st is not None else None
    else:
        _raw_st = raw.get("score_threshold")
        score_threshold = float(_raw_st) if _raw_st is not None else None

    return {
        # ── runtime ───────────────────────────────────────────────────────────
        "enabled": _as_bool(os.environ.get("GOVCHECK_RAG_ENABLED"), raw.get("enabled", False)),
        "mapping_path": mapping_path,
        "batch_size": _as_number(
            os.environ.get("GOVCHECK_RAG_BATCH_SIZE"), raw.get("batch_size"), int, 2
        ),
        "max_sections_per_item": _as_number(
            os.environ.get("GOVCHECK_RAG_MAX_SECTIONS_PER_ITEM"),
            raw.get("max_sections_per_item"),
            int,
            3,
        ),
        "max_excerpt_chars": _as_number(
            os.environ.get("GOVCHECK_RAG_MAX_EXCERPT_CHARS"),
            raw.get("max_excerpt_chars"),
            int,
            300,
        ),
        "timeout": _as_number(
            os.environ.get("GOVCHECK_RAG_TIMEOUT"), raw.get("timeout"), float, 120.0
        ),
        "max_items": _as_number(
            os.environ.get("GOVCHECK_RAG_MAX_ITEMS"), raw.get("max_items"), int, 30
        ),
        # ── build-time / embedding ────────────────────────────────────────────
        "embedding_base_url": embedding_base_url.rstrip("/"),
        "embedding_model": embedding_model,
        "embedding_dim": _as_number(
            os.environ.get("GOVCHECK_RAG_EMBEDDING_DIM"), raw.get("embedding_dim"), int, 1024
        ),
        "milvus_uri": milvus_uri,
        "top_k": _as_number(
            os.environ.get("GOVCHECK_RAG_TOP_K"), raw.get("top_k"), int, 4
        ),
        "score_threshold": score_threshold,
        # 機密（EMBEDDING_API_KEY / MILVUS_TOKEN）刻意不包含在此 dict 中
    }
