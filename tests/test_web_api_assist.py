"""提案單位端 AI 協填（/api/assist/suggest、/assist）與 /api/readiness 擴充的介面契約測試。

介面層只驗證契約：路由錯誤、payload 鍵集合、靜態頁零外部資源、readiness 區塊。
引擎行為由 tests/test_assist_engine.py 覆蓋；這裡以 monkeypatch 替換 `suggest_prefill`，
FileProcess 一律用 conftest 的 `fake_fileprocess`，不打真服務、不打真 LLM。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

import govcheck.web.api as api_module
from govcheck.models import (
    Citation,
    DocumentArtifact,
    DocumentParseStatus,
    Finding,
    PromptRunRef,
    Severity,
    UsageSummary,
)
from govcheck.web.api import app

try:  # P3 交付前以同形狀的本地模型替代，契約測試不因套件缺席而空轉
    from govcheck.assist import AssistResult, Suggestion
except ImportError:  # pragma: no cover - 只在 assist 套件尚未落地時走到
    class Suggestion(BaseModel):  # type: ignore[no-redef]
        form: Literal["f01", "f02", "f03"]
        field_id: str
        label: str
        cell: str
        kind: Literal["text", "yes_no"]
        covered: bool
        suggestion: str | None
        confidence: float
        citations: list[Citation] = Field(default_factory=list)
        rationale: str = ""

    class AssistResult(BaseModel):  # type: ignore[no-redef]
        assist_id: str
        forms: list[str]
        suggestions: list[Suggestion]
        document_statuses: list[DocumentArtifact] = Field(default_factory=list)
        notices: list[Finding] = Field(default_factory=list)
        usage: UsageSummary = Field(default_factory=UsageSummary)
        prompt_refs: list[PromptRunRef] = Field(default_factory=list)
        mode: Literal["llm", "retrieval_only", "unavailable"] = "retrieval_only"


ROOT = Path(__file__).resolve().parents[1]
FIELDS_FIXTURE = ROOT / "tests" / "fixtures" / "form_fields_minimal.json"

client = TestClient(app)

# 前端直接讀這些鍵；少任一個 UI 就顯示 undefined。
ASSIST_KEYS = {
    "assist_id", "forms", "mode", "suggestions", "documents",
    "notices", "usage", "prompt_refs", "markdown",
}


@pytest.fixture(autouse=True)
def _no_regulation_map(monkeypatch):
    monkeypatch.setenv("GOVCHECK_REGULATIONS_MAPPING_PATH", "/nonexistent/retrieval_map.json")


def _txt(name: str = "簽呈.txt", text: str = "系統名稱：客服語音機器人") -> tuple:
    return ("files", (name, text.encode("utf-8"), "text/plain"))


def _sample_result() -> AssistResult:
    citation = Citation(
        document_id="doc-1", unit_id="doc-1:0", excerpt_id="ex-1",
        locator_type="page", locator_value="2", quote="系統名稱：客服語音機器人",
    )
    return AssistResult(
        assist_id="assist-test-1",
        forms=["f01", "f03"],
        suggestions=[
            Suggestion(
                form="f01", field_id="f01.system_name", label="系統名稱", cell="A4",
                kind="text", covered=True, suggestion="客服語音機器人", confidence=0.9,
                citations=[citation], rationale="簽呈第 2 頁載明系統名稱。",
            ),
            Suggestion(
                form="f03", field_id="f03.1-1", label="1-1 管理議題", cell="I5",
                kind="text", covered=False, suggestion=None, confidence=0.0,
                citations=[], rationale="",
            ),
        ],
        document_statuses=[
            DocumentArtifact(
                document_id="doc-1", filename_hash="h", media_type="text/plain",
                parser_config_hash="c", status=DocumentParseStatus.PARSED,
            ),
        ],
        notices=[
            Finding(
                severity=Severity.INFO, code="ASSIST.LLM_DISABLED",
                title="語意協填未啟用", message="只列出候選出處。", source="llm",
            ),
        ],
        usage=UsageSummary(),
        prompt_refs=[],
        mode="retrieval_only",
    )


def _install_fake_engine(monkeypatch, result: AssistResult, calls: list | None = None):
    def fake_suggest(paths, *, forms, enable_llm, progress=None):
        if calls is not None:
            calls.append({"paths": list(paths), "forms": list(forms), "enable_llm": enable_llm})
        return result

    monkeypatch.setattr(api_module, "suggest_prefill", fake_suggest)
    monkeypatch.setattr(api_module, "assist_to_markdown", lambda r: f"# 填寫建議書 {r.assist_id}")


# ── 路由錯誤 ───────────────────────────────────────────────────────


def test_assist_requires_at_least_one_file():
    res = client.post("/api/assist/suggest", data={"forms": "f01"})
    assert res.status_code == 400
    assert "佐證" in res.json()["detail"]


def test_assist_rejects_unknown_form():
    res = client.post("/api/assist/suggest", files=[_txt()], data={"forms": "f09"})
    assert res.status_code == 400
    assert "f09" in res.json()["detail"]


def test_assist_rejects_empty_forms_list():
    res = client.post("/api/assist/suggest", files=[_txt()], data={"forms": " , "})
    assert res.status_code == 400


# ── 契約：happy path（引擎以 monkeypatch 替換）───────────────────────


def test_assist_payload_contract(monkeypatch):
    calls: list = []
    _install_fake_engine(monkeypatch, _sample_result(), calls)

    res = client.post(
        "/api/assist/suggest",
        files=[_txt(), _txt("簡報.txt", "供應商：內部自建")],
        data={"forms": "f03,f01", "enable_llm": "false"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == ASSIST_KEYS
    assert body["assist_id"] == "assist-test-1"
    assert body["forms"] == ["f01", "f03"]
    assert body["mode"] == "retrieval_only"
    assert body["markdown"].startswith("# 填寫建議書")

    # 引擎收到兩個落地檔（sup0/sup1 前綴）、表單依固定順序去重、LLM 開關照參數
    assert len(calls) == 1
    assert calls[0]["forms"] == ["f01", "f03"]
    assert calls[0]["enable_llm"] is False
    assert [Path(p).name[:4] for p in calls[0]["paths"]] == ["sup0", "sup1"]

    covered, uncovered = body["suggestions"]
    assert covered["covered"] is True and covered["suggestion"] == "客服語音機器人"
    # 引用投影成 locator dict：file 只回 document_id、page 轉 int、附引文
    cite = covered["citations"][0]
    assert cite["file"] == "doc-1" and cite["page"] == 2 and cite["section"] is None
    assert cite["quote"] == "系統名稱：客服語音機器人"
    assert cite["unit_id"] == "doc-1:0" and cite["excerpt_id"] == "ex-1"
    assert uncovered["covered"] is False and uncovered["citations"] == []

    assert body["documents"][0]["document_id"] == "doc-1"
    assert body["documents"][0]["status"] == "parsed"
    assert "source_units" not in body["documents"][0]
    assert body["notices"][0]["code"] == "ASSIST.LLM_DISABLED"
    assert body["notices"][0]["severity"] == "info"
    assert "input_tokens" in body["usage"]
    assert body["prompt_refs"] == []


def test_assist_default_forms_and_llm_flag(monkeypatch):
    calls: list = []
    _install_fake_engine(monkeypatch, _sample_result(), calls)
    res = client.post("/api/assist/suggest", files=[_txt()])
    assert res.status_code == 200
    assert calls[0]["forms"] == ["f01", "f02", "f03"]
    assert calls[0]["enable_llm"] is False  # conftest 清 env → 設定檔預設關


def test_assist_engine_exception_becomes_400(monkeypatch):
    def boom(paths, *, forms, enable_llm, progress=None):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(api_module, "suggest_prefill", boom)
    monkeypatch.setattr(api_module, "assist_to_markdown", lambda r: "")
    res = client.post("/api/assist/suggest", files=[_txt()])
    assert res.status_code == 400
    assert res.json()["detail"] == "解析或建議失敗：RuntimeError"


def test_assist_module_missing_returns_503(monkeypatch):
    monkeypatch.setattr(api_module, "suggest_prefill", None)
    res = client.post("/api/assist/suggest", files=[_txt()])
    assert res.status_code == 503


# ── 端到端（真引擎 + 假 FileProcess + 欄位目錄 fixture；LLM 關）──────


@pytest.mark.skipif(
    api_module.suggest_prefill is None or not FIELDS_FIXTURE.exists(),
    reason="assist 引擎或 tests/fixtures/form_fields_minimal.json 尚未落地",
)
def test_assist_end_to_end_retrieval_only(monkeypatch, fake_fileprocess):
    monkeypatch.setenv("GOVCHECK_FORM_FIELDS_PATH", str(FIELDS_FIXTURE))
    res = client.post(
        "/api/assist/suggest",
        files=[_txt("簽呈.txt", "系統名稱：客服語音機器人。負責單位：數位金融處。")],
        data={"forms": "f01", "enable_llm": "false"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == ASSIST_KEYS
    assert body["mode"] == "retrieval_only"
    assert "ASSIST.LLM_DISABLED" in {n["code"] for n in body["notices"]}
    assert body["documents"] and body["documents"][0]["status"] == "parsed"
    assert all(row["covered"] is False for row in body["suggestions"])
    assert "填寫建議書" in body["markdown"]
    # 只送 opaque 檔名，原始檔名不外傳
    assert all("簽呈" not in call.get("filename", "") for call in fake_fileprocess.calls)


# ── 靜態頁 ─────────────────────────────────────────────────────────


def test_assist_page_served():
    res = client.get("/assist")
    assert res.status_code == 200
    assert "AI 協填" in res.text
    assert "assist.js" in res.text
    # 地端不外送：頁面不得載入外部資源
    assert "http://" not in res.text and "https://" not in res.text


def test_assist_script_served_without_external_resources():
    script = client.get("/assist.js")
    assert script.status_code == 200
    assert "填寫建議書" in script.text
    assert 'postForm("/api/assist/suggest"' in script.text
    assert "文件未涵蓋，請自行填寫" in script.text
    assert "govcheck.assist.adopted." in script.text
    assert "http://" not in script.text and "https://" not in script.text


def test_index_links_to_assist_page():
    script = client.get("/app.js")
    assert script.status_code == 200
    assert 'href="/assist"' in script.text


# ── readiness 擴充 ─────────────────────────────────────────────────


def test_readiness_has_parser_and_assist_blocks(monkeypatch, tmp_path):
    # 本機可能已有 data/rag/form_fields.json；明確指向不存在的檔，讓「缺檔」語意不依賴本機資產
    monkeypatch.setenv("GOVCHECK_FORM_FIELDS_PATH", str(tmp_path / "missing_form_fields.json"))
    monkeypatch.setattr(
        api_module, "fileprocess_readiness",
        lambda settings=None: {"backend": "fileprocess", "url_host_approved": True, "reachable": False},
    )
    body = client.get("/api/readiness").json()
    # 既有鍵與語意不變：ready 仍是 F03 判讀就緒
    assert body["ready"] is False
    assert set(body["prompts"]) >= {"govcheck.f03.fairness", "govcheck.f03.escalation"}
    # F03 就緒不受 assist prompt 影響：prompts 只含 f03 feature
    assert "govcheck.assist.prefill" not in body["prompts"]

    assert body["parser"] == {"backend": "fileprocess", "url_host_approved": True, "reachable": False}
    assist = body["assist"]
    assert set(assist) == {"prompt", "fields_available", "ready"}
    assert assist["ready"] is False
    assert assist["fields_available"] is False  # 指向不存在的檔 → 缺檔
    assert isinstance(assist["prompt"], str)


def test_readiness_survives_parser_probe_failure(monkeypatch):
    def broken(settings=None):
        raise RuntimeError("probe failed")

    monkeypatch.setattr(api_module, "fileprocess_readiness", broken)
    body = client.get("/api/readiness").json()
    assert body["parser"]["reachable"] is False
    assert body["parser"]["backend"] == "fileprocess"


@pytest.mark.skipif(not FIELDS_FIXTURE.exists(), reason="form_fields_minimal.json 尚未落地")
def test_readiness_reports_fields_available(monkeypatch, fake_fileprocess):
    monkeypatch.setenv("GOVCHECK_FORM_FIELDS_PATH", str(FIELDS_FIXTURE))
    body = client.get("/api/readiness").json()
    assert body["assist"]["fields_available"] is True
