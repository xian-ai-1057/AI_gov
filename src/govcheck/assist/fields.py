"""表單欄位目錄（提案者協填用）。

欄位的標籤／說明文字來自官方範本，屬**範本用語**，與 `data/rag/retrieval_map.json` 同一政策：
由 `scripts/extract_form_fields.py` 從 `data/original/` 產出到 `data/rag/form_fields.json`（gitignored），
runtime 只讀。缺檔／壞檔／版本不符一律降級成 None（呼叫端回 `ASSIST.FIELDS_UNAVAILABLE`），
比照 `prompts/rubrics.load_rubrics`：本機資產缺了不該讓整個服務失敗，但也不該假裝有欄位可填。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from govcheck.logging_setup import get_logger
from govcheck.paths import resolve_data_path

log = get_logger("assist_fields")

SCHEMA_VERSION = 1
FORMS: tuple[str, ...] = ("f01", "f02", "f03")
DEFAULT_PATH = "data/rag/form_fields.json"
ENV_PATH = "GOVCHECK_FORM_FIELDS_PATH"


class FieldSpec(BaseModel):
    """一個可協填的表單欄位；`cell` 是官方 Excel 上對應的儲存格，供使用者對照抄寫。"""

    model_config = ConfigDict(extra="forbid")

    form: Literal["f01", "f02", "f03"]
    field_id: str
    label: str
    description: str = ""
    cell: str
    kind: Literal["text", "yes_no"] = "text"
    # F02 題組：同組成員共用 group；single_choice 群組最終只能留一個 Y（engine 強制）。
    group: str | None = None
    group_kind: Literal["single_choice", "multi_choice"] | None = None
    required: bool = True
    # 檢索用補充關鍵詞（腳本內手寫小字典），與 label/description 併成查詢字串。
    query_terms: list[str] = Field(default_factory=list)


def form_fields_path() -> Path:
    """欄位目錄位置：env `GOVCHECK_FORM_FIELDS_PATH` 優先，否則 `data/rag/form_fields.json`。"""
    return resolve_data_path(os.environ.get(ENV_PATH) or DEFAULT_PATH)


def load_form_fields(path: str | Path | None = None) -> list[FieldSpec] | None:
    """載入欄位目錄；任何問題一律回 None 並記 WARNING，**絕不 raise**。"""
    target = Path(path) if path is not None else form_fields_path()
    if not target.exists():
        log.warning("form fields absent path=%s", target.name)
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        log.warning("form fields unreadable err=%s", type(exc).__name__)
        return None
    if not isinstance(raw, dict):
        log.warning("form fields malformed: root is not an object")
        return None
    version = raw.get("schema_version")
    # 版本不符整份丟棄：欄位語意可能已改，半懂半猜地讀進來比沒有更危險。
    if str(version) != str(SCHEMA_VERSION):
        log.warning("form fields schema mismatch got=%s want=%s", version, SCHEMA_VERSION)
        return None
    rows = raw.get("fields")
    if not isinstance(rows, list) or not rows:
        log.warning("form fields malformed: fields is not a non-empty list")
        return None
    fields: list[FieldSpec] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        try:
            field = FieldSpec.model_validate(row)
        except ValidationError as exc:
            log.warning("form fields malformed: %s", type(exc).__name__)
            return None
        key = (field.form, field.field_id)
        if key in seen:
            log.warning("form fields malformed: duplicate field id form=%s", field.form)
            return None
        seen.add(key)
        fields.append(field)
    log.debug("form fields loaded fields=%d", len(fields))
    return fields


def query_text(field: FieldSpec) -> str:
    """欄位的檢索查詢：標籤 + 說明 + 補充關鍵詞。"""
    return " ".join(
        part for part in [field.label, field.description, *field.query_terms] if part
    ).strip()
