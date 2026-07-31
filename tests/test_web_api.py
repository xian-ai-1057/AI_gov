"""Web API（FastAPI）契約測試。

分兩類：
  - 無須官方範本（PDF / 純路由錯誤）→ 恆可跑，無網路確定性基線。
  - 須官方範本（F01/F02/F03 真檔端到端）→ data/original 未連結時 skip。

LLM 一律不啟用（enable_llm 預設關，確定性）；介面層只驗證契約，不重造 ground truth。
法規對照表一律視為缺席，讓結果不受本機是否有 data/rag/retrieval_map.json 影響。
"""

from __future__ import annotations

import io
import json

import openpyxl
import pytest
from fastapi.testclient import TestClient

import govcheck.web.api as api_module
from govcheck.models import (
    Citation,
    F03ItemAssessment,
    Finding,
    ReviewReport,
    RubricLine,
    Severity,
)
from govcheck.parsers.f02_parser import ASSESS_SHEET
from govcheck.review.config import load_review_config
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
    "review_id", "verdict", "layers", "defects", "notices",
    "common_facts", "uniform_columns", "rubrics",
}

_REVIEW_CFG = load_review_config()
F01_SHEET = _REVIEW_CFG["f01"]["main_sheet"]
F03_SHEET = _REVIEW_CFG["f03"]["sheet"]


@pytest.fixture(autouse=True)
def _no_regulation_map(monkeypatch):
    monkeypatch.setenv("GOVCHECK_REGULATIONS_MAPPING_PATH", "/nonexistent/retrieval_map.json")


def _pdf(name: str = "佐證.pdf") -> tuple:
    return ("supporting", (name, b"%PDF-1.4 fake", "application/pdf"))


def _form_file(field: str, path, name: str | None = None) -> tuple:
    return (field, (name or path.name, path.read_bytes(), "application/octet-stream"))


def _batch_file(name: str, data: bytes, media_type: str = "application/octet-stream") -> tuple:
    return ("files", (name, data, media_type))


def _xlsx_bytes(*sheet_names: str) -> bytes:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for sheet_name in sheet_names:
        worksheet = workbook.create_sheet(sheet_name)
        worksheet["A1"] = "合成測試內容"
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _sse_events(response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


# ── /api/classify：批次分類預覽 ──────────────────────────────────────


def test_classify_rejects_empty_upload():
    res = client.post("/api/classify")

    assert res.status_code == 400


def test_classify_response_contract_and_input_order():
    res = client.post(
        "/api/classify",
        files=[
            _batch_file("看似F02.xlsx", _xlsx_bytes(F01_SHEET)),
            _batch_file("佐證.pdf", b"%PDF", "application/pdf"),
            _batch_file("其他.xlsx", _xlsx_bytes("Sheet1")),
        ],
    )

    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"kinds", "files"}
    assert [option["value"] for option in body["kinds"]] == [
        "f01",
        "f02",
        "f03",
        "supporting",
        "ignore",
    ]
    assert [item["filename"] for item in body["files"]] == [
        "看似F02.xlsx",
        "佐證.pdf",
        "其他.xlsx",
    ]
    assert [item["kind"] for item in body["files"]] == [
        "f01",
        "supporting",
        "ignore",
    ]
    assert [item["flag"] for item in body["files"]] == [None, None, "unknown"]
    assert all(
        set(item) == {"index", "filename", "kind", "reason", "flag"}
        for item in body["files"]
    )


def test_classify_flags_duplicate_core_form():
    f03 = _xlsx_bytes(F03_SHEET)

    body = client.post(
        "/api/classify",
        files=[
            _batch_file("first.xlsx", f03),
            _batch_file("second.xlsx", f03),
        ],
    ).json()

    assert [item["kind"] for item in body["files"]] == ["f03", "f03"]
    assert [item["flag"] for item in body["files"]] == [None, "dup"]


def test_classify_uses_f02_priority_for_ambiguous_workbook():
    body = client.post(
        "/api/classify",
        files=[
            _batch_file(
                "mixed.xlsx",
                _xlsx_bytes(F03_SHEET, F01_SHEET, ASSESS_SHEET),
            )
        ],
    ).json()

    assert body["files"][0]["kind"] == "f02"


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
    assert len(body["review_id"]) == 32
    assert body["verdict"]["code"] == "blocked"
    assert {layer["id"]: layer["state"] for layer in body["layers"]} == {
        "L1": "done",
        "L2": "skipped",
        "L3": "skipped",
    }
    assert isinstance(body["defects"], list)
    assert isinstance(body["notices"], list)
    assert isinstance(body["common_facts"], list)
    assert isinstance(body["uniform_columns"], list)
    assert body["rubrics"] == {}


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


