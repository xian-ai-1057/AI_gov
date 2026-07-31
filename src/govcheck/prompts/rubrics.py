"""審查員檢核句（rubric）判準表的載入。

**判準是人工撰寫的，不是模型生成的。** 模型只回答「這段說明有沒有回應這一句」，
與法規條號一律由對照表決定性推導、模型只作交集過濾是同一個原則：
讓模型自己造判準等於讓它出題自己改，覆核者無從核對，AI 初判也就不可能被三遵採信。

判準衍生自治理辦法，與其他 prompt 一樣不入庫（見 CLAUDE.md）。
缺檔／格式錯誤一律降級成「沒有 rubric」——報告頁會少一個區塊，其餘判讀完全不受影響。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from govcheck.logging_setup import get_logger

log = get_logger("rubrics")

SCHEMA_VERSION = 1
_FILENAME = "govcheck.f03.rubrics.yaml"
# 單一檢核項的檢核句上限：句子越多，模型逐句對位越容易錯開，且展開列會長到沒人看。
_MAX_LINES = 8
_ITEM_ID_RE = re.compile(r"^\d+-\d+$")


def rubric_path() -> Path:
    """判準表位置。沿用 prompt 目錄（GOVCHECK_PROMPT_DIR），兩者一起佈署、一起缺。"""
    directory = os.environ.get("GOVCHECK_PROMPT_DIR") or "prompts"
    return Path(directory) / _FILENAME


def load_rubrics(path: str | Path | None = None) -> dict[str, list[str]]:
    """載入 {item_id: [檢核句, …]}。任何問題一律回 {}，**絕不 raise**。

    沿用 regulations/mapping 的降級慣例：判準表是本機資產，新環境常常沒有；
    缺檔不該讓整份審查失敗，但也不該讓報告假裝逐句檢核過了。
    """
    target = Path(path) if path is not None else rubric_path()
    if not target.exists():
        log.warning("rubric map absent path=%s", target.name)
        return {}
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        log.warning("rubric map unreadable err=%s", type(exc).__name__)
        return {}

    if not isinstance(raw, dict):
        log.warning("rubric map malformed: root is not a mapping")
        return {}
    version = raw.get("schema_version")
    # 版本不符時整份丟棄：欄位語意可能已改，半懂半猜地讀進來比沒有更危險。
    if str(version) != str(SCHEMA_VERSION):
        log.warning("rubric map schema mismatch got=%s want=%s", version, SCHEMA_VERSION)
        return {}

    items = raw.get("items")
    if not isinstance(items, dict):
        log.warning("rubric map malformed: items is not a mapping")
        return {}

    rubrics: dict[str, list[str]] = {}
    for item_id, entry in items.items():
        if not isinstance(item_id, str) or not _ITEM_ID_RE.fullmatch(item_id):
            log.warning("rubric map malformed: invalid item id")
            return {}
        if not isinstance(entry, dict):
            log.warning("rubric map malformed: item entry is not a mapping")
            return {}
        lines = entry.get("lines")
        if not isinstance(lines, list) or not lines:
            log.warning("rubric map malformed: lines is not a non-empty list")
            return {}
        if any(not isinstance(line, str) or not line.strip() for line in lines):
            log.warning("rubric map malformed: every line must be a non-empty string")
            return {}
        rubrics[item_id] = [line.strip() for line in lines[:_MAX_LINES]]
    log.debug("rubric map loaded items=%d", len(rubrics))
    return rubrics
