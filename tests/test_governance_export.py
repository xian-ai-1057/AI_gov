"""治理紀錄匯出：只讀稽核 log 與決定檔，輸出識別資訊與計數，不帶任何內文。"""

from __future__ import annotations

import json
from pathlib import Path

from govcheck.report import governance_export as ge
from govcheck.web import decisions as store

_ID = "a" * 32
_LEGACY_FREE = "b" * 32


def _audit_file(tmp_path: Path, lines: list[dict | str]) -> Path:
    path = tmp_path / "audit.log"
    text = "\n".join(
        line if isinstance(line, str) else json.dumps(line, ensure_ascii=False) for line in lines
    )
    path.write_text(text + "\n", encoding="utf-8")
    return path


def _review_line(**overrides) -> dict:
    base = {
        "event": "review_done", "ts": "2026-09-01T01:00:00+00:00", "request_id": "r1",
        "operator": "-", "review_id": _ID, "subject": "系統A", "filing_unit": "風控部",
        "form_type": "送件包", "workflow_stage": "proposal", "risk_grade": "中",
        "risk_score": 58.0, "error": 1, "warn": 2, "decision_status": "changes_required",
        "enable_llm": False, "parser_documents": 2, "review_llm_calls": 0, "duration_ms": 30,
    }
    base.update(overrides)
    return base


def test_export_links_decisions_from_files(tmp_path, monkeypatch):
    monkeypatch.setenv("GOVCHECK_DECISION_DIR", str(tmp_path / "decisions"))
    store.save(_ID, {"1-1": "accept", "1-2": "reject", "2-1": "pending"},
               subject="系統A", note="這段備註不該外流")
    audit = _audit_file(tmp_path, [_review_line()])

    out = ge.export_records(audit, store.decision_dir())

    assert out["schema_version"] == 1
    assert len(out["reviews"]) == 1
    row = out["reviews"][0]
    assert row["review_id"] == _ID
    assert row["subject"] == "系統A" and row["filing_unit"] == "風控部"
    assert row["workflow_stage"] == "proposal" and row["risk_grade"] == "中"
    assert row["decisions"] == {
        "updated_at": row["decisions"]["updated_at"],
        "items": 3, "accept": 1, "reject": 1, "na": 0, "pending": 1,
    }
    assert row["decisions"]["updated_at"]
    # 稽核行裡的耗時、parser 計數等技術欄位不進交接契約；備註更不該出現。
    assert "duration_ms" not in row and "note" not in json.dumps(out, ensure_ascii=False)
    assert "這段備註不該外流" not in json.dumps(out, ensure_ascii=False)


def test_export_falls_back_to_audit_tally_when_file_missing(tmp_path):
    audit = _audit_file(tmp_path, [
        _review_line(),
        {"event": "decisions_saved", "ts": "2026-09-01T02:00:00+00:00", "request_id": "r2",
         "operator": "-", "review_id": _ID, "subject": "系統A", "items": 20,
         "accept": 1, "reject": 0, "na": 0, "pending": 19},
    ])

    row = ge.export_records(audit, tmp_path / "no-such-dir")["reviews"][0]

    assert row["decisions"] == {"updated_at": "2026-09-01T02:00:00+00:00", "items": 20,
                                "accept": 1, "reject": 0, "na": 0, "pending": 19}


def test_legacy_lines_without_review_id_or_ts_are_kept_and_sorted_last(tmp_path):
    legacy = {"event": "review_done", "request_id": "old", "operator": "-", "subject": None,
              "filing_unit": None, "form_type": "送件包", "error": 1, "warn": 2,
              "decision_status": "changes_required", "enable_llm": True}
    audit = _audit_file(tmp_path, [
        legacy,
        _review_line(ts="2026-09-02T00:00:00+00:00", review_id=_LEGACY_FREE, subject="系統B"),
        _review_line(ts="2026-09-01T00:00:00+00:00"),
    ])

    reviews = ge.export_records(audit, tmp_path / "decisions")["reviews"]

    assert [r["subject"] for r in reviews] == ["系統A", "系統B", None]
    assert reviews[-1]["review_id"] is None and reviews[-1]["ts"] is None
    assert reviews[-1]["decisions"] is None
    assert reviews[-1]["workflow_stage"] is None  # 舊行沒有的欄位以 null 呈現，不猜值


def test_corrupt_audit_line_and_bad_decision_file_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("GOVCHECK_DECISION_DIR", str(tmp_path / "decisions"))
    store.save(_ID, {"1-1": "accept"})
    (store.decision_dir() / f"{_LEGACY_FREE}.json").write_text("{ broken", encoding="utf-8")
    (store.decision_dir() / "not-a-review-id.json").write_text("{}", encoding="utf-8")
    audit = _audit_file(tmp_path, ["{ not json", _review_line(),
                                   _review_line(review_id=_LEGACY_FREE, subject="系統B")])

    reviews = ge.export_records(audit, store.decision_dir())["reviews"]

    assert len(reviews) == 2
    assert reviews[0]["decisions"]["accept"] == 1
    assert reviews[1]["decisions"] is None


def test_missing_audit_file_exports_empty_contract(tmp_path):
    out = ge.export_records(tmp_path / "missing.log", tmp_path / "missing")
    assert out["reviews"] == [] and out["exported_at"]


def test_script_writes_file(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "export_script", Path(__file__).resolve().parents[1] / "scripts" / "export_governance_records.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    audit = _audit_file(tmp_path, [_review_line()])
    out = tmp_path / "records.json"

    assert module.main(["--audit", str(audit), "--decisions-dir", str(tmp_path / "d"),
                        "--out", str(out)]) == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["reviews"][0]["review_id"] == _ID