def test_review_batch_supporting_only_uses_current_report_contract():
    files = [_batch_file("evidence.txt", "佐證內容".encode(), "text/plain")]
    res = client.post(
        "/api/review",
        data={
            "kinds": json.dumps(["supporting"]),
            "workflow_stage": "golive",
        },
        files=files,
    )

    assert res.status_code == 200
    body = res.json()
    assert set(body) == REPORT_KEYS
    assert body["workflow_stage"] == "golive"
    assert body["decision_status"] == "changes_required"
    assert "passed" not in body
    assert {
        "CLASSIFY.SUMMARY",
        "DOC.MISSING_F01",
        "DOC.MISSING_F03",
    } <= {finding["code"] for finding in body["findings"]}


def test_review_batch_allows_unknown_xlsx_to_be_confirmed_as_supporting():
    files = [_batch_file("佐證試算表.xlsx", _xlsx_bytes("測試結果"))]
    res = client.post(
        "/api/review",
        data={
            "kinds": json.dumps(["supporting"]),
        },
        files=files,
    )

    assert res.status_code == 200
    body = res.json()
    assert len(body["document_statuses"]) == 1
    assert body["document_statuses"][0]["status"] == "parsed"
    summary = next(
        finding for finding in body["findings"] if finding["code"] == "CLASSIFY.SUMMARY"
    )
    assert "佐證試算表.xlsx → 佐證文件" in summary["message"]


def test_review_batch_keeps_ignored_file_in_report_and_decision():
    files = [
        _batch_file("evidence.txt", "佐證內容".encode(), "text/plain"),
        _batch_file("unknown.xlsx", _xlsx_bytes("Sheet1")),
    ]
    res = client.post(
        "/api/review",
        data={
            "kinds": json.dumps(["supporting", "ignore"]),
        },
        files=files,
    )

    assert res.status_code == 200
    body = res.json()
    codes = {finding["code"] for finding in body["findings"]}
    assert "CLASSIFY.IGNORED" in codes
    assert body["decision_status"] == "changes_required"
    summary = next(finding for finding in body["findings"] if finding["code"] == "CLASSIFY.SUMMARY")
    assert "共 2 個檔案" in summary["message"]
    assert "unknown.xlsx → 忽略（未納入審查）" in summary["message"]


def test_review_batch_duplicate_form_returns_classification_finding():
    files = [
        _batch_file("first.xlsm", b"not excel"),
        _batch_file("second.xlsm", b"not excel"),
    ]
    res = client.post(
        "/api/review",
        data={
            "kinds": json.dumps(["f02", "f02"]),
        },
        files=files,
    )

    assert res.status_code == 200
    body = res.json()
    codes = {finding["code"] for finding in body["findings"]}
    assert "CLASSIFY.SUMMARY" in codes
    assert "CLASSIFY.DUPLICATE_F02" in codes
    assert body["decision_status"] == "changes_required"
    assert body["warn_count"] >= 1


def test_review_rejects_mixed_batch_and_routed_uploads():
    res = client.post(
        "/api/review",
        data={"kinds": json.dumps(["supporting"])},
        files=[
            _batch_file("batch.pdf", b"%PDF", "application/pdf"),
            _pdf("routed.pdf"),
        ],
    )

    assert res.status_code == 400
    assert "不可混用" in res.json()["detail"]


def test_review_batch_requires_kinds():
    res = client.post(
        "/api/review",
        files=[_batch_file("evidence.pdf", b"%PDF", "application/pdf")],
    )

    assert res.status_code == 400
    assert "kinds" in res.json()["detail"]


@pytest.mark.parametrize("kinds", ["not json", json.dumps("supporting")])
def test_review_batch_rejects_malformed_or_non_array_kinds(kinds):
    res = client.post(
        "/api/review",
        data={"kinds": kinds},
        files=[_batch_file("evidence.pdf", b"%PDF", "application/pdf")],
    )

    assert res.status_code == 400
    assert "kinds" in res.json()["detail"]


def test_review_batch_rejects_kinds_length_mismatch():
    res = client.post(
        "/api/review",
        data={"kinds": json.dumps(["supporting", "ignore"])},
        files=[_batch_file("evidence.pdf", b"%PDF", "application/pdf")],
    )

    assert res.status_code == 400
    assert "數量" in res.json()["detail"]


@pytest.mark.parametrize("kind", ["unknown", "bogus", 123, None])
def test_review_batch_rejects_illegal_kind(kind):
    res = client.post(
        "/api/review",
        data={"kinds": json.dumps([kind])},
        files=[_batch_file("evidence.pdf", b"%PDF", "application/pdf")],
    )

    assert res.status_code == 400
    assert "判定" in res.json()["detail"]


