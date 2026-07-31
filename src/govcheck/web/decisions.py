"""覆核決定的地端檔案式儲存。

治理人員在報告頁對每個檢核項下的判斷（接受初判／不同意／不適用）必須有地方落腳，
否則決定只能回到 Excel 與郵件，這份報告就沒有出口。

兩個用途：
  1. 稽核軌跡——誰在哪一次審查對哪一項下了什麼判斷。
  2. 累積標註資料，日後用來算每一種檢查的準確率。準確率不足的檢查維持「試辦」、
     不進主清單，這是讓 L2／L3 逐步被信任的唯一機制。

**這不是 session 還原**：送件檔在審查結束時就已從暫存目錄刪除，重載頁面不會（也不該）
把報告本體變回來。這裡存的只有決定本身。

資料落在 `output/decisions/`（已 gitignore）。內容含檢核項次與決定，
**不含**佐證原文或任何檔案內容。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from govcheck.logging_setup import audit, get_logger

log = get_logger("decisions")

# review_id 由 engine 以 uuid4().hex 產生。這條 regex 同時是**路徑穿越的唯一防線**：
# review_id 會被拿來組檔名，放行 "../" 或絕對路徑等於讓呼叫端指定任意寫入位置。
REVIEW_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# 覆核決定的值域。與前端下拉選單一致；未列的值一律拒絕，不靜默存進去。
DECISION_VALUES = frozenset({"pending", "accept", "reject", "na"})

_DEFAULT_DIR = Path("output/decisions")
_SCHEMA_VERSION = 1


class DecisionError(ValueError):
    """輸入不合法（review_id 格式、決定值域、項次型別）。介面層轉成 400。"""


def decision_dir() -> Path:
    """儲存目錄；可用 GOVCHECK_DECISION_DIR 覆寫（測試與不同佈署環境用）。"""
    return Path(os.environ.get("GOVCHECK_DECISION_DIR") or _DEFAULT_DIR)


def _path_for(review_id: str) -> Path:
    if not REVIEW_ID_RE.match(review_id or ""):
        raise DecisionError("review_id 格式不正確。")
    return decision_dir() / f"{review_id}.json"


def _validate(decisions: object) -> dict[str, str]:
    if not isinstance(decisions, dict):
        raise DecisionError("decisions 應為物件（項次 → 決定）。")
    validated: dict[str, str] = {}
    for item_id, value in decisions.items():
        if not isinstance(item_id, str) or not item_id.strip():
            raise DecisionError("項次必須是非空字串。")
        if value not in DECISION_VALUES:
            raise DecisionError(f"未知的覆核決定 {value!r}。")
        validated[item_id] = value
    return validated


def save(
    review_id: str,
    decisions: object,
    *,
    subject: str | None = None,
    note: str | None = None,
) -> dict:
    """整批覆寫某次審查的覆核決定，回傳落檔內容。

    刻意用整批覆寫而非逐項合併：前端持有完整狀態，合併語意會讓「把某項改回待確認」
    變得無法表達。
    """
    path = _path_for(review_id)
    validated = _validate(decisions)
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "review_id": review_id,
        "subject": subject,
        "note": note,
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "decisions": validated,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    # 先寫暫存檔再 rename：中途失敗不會留下半截 JSON 讓下次讀取整個炸掉。
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        tmp_path = Path(handle.name)
    tmp_path.replace(path)

    tally: dict[str, int] = {}
    for value in validated.values():
        tally[value] = tally.get(value, 0) + 1
    log.info("decisions saved review_id=%s items=%d", review_id, len(validated))
    # 稽核只記識別資訊與計數；檢核項原文、佐證內容一律不進 audit.log。
    audit(
        "decisions_saved",
        review_id=review_id,
        subject=subject,
        items=len(validated),
        accept=tally.get("accept", 0),
        reject=tally.get("reject", 0),
        na=tally.get("na", 0),
        pending=tally.get("pending", 0),
    )
    return payload


def load(review_id: str) -> dict | None:
    """讀回某次審查的覆核決定；沒有存過回 None。

    檔案損毀（手動編輯過、寫入中斷）視同沒有：讀取失敗不該讓報告頁整頁掛掉。
    """
    path = _path_for(review_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("decisions unreadable review_id=%s err=%s", review_id, type(exc).__name__)
        return None
