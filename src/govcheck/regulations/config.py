"""法規對照表設定：YAML `regulations:` 區段 + 環境變數覆寫。

對照表是凍結資產，runtime 只需要「檔案在哪」與「每項餵給模型多少條文」四個鍵。
建置期參數（embedding 端點、Milvus URI、top_k、score_threshold）已隨建置管線一併封存，
不再出現在 runtime 設定。
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "llm_config.yaml"

DEFAULT_MAPPING_PATH = "data/rag/retrieval_map.json"


@functools.lru_cache(maxsize=1)
def _load_yaml(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _as_int(env_val: str | None, default, fallback: int) -> int:
    """環境變數優先、YAML 預設次之；兩者皆無效（空字串/非數字）才用 fallback。

    刻意不讓無效輸入塌縮成 0：例如 `GOVCHECK_REGULATIONS_MAX_ITEMS=''` 應退回預設值，
    而非 0（0 會讓所有檢核項都不送審，靜默失去法規比對）。
    """
    for candidate in (env_val, default):
        if candidate is None or candidate == "":
            continue
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return fallback


def load_regulations_config(path: str | None = None) -> dict:
    """回傳正規化後的法規對照表設定。

    環境變數覆寫鍵（`GOVCHECK_REGULATIONS_` 前綴）：
        MAPPING_PATH / MAX_SECTIONS_PER_ITEM / MAX_EXCERPT_CHARS / MAX_ITEMS

    函式不快取（env 可動態切換）；只有 `_load_yaml` 快取檔案讀取。
    """
    raw = _load_yaml(str(Path(path) if path else CONFIG_PATH)).get("regulations") or {}
    return {
        "mapping_path": (
            os.environ.get("GOVCHECK_REGULATIONS_MAPPING_PATH")
            or raw.get("mapping_path")
            or DEFAULT_MAPPING_PATH
        ),
        # 每個檢核項最多餵幾段條文摘錄給模型（prompt 預算）
        "max_sections_per_item": _as_int(
            os.environ.get("GOVCHECK_REGULATIONS_MAX_SECTIONS_PER_ITEM"),
            raw.get("max_sections_per_item"),
            3,
        ),
        # 每段摘錄的字數上限
        "max_excerpt_chars": _as_int(
            os.environ.get("GOVCHECK_REGULATIONS_MAX_EXCERPT_CHARS"),
            raw.get("max_excerpt_chars"),
            300,
        ),
        # F02 觸發題查表的引用上限（安全閥，避免題目全觸發時報告被引用洗版）
        "max_items": _as_int(
            os.environ.get("GOVCHECK_REGULATIONS_MAX_ITEMS"),
            raw.get("max_items"),
            30,
        ),
    }