def test_review_batch_rejects_all_ignored():
    files = [
        _batch_file("one.xlsx", _xlsx_bytes("Sheet1")),
        _batch_file("two.xlsx", _xlsx_bytes("Sheet2")),
    ]
    res = client.post(
        "/api/review",
        data={
            "kinds": json.dumps(["ignore", "ignore"]),
        },
        files=files,
    )

    assert res.status_code == 400
    assert "忽略" in res.json()["detail"]


def test_classify_enforces_single_file_size_before_classification(monkeypatch):
    monkeypatch.setattr(api_module, "MAX_UPLOAD_BYTES", 4)

    res = client.post(
        "/api/classify",
        files=[_batch_file("large.pdf", b"12345", "application/pdf")],
    )

    assert res.status_code == 413
    assert "large.pdf" in res.json()["detail"]


def test_classify_enforces_batch_total_size(monkeypatch):
    monkeypatch.setattr(api_module, "MAX_BATCH_UPLOAD_BYTES", 5)

    res = client.post(
        "/api/classify",
        files=[
            _batch_file("one.pdf", b"123", "application/pdf"),
            _batch_file("two.pdf", b"456", "application/pdf"),
        ],
    )

    assert res.status_code == 413
    assert "整批" in res.json()["detail"]


def test_review_routed_mode_rejects_batch_kinds():
    res = client.post(
        "/api/review",
        data={"kinds": json.dumps(["supporting"])},
        files=[_pdf()],
    )

    assert res.status_code == 400
    assert "分欄" in res.json()["detail"]


def test_review_unsafe_and_duplicate_filenames():
    # 惡意/重複檔名：basename 化擋逃逸、欄位前綴避免覆蓋；兩個同名佐證皆不應導致崩潰。
    files = [
        ("supporting", ("../../etc/passwd", b"%PDF-1.4 a", "application/pdf")),
        ("supporting", ("../../etc/passwd", b"%PDF-1.4 b", "application/pdf")),
    ]
    res = client.post("/api/review", files=files)
    assert res.status_code == 200
    assert "DOC.MISSING_F01" in {f["code"] for f in res.json()["findings"]}


# ── 報告頁 v2 投影／SSE／覆核決定 ──────────────────────────────────────


def test_report_projection_builds_rubric_quote_locator_and_item_codes():
    report = ReviewReport(
        form_type="送件包",
        review_id="a" * 32,
        findings=[Finding(
            severity=Severity.WARN,
            code="F03.ASSESSMENT_INCOMPLETE",
            title="內容不完整",
            message="請補述",
            location="項次 4-2（系統穩健性與安全性）",
            source="llm",
        )],
        item_assessments=[F03ItemAssessment(
            item_id="4-2",
            profile="security",
            rubric=[
                RubricLine(text="有指名監控指標", met=True),
                RubricLine(text="有寫檢視頻率", met=False),
            ],
            citations=[Citation(
                document_id="doc-safe-id",
                unit_id="doc-safe-id:12",
                locator_type="page",
                locator_value="12",
                quote="已建立監控指標。",
            )],
        )],
    )

    rubric = api_module._report_dict(report)["rubrics"]["4-2"]
    assert rubric["lines"] == [
        {"text": "有指名監控指標", "met": True},
        {"text": "有寫檢視頻率", "met": False},
    ]
    assert rubric["quote"] == "已建立監控指標。"
    assert rubric["quote_locator"] == {
        "file": "doc-safe-id",
        "sheet": None,
        "cell": None,
        "page": 12,
        "section": None,
    }
    assert rubric["codes"] == ["F03.ASSESSMENT_INCOMPLETE"]


def test_review_stream_sends_partial_before_final_with_serialized_reports():
    res = client.post("/api/review/stream", files=[_pdf()])

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(res)
    kinds = [event["kind"] for event in events]
    partial_index = kinds.index("partial")
    final_index = kinds.index("final")
    assert partial_index < final_index
    partial = events[partial_index]["report"]
    final = events[final_index]["report"]
    assert set(partial) == REPORT_KEYS
    assert set(final) == REPORT_KEYS
    assert partial["review_id"] == final["review_id"]
    assert isinstance(partial["defects"], list)
    assert not any(
        event["kind"] == "final" and "report" not in event
        for event in events
    )


def test_review_stream_preserves_batch_upload_mode():
    res = client.post(
        "/api/review/stream",
        data={"kinds": json.dumps(["supporting"])},
        files=[_batch_file("evidence.txt", "佐證內容".encode(), "text/plain")],
    )

    assert res.status_code == 200
    events = _sse_events(res)
    final = next(event for event in events if event["kind"] == "final")
    assert "CLASSIFY.SUMMARY" in {
        finding["code"] for finding in final["report"]["findings"]
    }


