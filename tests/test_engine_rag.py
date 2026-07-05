"""engine `_run_rag_checks` 整合測試（p3-03 §4.4 / AC-11、AC-12、AC-13、AC-18）。

涵蓋：
- 回歸零漂移（AC-11）：rag.enabled=false（預設）→ 直接比對 T4a 事先產出的 golden snapshot，證明零漂移；
- rag.enabled=True + fake map + FakeClient → F03.RAG_* 出現（判讀走通）；
- F02-only 送件包（無 F03）+ rag.enabled=True → 仍跑 f02_reg_refs（驗證插入點在 f03 區塊外，AC-12）；
- mapping 缺檔 → 僅多一筆 RAG.SKIPPED，其餘檢查不變（AC-13）；
- 未啟用 → _run_rag_checks 回 []；
- 各式壞輸入（fake map schema 壞、client 失敗）→ 皆回 Finding、絕不 raise（AC-18）。

LLM 一律以 FakeClient mock，不打真端點。fake retrieval_map 以 monkeypatch 注入，不讀磁碟。
"""

from __future__ import annotations

import json
from pathlib import Path

from govcheck.models.f02 import F02Form
from govcheck.models.f03 import F03Checklist, F03ChecklistItem
from govcheck.models.submission import FilePresence, Submission
from govcheck.rag.mapping import RetrievalMapError
from govcheck.rag.models import (
    SCHEMA_VERSION,
    F02QuestionRetrieval,
    F03ItemRetrieval,
    RetrievalMap,
    RetrievedSection,
)
from govcheck.review import engine

from tests.fixture_builder import (
    F01_DEFAULT_ROW,
    build_f01_fixture,
    build_f02_fixture,
    build_f03_fixture,
)

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


# ─────────────────────────────────────────────────────────────────────────────
# 測試替身
# ─────────────────────────────────────────────────────────────────────────────


class FakeClient:
    """依序回傳預備輸出；輸出為 Exception → raise（模擬端點/解析失敗）。範式同 test_f03_evidence_llm。"""

    def __init__(self, outputs: list):
        self._outputs = list(outputs)
        self.calls = 0

    def chat(self, messages, **kw) -> str:
        self.calls += 1
        out = self._outputs.pop(0) if self._outputs else '{"results":[]}'
        if isinstance(out, Exception):
            raise out
        return out


def _fake_map(*, f03_item_id: str = "1-1", f02_qid: str = "UC-04-04") -> RetrievalMap:
    """含一個 F03 檢核項 canonical + 一個 F02 觸發題引用的 fake retrieval_map。"""
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


def _enable_rag(monkeypatch, mapping_path: str = "fake/path.json") -> None:
    """monkeypatch engine.load_rag_config → enabled=True。"""
    cfg = {
        "enabled": True,
        "mapping_path": mapping_path,
        "batch_size": 2,
        "max_sections_per_item": 3,
        "max_excerpt_chars": 300,
        "timeout": 120.0,
        "max_items": 30,
        "embedding_base_url": "http://localhost:11434/v1",
        "embedding_model": "bge-m3",
        "embedding_dim": 1024,
        "milvus_uri": "data/milvus/governance.db",
        "top_k": 4,
        "score_threshold": None,
    }
    monkeypatch.setattr(engine, "load_rag_config", lambda *a, **k: dict(cfg))


def _build_full_submission(tmp: Path) -> dict[str, Path]:
    f01 = tmp / "f01.xlsx"
    f02 = tmp / "f02.xlsm"
    f03 = tmp / "f03.xlsx"
    build_f01_fixture(f01, rows=[dict(F01_DEFAULT_ROW)])
    build_f02_fixture(f02, answers=_BASELINE_ANSWERS, cached_grade="低")
    build_f03_fixture(f03, system_owner="李大華", items=[
        {"row": 5, "state": "是", "proposal": "已完成公平性測試", "golive": "上線後複測通過"},
    ])
    return {"f01": f01, "f02": f02, "f03": f03}


