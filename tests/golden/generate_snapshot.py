"""Phase 2 基準 golden snapshot 產生器。

用**未修改的現行 engine** 對 synthetic 送件包執行審查，把 ReviewReport 以決定性
序列化寫入 tests/golden/phase2_baseline.json，供 Phase 3b 後的回歸測試確認
rag.enabled=false（預設）時輸出與快照逐字節相同。

重新產生指令（任何人都可重跑；覆蓋既有快照時應再 commit）：
    uv run python tests/golden/generate_snapshot.py

決定性保證：
  - 輸入固定：synthetic 送件包由 tests/fixture_builder.py 的 BASELINE 答案集產生，
    不依賴任何隨機或時間成分。
  - 序列化固定：json.dumps(sort_keys=True) 確保物件鍵排序穩定；
    findings 順序維持 engine 的 sort(severity_order) 結果（Python stable sort 保序）。
  - 排除動態欄位：report.banner 為靜態字串，包含亦穩定；無 timestamp 欄位在模型中。

重跑驗證：腳本第二次執行時若快照已存在，會在覆蓋前先比對內容，差異則報錯。
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

# ── 路徑設定 ──────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[2]
# uv run 已把 govcheck 安裝成 editable package；src 路徑僅作為保險
sys.path.insert(0, str(ROOT / "src"))
# tests/ 需要在 path 以便匯入 tests.fixture_builder
sys.path.insert(0, str(ROOT))

from govcheck.review.engine import review_submission  # noqa: E402

# 匯入 fixture builder（需 ROOT 在 path）
from tests.fixture_builder import (  # noqa: E402
    F01_DEFAULT_ROW,
    build_f01_fixture,
    build_f02_fixture,
    build_f03_fixture,
)

# 合規低風險答案集（與 tests/test_f02_rules.py 共用 BASELINE）
# 內嵌一份，避免跨測試模組依賴，讓本腳本獨立可執行
_BASELINE_ANSWERS: dict[str, str] = {
    "UC-01": "N", "UC-02": "N", "UC-03": "N",
    "UC-04-01": "N", "UC-04-02": "N", "UC-04-03": "N", "UC-04-04": "Y",
    "UC-05-01": "N", "UC-05-02": "N", "UC-05-03": "N", "UC-05-04": "Y",
    "UC-06-01": "N", "UC-06-02": "N", "UC-06-03": "Y",
    "UC-07": "Y",
    "D-01-01": "N", "D-01-02": "N", "D-01-03": "N", "D-01-04": "Y",
    "D-02-01": "Y", "D-02-02": "N", "D-02-03": "N", "D-02-04": "N",
    "D-03": "N", "D-04": "Y", "M-01": "Y", "M-02": "Y",
    "M-03-01": "Y", "M-03-02": "N",
}

SNAPSHOT_PATH = Path(__file__).resolve().parent / "phase2_baseline.json"

SCHEMA_VERSION = "phase2_baseline_v1"


# ── 送件包建立 ────────────────────────────────────────────────────────────────


def _build_full_submission(tmp: Path) -> dict[str, Path]:
    """Full F01 + F02 + F03 送件包（合規低風險基線）。"""
    f01_path = tmp / "f01.xlsx"
    f02_path = tmp / "f02.xlsm"
    f03_path = tmp / "f03.xlsx"
    build_f01_fixture(f01_path, rows=[dict(F01_DEFAULT_ROW)])
    build_f02_fixture(f02_path, answers=_BASELINE_ANSWERS, cached_grade="低")
    build_f03_fixture(f03_path, system_owner="李大華")  # 與 F01_DEFAULT_ROW H 欄對齊
    return {"f01": f01_path, "f02": f02_path, "f03": f03_path}


def _build_f02_only_submission(tmp: Path) -> dict[str, Path]:
    """F02-only 送件包（缺 F01/F03；用來驗證缺件 Finding 代碼）。"""
    f02_path = tmp / "f02_only.xlsm"
    build_f02_fixture(f02_path, answers=_BASELINE_ANSWERS, cached_grade="低")
    return {"f02": f02_path}


# ── 快照資料產生 ──────────────────────────────────────────────────────────────


def _serialize_report(report) -> dict:
    """把 ReviewReport 序列化為純 JSON-safe dict（enum → str，所有欄位含 banner）。

    mode="json" 讓 Pydantic 把 Enum 轉字串值、None 保留 None，
    不引入任何執行期隨機/時間成分。
    """
    return report.model_dump(mode="json")


def generate_snapshot_data() -> dict:
    """在 tempdir 中建 fixture → 呼叫 engine → 回傳快照 dict。"""
    with tempfile.TemporaryDirectory(prefix="govcheck_golden_") as td:
        tmp = Path(td)

        full_report = review_submission(
            _build_full_submission(tmp / "full"),
            enable_llm=False,
        )

        f02_only_report = review_submission(
            _build_f02_only_submission(tmp / "f02only"),
            enable_llm=False,
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_by": "tests/golden/generate_snapshot.py",
        "note": (
            "Re-generate: uv run python tests/golden/generate_snapshot.py  "
            "| 此快照依賴未修改的 Phase 2 engine；Phase 3b 修改 engine 前先 commit 此快照。"
        ),
        "submissions": [
            {
                "label": "full_f01_f02_f03",
                "description": "F01+F02+F03 合規低風險基線；預期 findings=SUBMISSION.OK",
                "report": _serialize_report(full_report),
            },
            {
                "label": "f02_only",
                "description": "僅 F02、缺 F01/F03；預期 findings 含 DOC.MISSING_F01 / DOC.MISSING_F03",
                "report": _serialize_report(f02_only_report),
            },
        ],
    }


def _to_json(data: dict) -> str:
    """決定性 JSON 序列化：sort_keys + indent=2 + ensure_ascii=False。"""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── 統計報告 ──────────────────────────────────────────────────────────────────


def _print_stats(data: dict) -> None:
    for sub in data["submissions"]:
        label = sub["label"]
        findings = sub["report"]["findings"]
        by_sev: dict[str, int] = {}
        for f in findings:
            sev = f["severity"]
            by_sev[sev] = by_sev.get(sev, 0) + 1
        codes = [f["code"] for f in findings]
        print(f"  [{label}] {len(findings)} findings, severity breakdown: {by_sev}")
        print(f"    codes: {codes}")


# ── 主程式 ────────────────────────────────────────────────────────────────────


def main() -> int:
    """產生快照；若快照已存在則先比對，確認決定性後再覆蓋。回傳 0=成功，1=決定性驗證失敗。"""
    print("Generating Phase 2 golden snapshot...")
    data = generate_snapshot_data()
    new_json = _to_json(data)
    new_hash = _sha256(new_json)

    determinism_ok = True
    if SNAPSHOT_PATH.exists():
        existing_json = SNAPSHOT_PATH.read_text(encoding="utf-8")
        existing_hash = _sha256(existing_json)
        if existing_hash == new_hash:
            print(f"[OK] Determinism verified: SHA-256 matches existing snapshot ({new_hash[:16]}…)")
        else:
            print(
                "[FAIL] Non-determinism detected: new output differs from existing snapshot!\n"
                f"  existing SHA-256: {existing_hash[:16]}…\n"
                f"  new      SHA-256: {new_hash[:16]}…\n"
                "  Investigate before overwriting.",
            )
            determinism_ok = False
            # 仍覆蓋以便 diff 查看差異，但返回非零退出碼
    else:
        print(f"[NEW] No existing snapshot; writing fresh baseline ({new_hash[:16]}…)")

    SNAPSHOT_PATH.write_text(new_json, encoding="utf-8")
    size = SNAPSHOT_PATH.stat().st_size
    print(f"Written: {SNAPSHOT_PATH}")
    print(f"Size: {size} bytes  |  SHA-256: {new_hash}")
    print()
    _print_stats(data)

    if not determinism_ok:
        print("\n[FAIL] Please investigate non-determinism before committing.")
        return 1

    print("\n[DONE] Snapshot ready. Next steps:")
    print("  1. git add tests/golden/phase2_baseline.json tests/golden/")
    print("  2. Commit (before any engine changes)")
    print("  3. After Phase 3b engine changes, AC-11 regression test will compare against this snapshot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
