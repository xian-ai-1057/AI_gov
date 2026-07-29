"""FastAPI 後端：一個審查 API + 一個就緒查詢 + 靜態前端掛載。

對應前端兩步驟：
  1) 上傳送件包（F01 / F02 / F03 各一份 + 佐證多份）
  2) 審查報告 → POST /api/review（temp 用後即刪 → 結構化報告）

介面層零判讀邏輯：只負責落地檔案、呼叫 review_submission、把 ReviewReport 轉成 JSON。
暫存檔於 TemporaryDirectory 結束時一併清除（最小足跡、地端不外送）。
"""

from __future__ import annotations

import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from govcheck.llm.client import ChatClient
from govcheck.llm.config import load_llm_config
from govcheck.logging_setup import get_logger, new_request_id, set_request_id, setup_logging
from govcheck.models import ReviewReport, Severity, WorkflowStage
from govcheck.prompts import build_prompt_gateway
from govcheck.report.builder import to_markdown
from govcheck.review.engine import review_submission

log = get_logger("api")

# 前端靜態檔位於 repo 根的 web/（src/govcheck/web/api.py → parents[3] = repo root）
WEB_DIR = Path(__file__).resolve().parents[3] / "web"

# 單檔上限（50 MB）：防止記憶體耗盡或磁碟塞滿
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # 在伺服器啟動時才讀 .env / 設定 log（而非 import 時），避免被 import 即產生副作用。
    load_dotenv()  # repo root .env（若存在）→ os.environ；GOVCHECK_* 才讀得到覆寫值
    setup_logging()
    yield


app = FastAPI(title="AI 治理審查小幫手", docs_url=None, redoc_url=None, lifespan=_lifespan)


@app.middleware("http")
async def _request_log(request: Request, call_next):
    """每個請求設 request_id（串連 ops/audit log）。只記方法/路徑/狀態碼，不記 body。"""
    set_request_id(new_request_id())
    log.debug("%s %s", request.method, request.url.path)
    t0 = time.perf_counter()
    response = await call_next(request)
    log.info("%s %s -> %d (%dms)", request.method, request.url.path,
             response.status_code, round((time.perf_counter() - t0) * 1000))
    return response


def _finding_dict(idx: int, f) -> dict:
    return {
        "id": str(idx),
        "severity": f.severity.value,   # "error" / "warn" / "info"
        "code": f.code,
        "title": f.title,
        "message": f.message,
        "location": f.location,
        "expected": f.expected,
        "actual": f.actual,
        "source": f.source,
    }


def _report_dict(report: ReviewReport) -> dict:
    """ReviewReport → 前端 payload。

    刻意不回 `passed`：它只看 error_count，會與 decision_status 互相矛盾
    （規則全綠但 LLM 未跑 → passed=True 而 decision_status=manual_review）。
    通過與否一律以 decision_status 為準。
    """
    return {
        "form_type": report.form_type,
        "subject": report.subject,
        "decision_status": report.decision_status.value,
        "workflow_stage": report.review_run_meta.workflow_stage.value,
        "error_count": report.error_count,
        "warn_count": report.warn_count,
        "info_count": sum(1 for f in report.findings if f.severity is Severity.INFO),
        "risk_score": report.risk_score,
        "risk_grade": report.risk_grade,
        "findings": [_finding_dict(i, f) for i, f in enumerate(report.findings)],
        "item_assessments": [row.model_dump(mode="json") for row in report.item_assessments],
        "checklist_roster": [row.model_dump(mode="json") for row in report.checklist_roster],
        "document_statuses": [
            row.model_dump(mode="json", exclude={"source_units"})
            for row in report.document_statuses
        ],
        "citations": [
            citation.model_dump(mode="json")
            for assessment in report.item_assessments
            for citation in assessment.citations
        ],
        "usage_summary": report.usage_summary.model_dump(mode="json"),
        "review_run_meta": report.review_run_meta.model_dump(mode="json"),
        "markdown": to_markdown(report),
    }


@app.post("/api/review")
async def review(
    f01: UploadFile | None = None,
    f02: UploadFile | None = None,
    f03: UploadFile | None = None,
    supporting: list[UploadFile] | None = None,
    workflow_stage: WorkflowStage = Form(default=WorkflowStage.PROPOSAL),
    enable_llm: bool | None = Form(default=None),
) -> JSONResponse:
    """上傳送件包 → 審查 → 回報告 JSON（含 Markdown 版供下載）。

    F01/F02/F03 各一份（未上傳者由缺件檢查報出），佐證可多份。
    """
    forms = {"f01": f01, "f02": f02, "f03": f03}
    supporting = [up for up in (supporting or []) if up.filename]
    if not any(up is not None and up.filename for up in forms.values()) and not supporting:
        raise HTTPException(status_code=400, detail="請至少上傳一份表單或佐證檔案。")

    use_llm = bool(load_llm_config()["enabled"]) if enable_llm is None else bool(enable_llm)

    # 用後即刪：暫存檔在 TemporaryDirectory 結束時一併清除（最小足跡、地端不外送）
    with tempfile.TemporaryDirectory() as tmpdir:
        paths: dict[str, str] = {}
        for role, up in forms.items():
            if up is not None and up.filename:
                paths[role] = await _persist(up, Path(tmpdir), role)

        supporting_names: list[str] = []
        supporting_paths: list[str] = []
        for idx, up in enumerate(supporting):
            supporting_paths.append(await _persist(up, Path(tmpdir), f"sup{idx}"))
            supporting_names.append(up.filename or f"佐證{idx + 1}")

        try:
            report = review_submission(
                paths,
                supporting=supporting_names,
                supporting_paths=supporting_paths,
                enable_llm=use_llm,
                workflow_stage=workflow_stage,
            )
        except Exception as exc:  # noqa: BLE001 - 介面層需把解析/審查錯誤友善呈現
            log.error("review failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=400, detail=f"解析或審查失敗：{type(exc).__name__}"
            ) from exc

    return JSONResponse(_report_dict(report))


async def _persist(up: UploadFile, tmpdir: Path, prefix: str) -> str:
    """把上傳檔寫入暫存目錄，回傳落地路徑。

    檔名一律取 basename（擋掉 ../ 與絕對路徑逃逸），並加前綴避免同名覆蓋。
    """
    name = up.filename or "file"
    data = await up.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"上傳檔案超過大小限制（上限 50 MB）：{name}")
    dest = tmpdir / f"{prefix}_{Path(name).name or 'file'}"
    dest.write_bytes(data)
    return str(dest)


@app.get("/api/readiness")
async def readiness() -> JSONResponse:
    """LLM 判讀是否就緒（端點、模型、Prompt 三者齊備才算）。"""
    cfg = load_llm_config()
    prompts = build_prompt_gateway(ChatClient.from_config(cfg), cfg).readiness()
    prompt_ready = bool(prompts) and all(
        not value.startswith("unavailable:") for value in prompts.values()
    )
    model_configured = bool(cfg.get("base_url") and cfg.get("model"))
    return JSONResponse({
        "ready": bool(cfg.get("enabled") and prompt_ready and model_configured),
        "llm_enabled": bool(cfg.get("enabled")),
        "model_configured": model_configured,
        "prompts": prompts,
    })


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


# 靜態前端（CSS/JS/字型）。掛在最後，避免吃掉 /api 路由。
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