# ─────────────────────────────────────────────────────────────────────────────
# AC-11：回歸零漂移
# ─────────────────────────────────────────────────────────────────────────────


def test_regression_zero_drift() -> None:
    """AC-11：rag.enabled=false（預設）→ engine 輸出與 T4a 事先 commit 的 golden snapshot 完全一致。

    直接呼叫 generate_snapshot.generate_snapshot_data()（在 tempdir 建 synthetic fixture → 跑 engine）
    並用同一 _to_json 決定性序列化，與磁碟上的 phase2_baseline.json 逐字元比對。
    engine 若在預設路徑改變任何輸出，此測試立即紅燈（非「更新 golden」）。
    """
    from tests.golden.generate_snapshot import (
        SNAPSHOT_PATH,
        _to_json,
        generate_snapshot_data,
    )

    fresh_json = _to_json(generate_snapshot_data())
    committed_json = SNAPSHOT_PATH.read_text(encoding="utf-8")
    assert fresh_json == committed_json, (
        "回歸零漂移失敗：rag.enabled=false 下 engine 輸出與 golden snapshot 不一致。"
        "這是 bug，不是更新 golden 的理由。"
    )


def test_run_rag_checks_disabled_returns_empty(monkeypatch) -> None:
    """未啟用（rag.enabled=false 預設）→ _run_rag_checks 回 []（零事件、零 Finding）。"""
    events: list[dict] = []
    sub = Submission(presence=FilePresence(f02=True), f02=F02Form(answers={}))
    # 預設 load_rag_config enabled=false；enable_llm=True 也不啟用（需 AND）
    out = engine._run_rag_checks(sub, enable_llm=True, progress=events.append)
    assert out == []
    assert events == []


def test_run_rag_checks_enable_llm_false_returns_empty(monkeypatch) -> None:
    """rag.enabled=True 但 enable_llm=False → 不啟用（AND 判準）。"""
    _enable_rag(monkeypatch)
    sub = Submission(presence=FilePresence(f02=True), f02=F02Form(answers={}))
    out = engine._run_rag_checks(sub, enable_llm=False)
    assert out == []


# ─────────────────────────────────────────────────────────────────────────────
# AC-11 / F03 判讀走通：F03.RAG_* 出現
# ─────────────────────────────────────────────────────────────────────────────


def test_rag_enabled_f03_findings_appear(monkeypatch) -> None:
    """rag.enabled=True + fake map + FakeClient(gap) → F03.RAG_GAP / RAG_TABLE / RAG_SUMMARY 出現。"""
    _enable_rag(monkeypatch)
    monkeypatch.setattr(engine, "load_retrieval_map", lambda *a, **k: _fake_map())
    gap_resp = json.dumps({"results": [
        {"item_id": "1-1", "verdict": "gap", "gap_refs": [], "reason": "缺口"},
    ]})
    monkeypatch.setattr(engine, "ChatClient", lambda **kw: FakeClient([gap_resp]))

    checklist = F03Checklist(items=[F03ChecklistItem(
        item_id="1-1", topic="公平性", description="D",
        evidence_proposal="已完成公平性測試", evidence_golive="上線後複測通過",
    )], sheet_present=True)
    sub = Submission(presence=FilePresence(f03=True), f03_checklist=checklist)

    out = engine._run_rag_checks(sub, enable_llm=True)
    codes = {f.code for f in out}
    assert "F03.RAG_GAP" in codes
    assert "F03.RAG_TABLE" in codes
    assert "F03.RAG_SUMMARY" in codes


