"""Web API（FastAPI）契約測試。

分兩類（比照 test_classify.py）：
  - 無須官方範本（PDF / 空 xlsx / 純路由錯誤）→ 恆可跑，無網路確定性基線。
  - 須官方範本（F01/F02/F03 真檔端到端 + 重複偵測）→ data/original 未連結時 skip。

LLM 一律不啟用（enable_llm 預設關，確定性）；介面層只驗證契約，不重造 ground truth。
"""

from __future__ import annotations

import io
import json

import openpyxl
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


def _pdf(name: str = "佐證.pdf") -> tuple:
    return ("files", (name, b"%PDF-1.4 fake", "application/pdf"))


def _blank_xlsx(name: str = "其他.xlsx") -> tuple:
    buf = io.BytesIO()
    openpyxl.Workbook().save(buf)
    return ("files", (name, buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))


def _path_file(path, name: str | None = None) -> tuple:
    data = path.read_bytes()
    return ("files", (name or path.name, data, "application/octet-stream"))


# ── /api/classify：無須官方範本 ──────────────────────────────────────


def test_classify_pdf_is_supporting():
    res = client.post("/api/classify", files=[_pdf()])
    assert res.status_code == 200
    body = res.json()
    assert {o["value"] for o in body["kinds"]} == {"f01", "f02", "f03", "supporting", "ignore"}
    f = body["files"][0]
    assert f["kind"] == "supporting"
    assert f["flag"] is None
    assert "佐證" in f["reason"]


def test_classify_blank_xlsx_is_unknown_flag():
    res = client.post("/api/classify", files=[_blank_xlsx()])
    assert res.status_code == 200
    f = res.json()["files"][0]
    # 無法辨識 → 旗標 unknown，預設判定「忽略此檔」，逼使用者人工指定
    assert f["flag"] == "unknown"
    assert f["kind"] == "ignore"


def test_classify_preserves_index_and_order():
    res = client.post("/api/classify", files=[_pdf("a.pdf"), _blank_xlsx("b.xlsx"), _pdf("c.docx")])
    files = res.json()["files"]
    assert [f["index"] for f in files] == [0, 1, 2]
    assert [f["filename"] for f in files] == ["a.pdf", "b.xlsx", "c.docx"]


def test_classify_no_files_rejected():
    res = client.post("/api/classify", files=[])
    assert res.status_code in (400, 422)  # 無檔案 → 友善錯誤 / 驗證錯誤


# ── /api/review：無須官方範本 ────────────────────────────────────────


def test_review_supporting_only_missing_core_forms():
    res = client.post(
        "/api/review",
        data={"kinds": json.dumps(["supporting"])},
        files=[_pdf()],
    )
    assert res.status_code == 200
    body = res.json()
    codes = {f["code"] for f in body["findings"]}
    assert "DOC.MISSING_F01" in codes
    assert "DOC.MISSING_F03" in codes
    assert "CLASSIFY.SUMMARY" in codes
    assert body["passed"] is False
    assert body["error_count"] > 0
    # info_count 由端點計算，至少含 CLASSIFY.SUMMARY
    assert body["info_count"] >= 1
    assert body["markdown"].startswith("#")
    assert "初步審查報告" in body["markdown"]


def test_review_finding_shape():
    body = client.post(
        "/api/review", data={"kinds": json.dumps(["supporting"])}, files=[_pdf()]
    ).json()
    f = body["findings"][0]
    assert set(f) == {"id", "severity", "code", "title", "message", "location", "expected", "actual", "source"}
    assert f["severity"] in {"error", "warn", "info"}


def test_review_response_contract_keys():
    # 前端會直接讀這些頂層鍵；少任一個都會造成 UI 顯示 undefined（鎖定契約）。
    body = client.post(
        "/api/review", data={"kinds": json.dumps(["supporting"])}, files=[_pdf()]
    ).json()
    assert set(body) == {
        "form_type", "subject", "passed",
        "error_count", "warn_count", "info_count", "findings", "markdown",
    }