def test_review_stream_reports_execution_error_as_frame(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("sensitive details must not escape")

    monkeypatch.setattr(api_module, "review_submission", _boom)
    res = client.post("/api/review/stream", files=[_pdf()])

    assert res.status_code == 200
    events = _sse_events(res)
    assert events == [{"kind": "error", "status": 400, "detail": "解析或審查失敗：RuntimeError"}]
    assert "sensitive details" not in res.text


def test_decisions_api_round_trip_and_missing_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("GOVCHECK_DECISION_DIR", str(tmp_path / "decisions"))
    review_id = "a" * 32

    missing = client.get(f"/api/review/{review_id}/decisions")
    assert missing.status_code == 200
    assert missing.json() == {"review_id": review_id, "decisions": {}}

    saved = client.post(
        f"/api/review/{review_id}/decisions",
        json={
            "decisions": {"1-1": "accept", "4-2": "reject", "5-1": "pending"},
            "subject": "測試案件",
            "note": "已覆核",
        },
    )
    assert saved.status_code == 200
    assert saved.json()["decisions"] == {
        "1-1": "accept",
        "4-2": "reject",
        "5-1": "pending",
    }
    loaded = client.get(f"/api/review/{review_id}/decisions")
    assert loaded.status_code == 200
    assert loaded.json()["decisions"] == saved.json()["decisions"]
    assert loaded.json()["subject"] == "測試案件"
    assert loaded.json()["note"] == "已覆核"


@pytest.mark.parametrize("method", ["get", "post"])
def test_decisions_api_rejects_invalid_review_id(method):
    request = getattr(client, method)
    kwargs = {"json": {"decisions": {}}} if method == "post" else {}
    res = request(f"/api/review/{'A' * 32}/decisions", **kwargs)
    assert res.status_code == 400


def test_decisions_api_rejects_invalid_value(tmp_path, monkeypatch):
    monkeypatch.setenv("GOVCHECK_DECISION_DIR", str(tmp_path / "decisions"))
    res = client.post(
        f"/api/review/{'a' * 32}/decisions",
        json={"decisions": {"4-2": "approved"}},
    )
    assert res.status_code == 400
    assert "未知的覆核決定" in res.json()["detail"]


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


def test_frontend_includes_batch_classification_flow():
    script = client.get("/app.js")

    assert script.status_code == 200
    assert 'postForm("/api/classify"' in script.text
    assert 'fd.append("kinds", JSON.stringify(confirmed))' in script.text
    assert "renderStep2" in script.text
    assert "currentClassifications" in script.text
    assert "invalidateReport" in script.text
    assert 'data-action="set-kind"' in script.text


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
def test_review_batch_full_bundle_contract_and_decision_status(tmp_path):
    from tests.test_f02_rules import BASELINE

    f01 = build_f01_fixture(tmp_path / "f01.xlsx")
    f02 = build_f02_fixture(tmp_path / "f02.xlsm", answers=BASELINE, cached_grade="低")
    f03 = build_f03_fixture(tmp_path / "f03.xlsx")
    files = [
        _form_file("files", f03),
        _form_file("files", f01),
        _form_file("files", f02),
    ]
    res = client.post(
        "/api/review",
        data={
            "kinds": json.dumps(["f03", "f01", "f02"]),
            "workflow_stage": "proposal",
        },
        files=files,
    )

    assert res.status_code == 200
    body = res.json()
    assert set(body) == REPORT_KEYS
    assert body["error_count"] == 0
    assert body["warn_count"] == 0
    assert body["decision_status"] == "manual_review"
    assert {"CLASSIFY.SUMMARY", "SUBMISSION.OK"} <= {
        finding["code"] for finding in body["findings"]
    }


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


@requires_templates
def test_roster_payload_carries_source_text_and_stage_diff(tmp_path):
    """報告要能顯示原文供人工對照：名冊的原文與差異欄必須真的送到前端。"""
    f03 = build_f03_fixture(tmp_path / "f03.xlsx", items=[
        {"row": 5, "state": "是", "proposal": "提案時的做法", "golive": "上線後改用新做法"},
    ])

    def _roster_first(stage: str) -> dict:
        res = client.post("/api/review", data={"workflow_stage": stage}, files=[
            _form_file("f03", f03),
        ])
        assert res.status_code == 200
        return res.json()["checklist_roster"][0]

    golive = _roster_first("golive")
    assert golive["evidence_proposal_text"] == "提案時的做法"
    assert golive["evidence_golive_text"] == "上線後改用新做法"
    assert golive["golive_diff"] == "changed"
    assert golive["description"]

    # 提案階段不看 J 欄：帶出上線佐證原文會讓人以為 AI 已審過那段。
    proposal = _roster_first("proposal")
    assert proposal["evidence_proposal_text"] == "提案時的做法"
    assert proposal["evidence_golive_text"] is None
    assert proposal["golive_diff"] is None
