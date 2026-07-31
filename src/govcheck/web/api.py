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
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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
from govcheck.report.defects import (
    build_common_facts,
    build_defects,
    build_layers,
    build_notices,
    build_uniform_columns,
    build_verdict,
    parse_item_id,
)
from govcheck.review.config import load_review_config
from govcheck.review.engine import review_routed, review_submission
from govcheck.web import decisions as decision_store

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

# 串流用戶中途離開時，worker 仍須持有 TemporaryDirectory 直到審查收尾。
# asyncio event loop 只對 Task 保留 weak reference，故在此保留少量進行中的 task。
_BACKGROUND_TASKS: set[asyncio.Task] = set()


@dataclass(frozen=True)
class _ReviewUploads:
    """已驗證的審查請求；批次與分欄模式只會有一邊帶值。"""

    batch_uploads: list[UploadFile]
    confirmed_kinds: list[str] | None
    forms: dict[str, UploadFile | None]
    supporting: list[UploadFile]
    workflow_stage: WorkflowStage
    use_llm: bool


class _DecisionBody(BaseModel):
    """覆核決定整批覆寫的 request body。"""

    decisions: dict[str, str] = Field(default_factory=dict)
    subject: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


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


def _citation_locator(citation) -> dict | None:
    """把既有 Citation 的 type/value 安全投影成報告頁 locator。

    佐證 parser 刻意不保存原始檔名，所以 file 只回不可逆的 document_id；
    不把 hash 反解或從暫存路徑猜檔名。
    """
    if citation is None:
        return None
    locator_type = str(citation.locator_type or "").casefold()
    locator_value = str(citation.locator_value or "").strip()
    locator = {
        "file": citation.document_id or None,
        "sheet": None,
        "cell": None,
        "page": None,
        "section": None,
    }
    if locator_type == "page":
        try:
            locator["page"] = int(locator_value)
        except ValueError:
            locator["section"] = locator_value or None
    elif locator_type == "sheet":
        locator["sheet"] = locator_value or None
    elif locator_type in {"section", "heading"}:
        locator["section"] = locator_value or None
    elif locator_value:
        # 新 parser 若增加 locator type，先保留可稽核值，不靜默丟失。
        locator["section"] = f"{citation.locator_type}:{locator_value}"
    return locator


def _rubrics_dict(report: ReviewReport) -> dict[str, dict]:
    """展開列所需的 rubric／引用／命中代碼投影。

    句子與勾選只採信 assessment.rubric；引用只採信已經 allow-list
    驗證過的 assessment.citations。Finding 只提供該項實際命中的 code。
    """
    codes_by_item: dict[str, set[str]] = {}
    for finding in report.findings:
        item_id = parse_item_id(finding.location)
        if item_id:
            codes_by_item.setdefault(item_id, set()).add(finding.code)

    rubrics: dict[str, dict] = {}
    for assessment in report.item_assessments:
        if not assessment.rubric:
            continue
        citation = assessment.citations[0] if assessment.citations else None
        rubrics[assessment.item_id] = {
            "lines": [line.model_dump(mode="json") for line in assessment.rubric],
            "quote": citation.quote if citation and citation.quote else None,
            "quote_locator": _citation_locator(citation),
            "codes": sorted(codes_by_item.get(assessment.item_id, set())),
        }
    return rubrics


def _report_dict(report: ReviewReport) -> dict:
    """ReviewReport → 前端 payload。

    刻意不回 `passed`：它只看 error_count，會與 decision_status 互相矛盾
    （規則全綠但 LLM 未跑 → passed=True 而 decision_status=manual_review）。
    通過與否一律以 decision_status 為準。
    """
    defects = build_defects(report, load_review_config())
    notices = build_notices(report.findings)
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
        # v2 報告頁欄位：全數由 ReviewReport 推導，不影響舊 model_dump/golden。
        "review_id": report.review_id,
        "verdict": build_verdict(report, defects).model_dump(mode="json"),
        "layers": [row.model_dump(mode="json") for row in build_layers(report)],
        "defects": [row.model_dump(mode="json") for row in defects],
        "notices": [_finding_dict(i, row) for i, row in enumerate(notices)],
        "common_facts": [
            row.model_dump(mode="json")
            for row in build_common_facts(defects, len(report.checklist_roster))
        ],
        "uniform_columns": build_uniform_columns(report),
        "rubrics": _rubrics_dict(report),
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
    request = _prepare_review_uploads(
        files=files,
        kinds=kinds,
        f01=f01,
        f02=f02,
        f03=f03,
        supporting=supporting,
        workflow_stage=workflow_stage,
        enable_llm=enable_llm,
    )
    report = await _execute_review_uploads(request)
    return JSONResponse(_report_dict(report))


