"""治理紀錄匯出：把 audit.log 的審查事件與 output/decisions 的覆核決定整理成一份可交接的 JSON。

能力目錄（ts_solution）要把每個專案的治理文件、審查、核准與使用紀錄連在一起，
需要的不是報告全文，而是**每次審查的識別資訊、結論與覆核計數**。本模組只讀既有落檔
（稽核 log 與覆核決定檔），不碰審查管線，也不產生任何新的判讀內容。

輸出契約（``schema_version`` 1）::

    {
      "schema_version": 1,
      "exported_at": "<UTC ISO>",
      "reviews": [
        {
          "review_id": "<32 hex>" | null,        # 舊版稽核行沒有 review_id
          "ts": "<UTC ISO>" | null,              # 舊版稽核行沒有 ts
          "subject": "<F01 專案/服務名稱>" | null,
          "filing_unit": "<送件單位>" | null,
          "form_type": "送件包",
          "workflow_stage": "proposal" | "golive" | null,
          "decision_status": "approved" | "changes_required" | "manual_review" | "incomplete"
                             | null,                 # 舊版稽核行只有 passed，不反推結論
          "risk_grade": "低" | "中" | "高" | null,
          "risk_score": <float> | null,
          "error": <int>, "warn": <int>,
          "enable_llm": <bool>, "operator": "<str>", "request_id": "<str>",
          "decisions": {"updated_at", "items", "accept", "reject", "na", "pending"} | null
        }
      ]
    }

隱私邊界與 audit.log 相同：只有識別資訊與計數，**沒有**檔案內容、佐證原文、檢核項原文，
也刻意不帶覆核備註（note 是治理人員的自由文字，留在決定檔即可）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from govcheck.logging_setup import get_logger
from govcheck.web.decisions import DECISION_VALUES, REVIEW_ID_RE

log = get_logger("governance_export")

SCHEMA_VERSION = 1

# 稽核行裡直接照抄進匯出檔的欄位；不在此清單的鍵一律不帶（新加的稽核欄位要先經過這裡）。
_REVIEW_FIELDS = (
    "review_id", "ts", "subject", "filing_unit", "form_type", "workflow_stage",
    "decision_status", "risk_grade", "risk_score", "error", "warn", "enable_llm",
    "operator", "request_id",
)
_TALLY_KEYS = ("accept", "reject", "na", "pending")


def read_audit(path: str | Path) -> list[dict]:
    """讀 JSON-lines 稽核檔；壞行跳過並記 WARNING，不讓一行損毀擋掉整份匯出。"""
    p = Path(path)
    if not p.exists():
        return []
    records: list[dict] = []
    for number, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            log.warning("audit line skipped line=%d", number)
            continue
        if isinstance(record, dict) and isinstance(record.get("event"), str):
            records.append(record)
    return records


def read_decisions(directory: str | Path) -> dict[str, dict]:
    """讀覆核決定檔 → review_id → 計數摘要；只算數量，不帶項次、備註。"""
    d = Path(directory)
    if not d.is_dir():
        return {}
    summary: dict[str, dict] = {}
    for path in sorted(d.glob("*.json")):
        review_id = path.stem
        if not REVIEW_ID_RE.match(review_id):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("decision file skipped review_id=%s", review_id)
            continue
        decisions = payload.get("decisions") if isinstance(payload, dict) else None
        if not isinstance(decisions, dict):
            continue
        tally = {key: 0 for key in _TALLY_KEYS}
        for value in decisions.values():
            if value in DECISION_VALUES:
                tally[value] += 1
        summary[review_id] = {
            "updated_at": payload.get("updated_at"),
            "items": len(decisions),
            **tally,
        }
    return summary


def export_records(audit_path: str | Path, decisions_dir: str | Path) -> dict:
    """組出匯出契約。決定檔優先；沒有檔案時退回稽核 ``decisions_saved`` 的計數。"""
    audit_records = read_audit(audit_path)
    from_files = read_decisions(decisions_dir)
    from_audit: dict[str, dict] = {}
    for record in audit_records:
        if record.get("event") == "decisions_saved" and record.get("review_id"):
            from_audit[record["review_id"]] = {
                "updated_at": record.get("ts"),
                "items": record.get("items", 0),
                **{key: record.get(key, 0) for key in _TALLY_KEYS},
            }

    reviews = []
    for record in audit_records:
        if record.get("event") != "review_done":
            continue
        row = {key: record.get(key) for key in _REVIEW_FIELDS}
        review_id = row["review_id"]
        row["decisions"] = None
        if review_id:
            row["decisions"] = from_files.get(review_id) or from_audit.get(review_id)
        reviews.append(row)
    # 有時間戳的依時間排，沒有的（舊版稽核行）保持檔案順序排在最後。
    reviews.sort(key=lambda r: (r["ts"] is None, r["ts"] or ""))
    return {
        "schema_version": SCHEMA_VERSION,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "reviews": reviews,
    }


def write_records(records: dict, out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out
