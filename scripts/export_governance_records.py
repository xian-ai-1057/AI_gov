"""把稽核 log 與覆核決定匯出成能力目錄可讀的治理紀錄 JSON。

用法（repo root）：
    uv run python scripts/export_governance_records.py
    uv run python scripts/export_governance_records.py --out /path/to/governance_records.json

預設讀 logs/audit.log 與 output/decisions/，寫到 output/governance_records.json
（output/ 已 gitignore；檔案含系統名與送件單位等識別資訊，不入庫、不外送）。
接著在 ts_solution 執行 `python3 poc/cli.py governance import <該檔路徑>` 即可串進目錄。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govcheck.logging_setup import load_log_config  # noqa: E402
from govcheck.report.governance_export import export_records, write_records  # noqa: E402
from govcheck.web.decisions import decision_dir  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="匯出治理紀錄（審查事件 + 覆核計數）")
    parser.add_argument("--audit", default=None, help="稽核檔路徑（預設 <log dir>/audit.log）")
    parser.add_argument("--decisions-dir", default=None, help="覆核決定目錄（預設 output/decisions）")
    parser.add_argument("--out", default=str(ROOT / "output" / "governance_records.json"))
    args = parser.parse_args(argv)

    audit_path = Path(args.audit) if args.audit else Path(load_log_config()["dir"]) / "audit.log"
    decisions = Path(args.decisions_dir) if args.decisions_dir else decision_dir()
    records = export_records(audit_path, decisions)
    out = write_records(records, args.out)
    linked = sum(1 for r in records["reviews"] if r["decisions"])
    print(f"wrote {out}  reviews={len(records['reviews'])} with_decisions={linked}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
