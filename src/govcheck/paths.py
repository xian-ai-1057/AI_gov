"""設定檔中相對路徑的統一解析。

設定檔（llm_config.yaml / .env）裡的 `data/rag/retrieval_map.json`、`prompts` 等相對路徑，
若直接交給 open()／Path.exists() 就是相對於**行程的工作目錄**。從 repo root 以外的位置啟動
（IDE launch config 未指定 cwd、systemd unit、`uvicorn` 從上層目錄跑）就會找不到檔案，
表現成 RAG.SKIPPED / F03.PROMPT_UNAVAILABLE 這類降級訊號，而非明確的組態錯誤。

解析順序刻意保守，維持既有行為：
1. 絕對路徑 → 原樣使用
2. 相對於 cwd 存在 → 用 cwd 版本（既有腳本與測試的行為不變）
3. 否則 → 退回 repo root 錨定版本
"""

from __future__ import annotations

from pathlib import Path

# 本檔位於 src/govcheck/ → parents[2] 為 repo root（與 logging_setup 同一慣例）
REPO_ROOT = Path(__file__).resolve().parents[2]


def resolve_data_path(path: str | Path) -> Path:
    """把設定來的相對路徑錨定到 repo root，讓啟動位置不影響檔案是否找得到。"""
    p = Path(path)
    if p.is_absolute() or p.exists():
        return p
    return REPO_ROOT / p
