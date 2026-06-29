"""LLM 端點設定載入：YAML 非機密預設 + 環境變數覆寫（機密只走環境變數）。

仿 review.config.load_review_config，但因 api_key 等需由環境變數提供，
故只快取「檔案讀取」、每次呼叫重新套用環境變數覆寫（測試可動態切換 env）。
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

import yaml

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

    刻意不讓無效輸入塌縮成 0：例如 GOVCHECK_LLM_TIMEOUT='' 應退回預設 60，而非 0
    （timeout=0 會讓 requests 變非阻塞，使每次 LLM 呼叫立即失敗）。
    """
    for candidate in (env_val, default):
        if candidate is None or candidate == "":
            continue
        try:
            return cast(candidate)
        except (TypeError, ValueError):
            continue
    return fallback


def load_llm_config(path: str | None = None) -> dict:
    """回傳正規化後的 LLM 設定 dict。

    環境變數覆寫鍵：GOVCHECK_LLM_ENABLED / _BASE_URL / _MODEL / _API_KEY /
    _TIMEOUT / _TEMPERATURE / _MAX_ITEMS。api_key 一律由環境變數提供。
    """
    raw = _load_yaml(str(Path(path) if path else CONFIG_PATH))
    base_url = os.environ.get("GOVCHECK_LLM_BASE_URL", raw.get("base_url") or "http://localhost:11434/v1")
    return {
        "enabled": _as_bool(os.environ.get("GOVCHECK_LLM_ENABLED"), raw.get("enabled", False)),
        "base_url": base_url.rstrip("/"),
        "model": os.environ.get("GOVCHECK_LLM_MODEL", raw.get("model")),
        # api_key 只從環境變數取得；刻意不從 YAML 退回，避免金鑰誤寫入設定檔被一起入庫
        "api_key": os.environ.get("GOVCHECK_LLM_API_KEY"),
        "timeout": _as_number(os.environ.get("GOVCHECK_LLM_TIMEOUT"), raw.get("timeout"), float, 60.0),
        "temperature": _as_number(os.environ.get("GOVCHECK_LLM_TEMPERATURE"), raw.get("temperature"), float, 0.0),
        "max_items": _as_number(os.environ.get("GOVCHECK_LLM_MAX_ITEMS"), raw.get("max_items"), int, 30),
    }
