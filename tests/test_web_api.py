"""Web API（FastAPI）契約測試。

分兩類：
  - 無須官方範本（PDF / 純路由錯誤）→ 恆可跑，無網路確定性基線。
  - 須官方範本（F01/F02/F03 真檔端到端）→ data/original 未連結時 skip。

LLM 一律不啟用（enable_llm 預設關，確定性）；介面層只驗證契約，不重造 ground truth。
法規對照表一律視為缺席，讓結果不受本機是否有 data/rag/retrieval_map.json 影響。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from govcheck.web.api import app
from tests.fixture_builder import (
    OFFICIAL,
    OFFICIAL_F01,
    OFFICIAL_F03,
    build_f01_fixture,
    build_f02_fixture,
    build_f03_fixture,
)

client = TestClient(app)

requires_templates = pytest.mark.skipif(
    not (OFFICIAL.exists() and OFFICIAL_F01.exists() and OFFICIAL_F03.exists()),
    reason="官方範本不存在（data/original 未連結）",
)

# 報告 payload 的頂層契約：前端會直接讀這些鍵，少任一個 UI 就顯示 undefined。
REPORT_KEYS = {
    "form_type", "subject", "decision_status", "workflow_stage",
    "error_count", "warn_count", "info_count",
    "risk_score", "risk_grade", "findings",
    "item_assessments", "checklist_roster", "document_statuses",
    "citations", "usage_summary", "review_run_meta", "markdown",
}


@pytest.fixture(autouse=True)
def _no_regulation_map(monkeypatch):
    monkeypatch.setenv("GOVCHECK_REGULATIONS_MAPPING_PATH", "/nonexistent/retrieval_map.json")


def _pdf(name: str = "佐證.pdf") -> tuple:
    return ("supporting", (name, b"%PDF-1.4 fake", "application/pdf"))


def _form_file(field: str, path, name: str | None = None) -> tuple:
    return (field, (name or path.name, path.read_bytes(), "application/octet-stream"))


# ── /api/review：無須官方範本 ────────────────────────────────────────


def test_review_supporting_only_missing_core_forms():
    res = client.post("/api/review", files=[_pdf()])
    assert res.status_code == 200
    body = res.json()
    codes = {f["code"] for f in body["findings"]}
    assert "DOC.MISSING_F01" in codes
    assert "DOC.MISSING_F03" in codes
    assert body["decision_status"] == "changes_required"
    assert body["error_count"] > 0
    assert body["markdown"].startswith("#")
    assert "初步審查報告" in body["markdown"]


def test_review_finding_shape():
    body = client.post("/api/review", files=[_pdf()]).json()
    f = body["findings"][0]
    assert set(f) == {
        "id", "severity", "code", "title", "message",
        "location", "expected", "actual", "source",
    }
    assert f["severity"] in {"error", "warn", "info"}


def test_review_response_contract_keys():
    body = client.post("/api/review", files=[_pdf()]).json()
    assert set(body) == REPORT_KEYS
    # v1 的 passed 只看 error_count，會與 decision_status 互相矛盾；只給一個通過訊號。
    assert "passed" not in body
    # 無 F02 的送件包：風險欄位為 None（前端據此不渲染風險 tile）
    assert body["risk_score"] is None
    assert body["risk_grade"] is None


def test_review_returns_structured_metadata():
    res = client.post(
        "/api/review",
        data={"workflow_stage": "proposal"},
        files=[("supporting", ("evidence.txt", "佐證內容".encode(), "text/plain"))],
    )
    assert res.status_code == 200
    body = res.json()
    assert body["workflow_stage"] == "proposal"
    assert body["decision_status"] in {
        "approved", "changes_required", "manual_review", "incomplete"
    }
    assert body["document_statuses"][0]["status"] == "parsed"
    assert "source_units" not in body["document_statuses"][0]
    assert set(body["usage_summary"]) >= {
        "parser_vlm_calls", "review_llm_primary_calls", "cache_hits"
    }


def test_review_default_stage_is_proposal():
    body = client.post("/api/review", files=[_pdf()]).json()
    assert body["workflow_stage"] == "proposal"


def test_review_rejects_bad_stage():
    res = client.post("/api/review", data={"workflow_stage": "nope"}, files=[_pdf()])
    assert res.status_code == 422


def test_review_no_files_rejected():
    assert client.post("/api/review").status_code == 400


def test_review_unsafe_and_duplicate_filenames():
    # 惡意/重複檔名：basename 化擋逃逸、欄位前綴避免覆蓋；兩個同名佐證皆不應導致崩潰。
    files = [
        ("supporting", ("../../etc/passwd", b"%PDF-1.4 a", "application/pdf")),
        ("supporting", ("../../etc/passwd", b"%PDF-1.4 b", "application/pdf")),
    ]
    res = client.post("/api/review", files=files)
    assert res.status_code == 200
    assert "DOC.MISSING_F01" in {f["code"] for f in res.json()["findings"]}


def test_readiness_is_false_when_provider_disabled():
    body = client.get("/api/readiness").json()
    assert body["ready"] is False
    assert set(body["prompts"]) >= {"govcheck.f03.fairness", "govcheck.f03.escalation"}


# ── 靜態前端 ────────────────────────────────────────────────────────


def test_index_served():
    res = client.get("/")
    assert res.status_code == 200
    assert "AI 治理審查小幫手" in res.text
    # 地端不外送：首頁不得載入外部資源
    assert "googleapis.com" not in res.text
    assert "http://" not in res.text and "https://" not in res.text


# ── 須官方範本：真檔端到端 ────────────────────────────────────────────


@requires_templates
def test_review_full_bundle_passes(tmp_path):
    from tests.test_f02_rules import BASELINE
    f01 = build_f01_fixture(tmp_path / "f01.xlsx")
    f02 = build_f02_fixture(tmp_path / "f02.xlsm", answers=BASELINE, cached_grade="低")
    f03 = build_f03_fixture(tmp_path / "f03.xlsx")
    res = client.post("/api/review", files=[
        _form_file("f01", f01), _form_file("f02", f02), _form_file("f03", f03),
        _pdf("模型卡.pdf"),
    ])
    assert res.status_code == 200
    body = res.json()
    codes = {f["code"] for f in body["findings"]}
    assert "SUBMISSION.OK" in codes
    assert body["error_count"] == 0 and body["warn_count"] == 0
    assert body["subject"]  # F01 帶出受審對象
    # 含 F02 → 報告頂端風險欄位有值（前端據此渲染風險 tile）
    assert body["risk_grade"] in {"低", "中", "高"}
    assert isinstance(body["risk_score"], (int, float))
    assert "固有風險分級" in body["markdown"]


@requires_templates
def test_roster_covers_every_item_with_llm_off(tmp_path):
    """關掉 LLM 時名冊是總覽表唯一的完整項次來源，不可為空或漏項。"""
    f03 = build_f03_fixture(tmp_path / "f03.xlsx", items=[
        {"row": 5, "state": "不適用", "proposal": "本系統不涉及該情境"},
    ])
    res = client.post("/api/review", data={"workflow_stage": "proposal"}, files=[
        _form_file("f03", f03),
    ])
    assert res.status_code == 200
    body = res.json()
    roster = body["checklist_roster"]
    assert len(roster) > len(body["item_assessments"])
    first = roster[0]
    assert first["coverage"] == "not_applicable"
    assert first["reason"] == "本系統不涉及該情境"
    assert {row["coverage"] for row in roster} <= {"assessable", "not_applicable", "no_evidence"}
