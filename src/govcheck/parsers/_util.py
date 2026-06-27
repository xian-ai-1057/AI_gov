"""parser 共用小工具。"""

from __future__ import annotations


def clean(v) -> str | None:
    """儲存格值 → 去前後空白的字串；空值（None / 空字串）回 None。"""
    if v is None:
        return None
    s = str(v).strip()
    return s or None
