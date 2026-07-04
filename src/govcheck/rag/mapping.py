"""RetrievalMap runtime loader + 引用顯示 helper（p3-02 §4.4 / p3-03 §4.5）。

runtime 唯一入口：載入 data/rag/retrieval_map.json + schema 驗證；
缺檔 / schema_version 不符 / Pydantic 驗證失敗 → 一律 RetrievalMapError
（由 engine 的 _run_rag_checks 攔截降級為 RAG.SKIPPED，絕不讓例外逸出到介面層）。

**runtime 零 embedding、零 Milvus**：本模組只讀 JSON + Pydantic 驗證，
不 import EmbeddingClient / RegulationStore。
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from govcheck.logging_setup import get_logger
from govcheck.rag.models import SCHEMA_VERSION, RetrievalMap

log = get_logger("rag_mapping")

_DEFAULT_PATH = Path("data/rag/retrieval_map.json")


class RetrievalMapError(RuntimeError):
    """retrieval_map 缺檔 / 版本不符 / schema 驗證失敗。"""


def load_retrieval_map(path: str | Path | None = None) -> RetrievalMap:
    """從磁碟載入 retrieval_map.json；任何失敗一律丟 RetrievalMapError（訊息含路徑，不含內容）。"""
    p = Path(path) if path is not None else _DEFAULT_PATH
    log.debug("load_retrieval_map path=%s", p)

    if not p.exists():
        raise RetrievalMapError(f"mapping 檔案不存在：{p}")

    try:
        with p.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        raise RetrievalMapError(f"mapping JSON 解析失敗（{p}）：{type(exc).__name__}") from exc

    if not isinstance(data, dict):
        raise RetrievalMapError(f"mapping 內容非 JSON 物件（{p}）")

    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise RetrievalMapError(
            f"mapping 版本不符（{p}）：expected {SCHEMA_VERSION}, got {version}"
        )

    try:
        mapping = RetrievalMap.model_validate(data)
    except ValidationError as exc:
        # 只記/夾錯誤計數與型別，不夾原始內容（excerpt 為法規摘錄）
        raise RetrievalMapError(
            f"mapping schema 驗證失敗（{p}）：{exc.error_count()} 個欄位錯誤"
        ) from exc

    log.debug(
        "load_retrieval_map ok f03_items=%d f02_questions=%d",
        len(mapping.f03_items), len(mapping.f02_questions),
    )
    return mapping


def format_section_ref(reg_code: str, section_path: str) -> str:
    """section_path 正準形式 → 顯示字串（f03_rag / f02_reg_refs 共用；spec §4.5）。

    - 章節式 ``五/(二)/2`` → ``R03 五、(二) 2``（首段中文序號後接「、」，
      括號層直接接續，阿拉伯數字等其餘段前置空格）。
    - 條文式 ``第一條`` → ``R01 第一條``（單段直接接合）。
    """
    parts = [p for p in section_path.split("/") if p]
    if not parts:
        return reg_code
    if len(parts) == 1:
        return f"{reg_code} {parts[0]}"

    out = parts[0] + "、"
    for part in parts[1:]:
        if part.startswith("("):
            out += part                          # 括號層：直接接續
        elif out.endswith("、"):
            out += part                          # 「、」之後不再補空格
        else:
            out += " " + part                    # 阿拉伯數字等：前置空格
    return f"{reg_code} {out}"
