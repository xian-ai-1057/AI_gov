"""FastAPI 後端：批次分類、送件包審查、就緒查詢與靜態前端掛載。

對應前端三步驟：
  1) 一次上傳整批送件包
  2) POST /api/classify 預覽並確認自動分類
  3) POST /api/review 審查（temp 用後即刪 → 結構化報告）

`/api/review` 也保留 f01/f02/f03/supporting 分欄參數，供既有呼叫端使用。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from govcheck.classify import (
    KIND_LABEL,
    FileClassification,
    FileKind,
    classify_fileobj,
    route_classifications,
)
from govcheck.llm.client import ChatClient
from govcheck.llm.config import load_llm_config
from govcheck.logging_setup import get_logger, new_request_id, set_request_id, setup_logging
from govcheck.models import ReviewReport, Severity, WorkflowStage
from govcheck.prompts import build_prompt_gateway
from govcheck.report.builder import to_markdown
from govcheck.review.engine import review_routed, review_submission

log = get_logger("api")

# 前端靜態檔位於 repo 根的 web/（src/govcheck/web/api.py → parents[3] = repo root）
WEB_DIR = Path(__file__).resolve().parents[3] / "web"

# 單檔上限（50 MB）：防止記憶體耗盡或磁碟塞滿
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_BATCH_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_UPLOAD_FILES = 100
UPLOAD_CHUNK_BYTES = 1024 * 1024

# UNKNOWN 不可直接送審；使用者須明確改判為一種可路由類別，或選擇忽略。
IGNORE = FileKind.IGNORED.value
_ASSIGNABLE = (FileKind.F01, FileKind.F02, FileKind.F03, FileKind.SUPPORTING)
KIND_OPTIONS = [{"value": kind.value, "label": KIND_LABEL[kind]} for kind in _ASSIGNABLE]
KIND_OPTIONS.append({"value": IGNORE, "label": "忽略此檔"})


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


@app.post("/api/classify")
async def classify(files: list[UploadFile] | None = None) -> JSONResponse:
    """預覽批次分類；應用不持久化內容，request 暫存由框架在結束後清除。"""
    uploads = [upload for upload in (files or []) if upload.filename]
    if not uploads:
        raise HTTPException(status_code=400, detail="請上傳至少一個檔案。")
    _validate_uploads(uploads)

    results: list[dict] = []
    seen_form_kinds: set[FileKind] = set()
    for index, upload in enumerate(uploads):
        name = upload.filename or f"file_{index}"
        await upload.seek(0)
        classification = await asyncio.to_thread(
            classify_fileobj,
            upload.file,
            name,
        )
        await upload.seek(0)

        flag: str | None = None
        if classification.kind is FileKind.UNKNOWN:
            flag = "unknown"
        elif classification.kind is not FileKind.SUPPORTING:
            if classification.kind in seen_form_kinds:
                flag = "dup"
            else:
                seen_form_kinds.add(classification.kind)

        default_kind = (
            classification.kind.value
            if classification.kind in _ASSIGNABLE
            else IGNORE
        )
        results.append(
            {
                "index": index,
                "filename": name,
                "kind": default_kind,
                "reason": classification.reason,
                "flag": flag,
            }
        )

    return JSONResponse({"kinds": KIND_OPTIONS, "files": results})


@app.post("/api/review")
async def review(
    files: list[UploadFile] | None = None,
    kinds: str | None = Form(default=None),
    f01: UploadFile | None = None,
    f02: UploadFile | None = None,
    f03: UploadFile | None = None,
    supporting: list[UploadFile] | None = None,
    workflow_stage: WorkflowStage = Form(default=WorkflowStage.PROPOSAL),
    enable_llm: bool | None = Form(default=None),
) -> JSONResponse:
    """審查送件包並回傳報告 JSON（含 Markdown）。

    批次模式使用 repeated `files` + 與其等長的 `kinds` JSON 陣列；既有分欄模式仍可使用
    `f01`、`f02`、`f03` 與 repeated `supporting`。兩種模式不可混用。
    """
    batch_uploads = [upload for upload in (files or []) if upload.filename]
    forms = {"f01": f01, "f02": f02, "f03": f03}
    supporting = [up for up in (supporting or []) if up.filename]
    has_routed_uploads = (
        any(upload is not None and upload.filename for upload in forms.values())
        or bool(supporting)
    )
    if batch_uploads and has_routed_uploads:
        raise HTTPException(status_code=400, detail="批次上傳與分欄上傳不可混用。")
    if not batch_uploads and not has_routed_uploads:
        raise HTTPException(status_code=400, detail="請至少上傳一份表單或佐證檔案。")

    use_llm = bool(load_llm_config()["enabled"]) if enable_llm is None else bool(enable_llm)

    if batch_uploads:
        if kinds is None:
            raise HTTPException(status_code=400, detail="批次上傳缺少分類確認結果 kinds。")
        confirmed_kinds = _parse_confirmed_kinds(kinds, len(batch_uploads))
        report = await _review_batch_uploads(
            batch_uploads,
            confirmed_kinds,
            use_llm,
            workflow_stage,
        )
        return JSONResponse(_report_dict(report))
    if kinds is not None:
        raise HTTPException(status_code=400, detail="分欄上傳不應提供 kinds。")

    routed_uploads = [
        upload
        for upload in [*forms.values(), *supporting]
        if upload is not None and upload.filename
    ]
    _validate_uploads(routed_uploads)

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


async def _review_batch_uploads(
    uploads: list[UploadFile],
    confirmed_kinds: list[str],
    use_llm: bool,
    workflow_stage: WorkflowStage,
) -> ReviewReport:
    """落地使用者確認後的批次分類，路由至現行審查引擎。"""
    _validate_uploads(uploads)

    with tempfile.TemporaryDirectory() as tmpdir:
        confirmed: list[FileClassification] = []
        for index, (upload, kind_value) in enumerate(zip(uploads, confirmed_kinds)):
            try:
                kind = FileKind(kind_value)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=f"未知判定 {kind_value!r}") from exc
            if kind not in {*_ASSIGNABLE, FileKind.IGNORED}:
                raise HTTPException(status_code=400, detail=f"不可指派的判定 {kind_value!r}")

            name = upload.filename or f"file_{index}"
            if kind is FileKind.IGNORED:
                confirmed.append(
                    FileClassification(
                        filename=name,
                        kind=kind,
                        reason="使用者確認忽略",
                    )
                )
                continue

            path = await _persist(upload, Path(tmpdir), f"batch{index}")
            confirmed.append(
                FileClassification(
                    path=path,
                    filename=name,
                    kind=kind,
                    reason="使用者確認/修正",
                )
            )

        if all(item.kind is FileKind.IGNORED for item in confirmed):
            raise HTTPException(
                status_code=400,
                detail="所有檔案都被標記為「忽略此檔」，沒有可審查的內容。",
            )

        routed_files, supporting_names, class_findings = route_classifications(confirmed)
        supporting_paths = [
            item.path
            for item in confirmed
            if item.kind is FileKind.SUPPORTING and item.path is not None
        ]
        try:
            return review_routed(
                routed_files,
                supporting=supporting_names,
                class_findings=class_findings,
                enable_llm=use_llm,
                supporting_paths=supporting_paths,
                workflow_stage=workflow_stage,
            )
        except Exception as exc:  # noqa: BLE001 - 介面層需友善呈現解析／審查錯誤
            log.error("batch review failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=400,
                detail=f"解析或審查失敗：{type(exc).__name__}",
            ) from exc


def _parse_confirmed_kinds(kinds: str, file_count: int) -> list[str]:
    """解析並驗證前端送來的 kinds JSON 陣列。"""
    try:
        confirmed = json.loads(kinds)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"kinds 格式錯誤：{exc}") from exc
    if not isinstance(confirmed, list):
        raise HTTPException(status_code=400, detail="kinds 應為陣列。")
    if len(confirmed) != file_count:
        raise HTTPException(status_code=400, detail="kinds 數量與檔案數不符。")
    allowed = {kind.value for kind in _ASSIGNABLE} | {IGNORE}
    invalid = next(
        (value for value in confirmed if not isinstance(value, str) or value not in allowed),
        None,
    )
    if invalid is not None or any(value is None for value in confirmed):
        raise HTTPException(status_code=400, detail=f"未知判定 {invalid!r}")
    return confirmed


async def _persist(up: UploadFile, tmpdir: Path, prefix: str) -> str:
    """把上傳檔寫入暫存目錄，回傳落地路徑。

    檔名一律取 basename（擋掉 ../ 與絕對路徑逃逸），並加前綴避免同名覆蓋。
    """
    name = up.filename or "file"
    dest = tmpdir / f"{prefix}_{Path(name).name or 'file'}"
    written = 0
    await up.seek(0)
    try:
        with dest.open("wb") as target:
            while chunk := await up.read(UPLOAD_CHUNK_BYTES):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"上傳檔案超過大小限制（上限 50 MB）：{name}",
                    )
                target.write(chunk)
    finally:
        await up.seek(0)
    return str(dest)


def _validate_uploads(uploads: list[UploadFile]) -> None:
    """驗證檔案數、單檔與整批大小。"""
    if len(uploads) > MAX_UPLOAD_FILES:
        raise HTTPException(
            status_code=413,
            detail=f"上傳檔案數超過限制（上限 {MAX_UPLOAD_FILES} 個）。",
        )

    total = 0
    for upload in uploads:
        size = upload.size
        if size is None:
            position = upload.file.tell()
            upload.file.seek(0, 2)
            size = upload.file.tell()
            upload.file.seek(position)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    "上傳檔案超過大小限制（上限 50 MB）："
                    f"{upload.filename or 'file'}"
                ),
            )
        total += size
    if total > MAX_BATCH_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="整批上傳超過大小限制（上限 200 MB）。",
        )


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
