"""engine 編排整合測試：法規對照表接線 + 缺表降級 + 回歸零漂移。

涵蓋：
- 回歸零漂移：對照表缺席（新環境的常態）→ 直接比對事先產出的 golden snapshot；
- 對照表可用 + F02 觸發題 → 報告出現 F02.REG_REF_*（需求「專案內容是否合規」的規則側）；
- 對照表缺檔 → 僅多一筆 REGULATION.MAP_UNAVAILABLE，其餘檢查逐碼不變、絕不 raise；
- 對照表載入拋任意例外 → 一律收斂為 None（降級），不逸出到介面層。

LLM 一律 mock，不打真端點。fake retrieval_map 以 monkeypatch 注入，不讀磁碟。
"""

from __future__ import annotations

from pathlib import Path

from govcheck.regulations.mapping import RetrievalMapError
from govcheck.regulations.models import (
    SCHEMA_VERSION,
    F02QuestionRetrieval,
    F03ItemRetrieval,
    RetrievalMap,
    RetrievedSection,
)
from govcheck.review import engine
from tests.fixture_builder import requires_templates, build_f02_fixture

pytestmark = requires_templates

# BASELINE 答案集（與 tests/golden/generate_snapshot.py 一致；避免跨測試模組耦合）。
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


def _fake_map(*, f03_item_id: str = "1-1", f02_qid: str = "UC-04-04") -> RetrievalMap:
    """含一個 F03 檢核項 canonical + 一個 F02 觸發題引用的 fake 對照表。"""
    return RetrievalMap(
        schema_version=SCHEMA_VERSION,
        built_at="2026-07-04T00:00:00+08:00",
        embedding_model="fake-embed",
        f03_items={
            f03_item_id: F03ItemRetrieval(
                item_id=f03_item_id,
                canonical_topic="公平性",
                canonical_description="D",
                canonical_ref_raw="R03",
                sections=[RetrievedSection(
                    reg_code="R03", section_path="五/(二)/2",
                    title="公平性測試", excerpt="條文摘錄", origin="curated",
                )],
            ),
        },
        f02_questions={
            f02_qid: F02QuestionRetrieval(
                qid=f02_qid,
                sections=[RetrievedSection(
                    reg_code="R03", section_path="第一條",
                    title="適用範圍", excerpt="摘錄", origin="semantic", score=0.88,
                )],
            ),
        },
    )


def _no_map(monkeypatch) -> None:
    """把對照表指到不存在的路徑：測試結果不受本機是否有 data/rag/retrieval_map.json 影響。"""
    monkeypatch.setenv("GOVCHECK_REGULATIONS_MAPPING_PATH", "/nonexistent/retrieval_map.json")


# ─────────────────────────────────────────────────────────────────────────────
# 回歸零漂移
# ─────────────────────────────────────────────────────────────────────────────


def test_regression_zero_drift(monkeypatch) -> None:
    """對照表缺席時，engine 輸出與事先 commit 的 golden snapshot 逐字元一致。

    engine 若在此路徑改變任何輸出，此測試立即紅燈（是 bug，不是「更新 golden」的理由）。
    """
    _no_map(monkeypatch)
    from tests.golden.generate_snapshot import (
        SNAPSHOT_PATH,
        _to_json,
        generate_snapshot_data,
    )

    fresh_json = _to_json(generate_snapshot_data())
    committed_json = SNAPSHOT_PATH.read_text(encoding="utf-8")
    assert fresh_json == committed_json, (
        "回歸零漂移失敗：engine 輸出與 golden snapshot 不一致。"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 法規查表接線（需求「專案內容是否合規」的規則側）
# ─────────────────────────────────────────────────────────────────────────────


def test_f02_reg_refs_run_without_llm(monkeypatch, tmp_path: Path) -> None:
    """對照表可用 + F02 觸發題 → 出 F02.REG_REF_*，且不需要啟用 LLM（純查表）。"""
    monkeypatch.setattr(engine, "load_retrieval_map", lambda *a, **k: _fake_map())
    f02 = tmp_path / "f02_only.xlsm"
    build_f02_fixture(f02, answers=_BASELINE_ANSWERS, cached_grade="低")

    codes = [f.code for f in engine.review_submission({"f02": f02}, enable_llm=False).findings]
    assert "F02.REG_REF_NOTE" in codes      # UC-04-04 觸發且 fake map 有引用
    assert "F02.REG_REF_SUMMARY" in codes
    assert "REGULATION.MAP_UNAVAILABLE" not in codes


def test_missing_map_only_adds_one_notice(monkeypatch, tmp_path: Path) -> None:
    """對照表缺檔 → 不 raise；findings 僅較「對照表可用」少查表結果、多一筆 MAP_UNAVAILABLE。"""
    f02 = tmp_path / "f02.xlsm"
    build_f02_fixture(f02, answers=_BASELINE_ANSWERS, cached_grade="低")

    monkeypatch.setattr(engine, "load_retrieval_map", lambda *a, **k: _fake_map())
    with_map = sorted(
        f.code for f in engine.review_submission({"f02": f02}).findings
        if not f.code.startswith("F02.REG_REF")
    )

    def _missing(*a, **k):
        raise RetrievalMapError("mapping 檔案不存在")

    monkeypatch.setattr(engine, "load_retrieval_map", _missing)
    report = engine.review_submission({"f02": f02})
    notices = [f for f in report.findings if f.code == "REGULATION.MAP_UNAVAILABLE"]
    assert len(notices) == 1
    assert notices[0].severity.value == "info"
    without_map = sorted(
        f.code for f in report.findings if f.code != "REGULATION.MAP_UNAVAILABLE"
    )
    assert without_map == with_map


def test_map_load_error_degrades_to_none(monkeypatch) -> None:
    """對照表載入拋任意例外 → _load_regulation_map 回 None（降級），不逸出。"""
    for exc in (RetrievalMapError("版本不符"), ValueError("unexpected")):
        def _boom(*a, _exc=exc, **k):
            raise _exc

        monkeypatch.setattr(engine, "load_retrieval_map", _boom)
        assert engine._load_regulation_map({"mapping_path": "x.json"}) is None


def test_no_f02_no_f03_skips_map_notice(monkeypatch, tmp_path: Path) -> None:
    """只有 F01 時沒有可比對的對象 → 不發 MAP_UNAVAILABLE（避免無意義噪音）。"""
    _no_map(monkeypatch)
    from tests.fixture_builder import build_f01_fixture

    f01 = build_f01_fixture(tmp_path / "f01.xlsx")
    codes = [f.code for f in engine.review_submission({"f01": f01}).findings]
    assert "REGULATION.MAP_UNAVAILABLE" not in codes


def test_progress_contract_emits_skipped_layers_and_full_final_report(monkeypatch) -> None:
    """非 SSE 呼叫端也能只靠 progress callback 取得完整且可結束的狀態。"""
    _no_map(monkeypatch)
    events: list[dict] = []
    audits: list[tuple[str, dict]] = []
    monkeypatch.setattr(engine, "audit", lambda event, **fields: audits.append((event, fields)))

    report = engine.review_submission({}, progress=events.append)

    layer_states = [
        (event["id"], event["state"])
        for event in events
        if event["kind"] == "layer"
    ]
    assert ("L2", "skipped") in layer_states
    assert ("L3", "skipped") in layer_states
    final = next(event for event in events if event["kind"] == "final")
    assert final["report"] is report
    assert audits == [("review_done", audits[0][1])]
    assert audits[0][1]["review_id"] == report.review_id