def test_rag_client_uses_rag_timeout(monkeypatch) -> None:
    """RAG 專用 ChatClient 以 cfg.timeout（120）建構，不沿用 chat 的 60s。"""
    _enable_rag(monkeypatch)
    monkeypatch.setattr(engine, "load_retrieval_map", lambda *a, **k: _fake_map())
    captured: dict = {}

    def _fake_client(**kw):
        captured.update(kw)
        return FakeClient(['{"results":[{"item_id":"1-1","verdict":"covered","gap_refs":[],"reason":""}]}'])

    monkeypatch.setattr(engine, "ChatClient", _fake_client)
    checklist = F03Checklist(items=[F03ChecklistItem(
        item_id="1-1", topic="公平性", evidence_proposal="P", evidence_golive="G",
    )], sheet_present=True)
    sub = Submission(presence=FilePresence(f03=True), f03_checklist=checklist)

    engine._run_rag_checks(sub, enable_llm=True)
    assert captured.get("timeout") == 120.0


# ─────────────────────────────────────────────────────────────────────────────
# AC-12：F02-only 送件包也跑 f02_reg_refs（插入點在 f03 區塊外）
# ─────────────────────────────────────────────────────────────────────────────


def test_f02_only_runs_reg_refs(monkeypatch) -> None:
    """F02-only（無 F03）+ enable_llm=True + rag.enabled=True + fake map → 出 F02.REG_REF_*。"""
    _enable_rag(monkeypatch)
    monkeypatch.setattr(engine, "load_retrieval_map", lambda *a, **k: _fake_map())

    # F02Form 以 BASELINE 答案；UC-04-04 觸發（fake map 有其引用）
    sub = Submission(
        presence=FilePresence(f02=True),
        f02=F02Form(answers=dict(_BASELINE_ANSWERS)),
    )
    out = engine._run_rag_checks(sub, enable_llm=True)
    codes = [f.code for f in out]
    assert "F02.REG_REF_NOTE" in codes  # UC-04-04 觸發且 fake map 有引用
    assert "F02.REG_REF_SUMMARY" in codes
    # 無 F03 checklist → 不應有 F03.RAG_* Finding
    assert not any(c.startswith("F03.RAG") for c in codes)


def test_f02_only_reg_refs_via_review_submission(monkeypatch, tmp_path) -> None:
    """端到端：F02-only 送件包經 review_submission，rag 啟用 → 報告含 F02.REG_REF_*（插入點在 f03 區塊外驗證）。"""
    _enable_rag(monkeypatch)
    monkeypatch.setattr(engine, "load_retrieval_map", lambda *a, **k: _fake_map())
    f02 = tmp_path / "f02_only.xlsm"
    build_f02_fixture(f02, answers=_BASELINE_ANSWERS, cached_grade="低")

    report = engine.review_submission({"f02": f02}, enable_llm=True)
    codes = [f.code for f in report.findings]
    assert "F02.REG_REF_SUMMARY" in codes


# ─────────────────────────────────────────────────────────────────────────────
# AC-13：mapping 缺檔 → 僅多一筆 RAG.SKIPPED
# ─────────────────────────────────────────────────────────────────────────────


def test_mapping_missing_only_adds_skipped(monkeypatch, tmp_path) -> None:
    """rag.enabled=True 但 mapping 檔缺 → engine 不 raise，findings 僅較關閉多一筆 RAG.SKIPPED。"""
    f02 = tmp_path / "f02.xlsm"
    build_f02_fixture(f02, answers=_BASELINE_ANSWERS, cached_grade="低")

    # baseline：rag 關閉（預設）
    report_off = engine.review_submission({"f02": f02}, enable_llm=True)
    codes_off = sorted(f.code for f in report_off.findings)

    # rag 啟用但 mapping 檔不存在（真實 load_retrieval_map → RetrievalMapError）
    _enable_rag(monkeypatch, mapping_path=str(tmp_path / "does_not_exist.json"))
    report_on = engine.review_submission({"f02": f02}, enable_llm=True)
    codes_on = sorted(f.code for f in report_on.findings)

    skipped = [f for f in report_on.findings if f.code == "RAG.SKIPPED"]
    assert len(skipped) == 1
    assert skipped[0].severity.value == "info"
    # 移除 RAG.SKIPPED 後與關閉時完全一致（其餘檢查不變）
    codes_on_wo_skip = sorted(c for c in codes_on if c != "RAG.SKIPPED")
    assert codes_on_wo_skip == codes_off


