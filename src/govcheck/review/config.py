"""送件包審查設定載入器（缺件 / F01 必填 / 跨表映射）。

刻意與自動產生的 f02_scoring.yaml 分開：review_config.yaml 為手動維護，
不會被 scripts/extract_f02_scoring.py 覆蓋。仿 scoring.f02_score.load_config 的單例快取。
"""

from __future__ import annotations

import functools
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "review_config.yaml"


@functools.lru_cache(maxsize=1)
def load_review_config(path: str | None = None) -> dict:
    p = Path(path) if path else CONFIG_PATH
    with p.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)