def _prepare_review_uploads(
    *,
    files: list[UploadFile] | None,
    kinds: str | None,
    f01: UploadFile | None,
    f02: UploadFile | None,
    f03: UploadFile | None,
    supporting: list[UploadFile] | None,
    workflow_stage: WorkflowStage,
    enable_llm: bool | None,
) -> _ReviewUploads:
    """共用的 multipart 模式、分類結果與上傳限制驗證。"""
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
    confirmed_kinds: list[str] | None = None
    if batch_uploads:
        if kinds is None:
            raise HTTPException(status_code=400, detail="批次上傳缺少分類確認結果 kinds。")
        confirmed_kinds = _parse_confirmed_kinds(kinds, len(batch_uploads))
        _validate_uploads(batch_uploads)
    elif kinds is not None:
        raise HTTPException(status_code=400, detail="分欄上傳不應提供 kinds。")

    routed_uploads = [
        upload
        for upload in [*forms.values(), *supporting]
        if upload is not None and upload.filename
    ]
    if routed_uploads:
        _validate_uploads(routed_uploads)
    return _ReviewUploads(
        batch_uploads=batch_uploads,
        confirmed_kinds=confirmed_kinds,
        forms=forms,
        supporting=supporting,
        workflow_stage=workflow_stage,
        use_llm=use_llm,
    )


async def _execute_review_uploads(
    request: _ReviewUploads,
    *,
    progress=None,
) -> ReviewReport:
    """共用的審查執行入口；一次性 JSON 與 SSE 必然走同一條路徑。"""
    if request.batch_uploads:
        return await _review_batch_uploads(
            request.batch_uploads,
            request.confirmed_kinds or [],
            request.use_llm,
            request.workflow_stage,
            progress=progress,
        )
    return await _review_routed_uploads(request, progress=progress)


async def _review_routed_uploads(
    request: _ReviewUploads,
    *,
    progress=None,
) -> ReviewReport:
    """落地分欄上傳並在 worker thread 執行審查。"""

    # 用後即刪：暫存檔在 TemporaryDirectory 結束時一併清除（最小足跡、地端不外送）
    with tempfile.TemporaryDirectory() as tmpdir:
        paths: dict[str, str] = {}
        for role, up in request.forms.items():
            if up is not None and up.filename:
                paths[role] = await _persist(up, Path(tmpdir), role)

        supporting_names: list[str] = []
        supporting_paths: list[str] = []
        for idx, up in enumerate(request.supporting):
            supporting_paths.append(await _persist(up, Path(tmpdir), f"sup{idx}"))
            supporting_names.append(up.filename or f"佐證{idx + 1}")

        try:
            return await asyncio.to_thread(
                review_submission,
                paths,
                supporting=supporting_names,
                supporting_paths=supporting_paths,
                enable_llm=request.use_llm,
                workflow_stage=request.workflow_stage,
                progress=progress,
            )
        except Exception as exc:  # noqa: BLE001 - 介面層需把解析/審查錯誤友善呈現
            log.error("review failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=400, detail=f"解析或審查失敗：{type(exc).__name__}"
            ) from exc


async def _review_batch_uploads(
    uploads: list[UploadFile],
    confirmed_kinds: list[str],
    use_llm: bool,
    workflow_stage: WorkflowStage,
    *,
    progress=None,
) -> ReviewReport:
    """落地使用者確認後的批次分類，路由至現行審查引擎。"""
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
            return await asyncio.to_thread(
                review_routed,
                routed_files,
                supporting=supporting_names,
                class_findings=class_findings,
                enable_llm=use_llm,
                supporting_paths=supporting_paths,
                workflow_stage=workflow_stage,
                progress=progress,
            )
        except Exception as exc:  # noqa: BLE001 - 介面層需友善呈現解析／審查錯誤
            log.error("batch review failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=400,
                detail=f"解析或審查失敗：{type(exc).__name__}",
            ) from exc