# ─────────────────────────────────────────────────────────────────────────────
# AC-18：各式壞輸入 → 絕不 raise
# ─────────────────────────────────────────────────────────────────────────────


def test_never_raise_on_map_load_error(monkeypatch) -> None:
    """load_retrieval_map 拋任意例外 → 回 [RAG.SKIPPED]，無例外逸出。"""
    _enable_rag(monkeypatch)

    def _boom(*a, **k):
        raise RetrievalMapError("版本不符")

    monkeypatch.setattr(engine, "load_retrieval_map", _boom)
    sub = Submission(presence=FilePresence(f02=True), f02=F02Form(answers={}))
    out = engine._run_rag_checks(sub, enable_llm=True)
    assert [f.code for f in out] == ["RAG.SKIPPED"]


def test_never_raise_on_non_retrievalmap_exception(monkeypatch) -> None:
    """load_retrieval_map 拋非 RetrievalMapError 例外 → 一樣收斂為 RAG.SKIPPED。"""
    _enable_rag(monkeypatch)

    def _boom(*a, **k):
        raise ValueError("unexpected")

    monkeypatch.setattr(engine, "load_retrieval_map", _boom)
    sub = Submission(presence=FilePresence(f02=True), f02=F02Form(answers={}))
    out = engine._run_rag_checks(sub, enable_llm=True)
    assert [f.code for f in out] == ["RAG.SKIPPED"]


def test_never_raise_on_client_construction_failure(monkeypatch) -> None:
    """ChatClient 建構失敗（f03 路徑）→ 收斂為 RAG.SKIPPED，不逸出。"""
    _enable_rag(monkeypatch)
    monkeypatch.setattr(engine, "load_retrieval_map", lambda *a, **k: _fake_map())

    def _boom_client(**kw):
        raise RuntimeError("client init failed")

    monkeypatch.setattr(engine, "ChatClient", _boom_client)
    checklist = F03Checklist(items=[F03ChecklistItem(
        item_id="1-1", evidence_proposal="P", evidence_golive="G",
    )], sheet_present=True)
    sub = Submission(presence=FilePresence(f03=True), f03_checklist=checklist)

    out = engine._run_rag_checks(sub, enable_llm=True)
    assert [f.code for f in out] == ["RAG.SKIPPED"]


def test_never_raise_bad_ragconfig_shape(monkeypatch) -> None:
    """load_rag_config 回傳含多餘鍵/壞型別 → 防禦性過濾 + try 保護，收斂為 RAG.SKIPPED 或正常，絕不 raise。"""
    # timeout 給非法型別，RagConfig.model_validate 會拋 → 應被內層 try 接住成 RAG.SKIPPED
    bad_cfg = {
        "enabled": True, "mapping_path": "x.json", "timeout": "not-a-number",
        "extra_bogus_key": 123,  # 多餘鍵：防禦性過濾應剔除，不觸發 extra="forbid"
    }
    monkeypatch.setattr(engine, "load_rag_config", lambda *a, **k: dict(bad_cfg))
    monkeypatch.setattr(engine, "load_retrieval_map", lambda *a, **k: _fake_map())
    sub = Submission(presence=FilePresence(f02=True), f02=F02Form(answers={}))
    out = engine._run_rag_checks(sub, enable_llm=True)  # 必須不 raise
    assert all(isinstance(f.code, str) for f in out)
    # timeout 壞值 → RagConfig 驗證失敗 → 內層 try → RAG.SKIPPED
    assert [f.code for f in out] == ["RAG.SKIPPED"]