def test_review_kinds_not_list_rejected():
    # 合法 JSON 但非陣列（如 "null"）須回 400，不可變成未處理的 500。
    res = client.post("/api/review", data={"kinds": "null"}, files=[_pdf()])
    assert res.status_code == 400


def test_review_unsafe_and_duplicate_filenames():
    # 惡意/重複檔名：basename 化擋逃逸、索引前綴避免覆蓋；兩個同名佐證皆不應導致崩潰。
    files = [
        ("files", ("../../etc/passwd", b"%PDF-1.4 a", "application/pdf")),
        ("files", ("../../etc/passwd", b"%PDF-1.4 b", "application/pdf")),
    ]
    res = client.post(
        "/api/review", data={"kinds": json.dumps(["supporting", "supporting"])}, files=files
    )
    assert res.status_code == 200
    # 缺核心表單 → 仍正常出報告，未發生路徑逃逸或覆蓋錯誤
    assert "DOC.MISSING_F01" in {f["code"] for f in res.json()["findings"]}


def test_review_all_ignored_rejected():
    res = client.post("/api/review", data={"kinds": json.dumps(["ignore"])}, files=[_pdf()])
    assert res.status_code == 400
    assert "忽略" in res.json()["detail"]


def test_review_kinds_length_mismatch_rejected():
    res = client.post(
        "/api/review",
        data={"kinds": json.dumps(["supporting", "supporting"])},
        files=[_pdf()],
    )
    assert res.status_code == 400


def test_review_bad_kinds_json_rejected():
    res = client.post("/api/review", data={"kinds": "not-json"}, files=[_pdf()])
    assert res.status_code == 400


# ── /api/review/stream：SSE 逐階段進度（無須官方範本）────────────────────


def _sse_events(res) -> list[dict]:
    """把 text/event-stream 回應切成事件 dict 清單。"""
    return [json.loads(line[5:].strip()) for line in res.text.splitlines() if line.startswith("data:")]