def _sse_frame(event: dict) -> str:
    """單一 SSE data frame；報告內容使用 JSON，不把文字拆成多行 SSE。"""
    return f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _stream_event(event: dict) -> dict:
    """把 engine progress event 轉成可 JSON 序列化的 API event。"""
    payload = dict(event)
    report = payload.get("report")
    if isinstance(report, ReviewReport):
        payload["report"] = _report_dict(report)
    return payload


@app.post("/api/review/stream")
async def review_stream(
    files: list[UploadFile] | None = None,
    kinds: str | None = Form(default=None),
    f01: UploadFile | None = None,
    f02: UploadFile | None = None,
    f03: UploadFile | None = None,
    supporting: list[UploadFile] | None = None,
    workflow_stage: WorkflowStage = Form(default=WorkflowStage.PROPOSAL),
    enable_llm: bool | None = Form(default=None),
) -> StreamingResponse:
    """以 SSE 漸進回傳分層狀態、L1 中間報告與最終報告。

    EventSource 不能傳 multipart POST，前端應使用 fetch + ReadableStream。
    引擎在 worker thread 執行，progress callback 只透過 thread-safe queue
    將小型事件送回 event loop。
    """
    review_request = _prepare_review_uploads(
        files=files,
        kinds=kinds,
        f01=f01,
        f02=f02,
        f03=f03,
        supporting=supporting,
        workflow_stage=workflow_stage,
        enable_llm=enable_llm,
    )

    async def events():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict | None] = asyncio.Queue()

        def progress(event: dict) -> None:
            # engine 的 final 已帶完整 ReviewReport；HTTP 層仍以 producer 取得 return value
            # 後統一投影成 API dict，並濾掉 callback 版本，避免前端收到兩個 final。
            if event.get("kind") == "final":
                return
            loop.call_soon_threadsafe(queue.put_nowait, event)

        async def produce() -> None:
            try:
                report = await _execute_review_uploads(review_request, progress=progress)
                await queue.put({"kind": "final", "report": _report_dict(report)})
            except HTTPException as exc:
                await queue.put({
                    "kind": "error",
                    "status": exc.status_code,
                    "detail": exc.detail,
                })
            except Exception as exc:  # noqa: BLE001 - 串流已開始，只能以 error frame 回報
                log.error("stream review failed: %s", type(exc).__name__)
                await queue.put({
                    "kind": "error",
                    "status": 500,
                    "detail": f"審查失敗：{type(exc).__name__}",
                })
            finally:
                await queue.put(None)

        producer = asyncio.create_task(produce())
        _BACKGROUND_TASKS.add(producer)
        producer.add_done_callback(_BACKGROUND_TASKS.discard)
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield _sse_frame(_stream_event(event))
            await producer
        finally:
            # 用戶中途關閉頁面時不 cancel worker；cancel asyncio.to_thread 不會
            # 停下實際 thread，卻可能讓 TemporaryDirectory 提前被刪除。
            if producer.done():
                producer.result()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


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


@app.post("/api/review/{review_id}/decisions")
async def save_review_decisions(review_id: str, body: _DecisionBody) -> JSONResponse:
    """整批覆寫某次審查的覆核決定；只存項次、決定與備註。"""
    try:
        saved = await asyncio.to_thread(
            decision_store.save,
            review_id,
            body.decisions,
            subject=body.subject,
            note=body.note,
        )
    except decision_store.DecisionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(saved)


@app.get("/api/review/{review_id}/decisions")
async def get_review_decisions(review_id: str) -> JSONResponse:
    """讀回某次審查的覆核決定；尚未存過時回空 decisions。"""
    try:
        saved = await asyncio.to_thread(decision_store.load, review_id)
    except decision_store.DecisionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(saved or {"review_id": review_id, "decisions": {}})


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