def test_review_stream_emits_progress_then_done():
    res = client.post(
        "/api/review/stream", data={"kinds": json.dumps(["supporting"])}, files=[_pdf()]
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(res)
    stages = [e["stage"] for e in events]
    assert "upload" in stages and "rules" in stages       # 至少含落地與規則階段進度
    assert stages[-1] == "done"                            # 最後一筆為完成事件
    # done 事件帶完整報告，且頂層契約與 /api/review 一致（前端直接讀這些鍵）
    report = events[-1]["report"]
    assert set(report) == {
        "form_type", "subject", "passed",
        "error_count", "warn_count", "info_count", "findings", "markdown",
    }
    assert "DOC.MISSING_F01" in {f["code"] for f in report["findings"]}


def test_review_stream_progress_monotonic_within_stage():
    res = client.post(
        "/api/review/stream", data={"kinds": json.dumps(["supporting"])}, files=[_pdf()]
    )
    rules = [e for e in _sse_events(res) if e["stage"] == "rules"]
    assert [e["done"] for e in rules] == sorted(e["done"] for e in rules)  # done 不回退
    assert all(0 < e["done"] <= e["total"] for e in rules)


def test_review_stream_matches_review_findings():
    """串流端點與舊端點對同輸入應產出相同 findings（契約不漂移）。"""
    payload = {"data": {"kinds": json.dumps(["supporting"])}, "files": [_pdf()]}
    plain = client.post("/api/review", **payload).json()
    stream_report = _sse_events(client.post("/api/review/stream", **payload))[-1]["report"]
    assert {f["code"] for f in plain["findings"]} == {f["code"] for f in stream_report["findings"]}
    assert plain["passed"] == stream_report["passed"]


def test_review_stream_validation_errors_are_http_status():
    # 入參錯誤一律在串流開始前回正規 HTTP 4xx（而非藏在 SSE error 事件裡），與 /api/review 對齊
    assert client.post("/api/review/stream", files=[]).status_code in (400, 422)
    assert client.post(
        "/api/review/stream", data={"kinds": "not-json"}, files=[_pdf()]
    ).status_code == 400
    assert client.post(
        "/api/review/stream", data={"kinds": json.dumps(["supporting", "supporting"])}, files=[_pdf()]
    ).status_code == 400
    # 全部「忽略此檔」→ 前置 400（與 /api/review 同契約），不進串流
    res = client.post("/api/review/stream", data={"kinds": json.dumps(["ignore"])}, files=[_pdf()])
    assert res.status_code == 400 and "忽略" in res.json()["detail"]
    # 不可指派的判定（如 unknown）→ 前置 400
    assert client.post(
        "/api/review/stream", data={"kinds": json.dumps(["unknown"])}, files=[_pdf()]
    ).status_code == 400


# ── 靜態前端 ────────────────────────────────────────────────────────


def test_index_served():
    res = client.get("/")
    assert res.status_code == 200
    assert "AI 治理審查小幫手" in res.text
    # 地端不外送：首頁不得載入外部資源
    assert "googleapis.com" not in res.text
    assert "http://" not in res.text and "https://" not in res.text


# ── 須官方範本：真檔端到端 + 重複偵測 ──────────────────────────────────


@requires_templates
def test_classify_duplicate_f02_flag(tmp_path):
    from tests.test_f02_rules import BASELINE
    a = build_f02_fixture(tmp_path / "f02_a.xlsm", answers=BASELINE, cached_grade="低")
    b = build_f02_fixture(tmp_path / "f02_b.xlsm", answers=BASELINE, cached_grade="低")
    res = client.post("/api/classify", files=[_path_file(a), _path_file(b)])
    files = res.json()["files"]
    assert files[0]["kind"] == "f02" and files[0]["flag"] is None
    assert files[1]["flag"] == "dup"  # 第二份 F02 標記重複


@requires_templates
def test_review_full_bundle_passes(tmp_path):
    from tests.test_f02_rules import BASELINE
    f01 = build_f01_fixture(tmp_path / "f01.xlsx")
    f02 = build_f02_fixture(tmp_path / "f02.xlsm", answers=BASELINE, cached_grade="低")
    f03 = build_f03_fixture(tmp_path / "f03.xlsx")
    pdf = _pdf("模型卡.pdf")
    res = client.post(
        "/api/review",
        data={"kinds": json.dumps(["f01", "f02", "f03", "supporting"])},
        files=[_path_file(f01), _path_file(f02), _path_file(f03), pdf],
    )
    assert res.status_code == 200
    body = res.json()
    codes = {f["code"] for f in body["findings"]}
    assert "SUBMISSION.OK" in codes
    assert body["passed"] is True
    assert body["error_count"] == 0 and body["warn_count"] == 0
    assert body["subject"]  # F01 帶出受審對象


@requires_templates
def test_review_stream_full_bundle_emits_parse_and_passes(tmp_path):
    from tests.test_f02_rules import BASELINE
    f01 = build_f01_fixture(tmp_path / "f01.xlsx")
    f02 = build_f02_fixture(tmp_path / "f02.xlsm", answers=BASELINE, cached_grade="低")
    f03 = build_f03_fixture(tmp_path / "f03.xlsx")
    res = client.post(
        "/api/review/stream",
        data={"kinds": json.dumps(["f01", "f02", "f03", "supporting"])},
        files=[_path_file(f01), _path_file(f02), _path_file(f03), _pdf("模型卡.pdf")],
    )
    assert res.status_code == 200
    events = _sse_events(res)
    parse = [e for e in events if e["stage"] == "parse"]
    assert [e["done"] for e in parse] == [1, 2, 3]   # F01/F02/F03 三表逐一解析
    report = events[-1]["report"]
    assert "SUBMISSION.OK" in {f["code"] for f in report["findings"]} and report["passed"] is True
