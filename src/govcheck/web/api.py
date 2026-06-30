"""FastAPI 後端：分類 / 審查 兩個 API + 靜態前端掛載。

對應前端三步驟：
  1) 上傳送件包  → 前端持有檔案
  2) 確認自動分類 → POST /api/classify（記憶體分類、不落地）
  3) 審查報告     → POST /api/review（temp 用後即刪 → review_routed → findings + markdown）

重用既有 pipeline，零改動 checks/parsers/review。比照 app.py 的記憶體分類與
TemporaryDirectory 用後即刪策略（最小足跡、地端不外送）。
"""

from __future__ import annotations

import asyncio
import io
import json
import tempfile
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from govcheck.classify import (
    KIND_LABEL,
    FileClassification,
    FileKind,
    classify_fileobj,
    route_classifications,
)
from govcheck.llm.config import load_llm_config
from govcheck.logging_setup import get_logger, new_request_id, set_request_id, setup_logging
from govcheck.models import ReviewReport, Severity
from govcheck.report.builder import to_markdown
from govcheck.review.engine import review_routed

log = get_logger("api")

# 前端靜態檔位於 repo 根的 web/（src/govcheck/web/api.py → parents[3] = repo root）
WEB_DIR = Path(__file__).resolve().parents[3] / "web"

# 可指派的判定（對齊 app.py：UNKNOWN 不可手動指派，以「忽略此檔」表示排除）
IGNORE = "ignore"
_ASSIGNABLE = [FileKind.F01, FileKind.F02, FileKind.F03, FileKind.SUPPORTING]
KIND_OPTIONS = [{"value": k.value, "label": KIND_LABEL[k]} for k in _ASSIGNABLE] + [
    {"value": IGNORE, "label": "忽略此檔"}
]

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # 在伺服器啟動時才設定 log（而非 import 時），避免被 import 即在 repo root 建出 logs/。
    setup_logging()
    yield


app = FastAPI(title="AI 治理審查小幫手", docs_url=None, redoc_url=None, lifespan=_lifespan)


@app.middleware("http")
async def _request_log(request: Request, call_next):
    """每個請求設 request_id（contextvar，隨 asyncio.to_thread 複製進 SSE worker），
    DEBUG 記進入、INFO 記命中端點與耗時（重點）。只記方法/路徑/狀態碼，不記 body。"""
    set_request_id(new_request_id())
    log.debug("%s %s", request.method, request.url.path)
    t0 = time.perf_counter()
    response = await call_next(request)
    log.info("%s %s -> %d (%dms)", request.method, request.url.path,
             response.status_code, round((time.perf_counter() - t0) * 1000))
    return response


def _severity_str(sev: Severity) -> str:
    return sev.value  # "error" / "warn" / "info"


def _finding_dict(idx: int, f) -> dict:
    return {
        "id": str(idx),
        "severity": _severity_str(f.severity),
        "code": f.code,
        "title": f.title,
        "message": f.message,
        "location": f.location,
        "expected": f.expected,
        "actual": f.actual,
        "source": f.source,
    }


def _report_dict(report: ReviewReport) -> dict:
    findings = [_finding_dict(i, f) for i, f in enumerate(report.findings)]
    info_count = sum(1 for f in report.findings if f.severity is Severity.INFO)
    return {
        "form_type": report.form_type,
        "subject": report.subject,
        "passed": report.passed,
        "error_count": report.error_count,
        "warn_count": report.warn_count,
        "info_count": info_count,
        "findings": findings,
        "markdown": to_markdown(report),
    }


@app.post("/api/classify")
async def classify(files: list[UploadFile]) -> JSONResponse:
    """記憶體分類預覽（不落地）：回傳每檔判定 + 旗標（dup/unknown）+ 可選項。"""
    if not files:
        raise HTTPException(status_code=400, detail="請上傳至少一個檔案。")

    out: list[dict] = []
    seen_form_kinds: set[FileKind] = set()
    for idx, up in enumerate(files):
        data = await up.read()
        name = up.filename or f"file_{idx}"
        c = classify_fileobj(io.BytesIO(data), name)

        flag: str | None = None
        if c.kind is FileKind.UNKNOWN:
            flag = "unknown"
        elif c.kind in _ASSIGNABLE and c.kind is not FileKind.SUPPORTING:
            # F01/F02/F03 同類第 2 份起標記重複（對應分類器 route 的 DUPLICATE_*）
            if c.kind in seen_form_kinds:
                flag = "dup"
            else:
                seen_form_kinds.add(c.kind)

        # 無法辨識 → 預設「忽略此檔」，逼使用者人工指定，不靜默誤路由（比照 app.py）
        default_kind = c.kind.value if c.kind in _ASSIGNABLE else IGNORE
        out.append({
            "index": idx,
            "filename": name,
            "kind": default_kind,
            "reason": c.reason,
            "flag": flag,
        })

    return JSONResponse({"kinds": KIND_OPTIONS, "files": out})


@app.post("/api/review")
async def review(
    files: list[UploadFile],
    kinds: str = Form(...),
    enable_llm: bool | None = Form(default=None),
) -> JSONResponse:
    """依使用者確認後的 kinds 落地 → 路由 → 審查 → 回報告 JSON + markdown。"""
    if not files:
        raise HTTPException(status_code=400, detail="請上傳至少一個檔案。")
    confirmed_kinds = _parse_confirmed_kinds(kinds, len(files))

    use_llm = bool(load_llm_config()["enabled"]) if enable_llm is None else bool(enable_llm)

    # 用後即刪：暫存檔在 TemporaryDirectory 結束時一併清除（最小足跡、地端不外送）
    with tempfile.TemporaryDirectory() as tmpdir:
        confirmed: list[FileClassification] = []
        for idx, (up, kind_value) in enumerate(zip(files, confirmed_kinds)):
            if kind_value == IGNORE:
                continue
            try:
                kind = FileKind(kind_value)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"未知判定 {kind_value!r}") from exc
            if kind not in _ASSIGNABLE:
                raise HTTPException(status_code=400, detail=f"不可指派的判定 {kind_value!r}")
            name = up.filename or "file"
            # 落地檔名一律取 basename（擋掉 ../ 與絕對路徑逃逸），並以索引前綴避免同名覆蓋。
            # 顯示用 filename 仍保留原始名稱（供分類器/報告呈現）。
            safe = Path(name).name or "file"
            dest = Path(tmpdir) / f"{idx}_{safe}"
            dest.write_bytes(await up.read())
            confirmed.append(FileClassification(
                path=str(dest), filename=name, kind=kind, reason="使用者確認/修正",
            ))

        if not confirmed:
            raise HTTPException(
                status_code=400, detail="所有檔案都被標記為「忽略此檔」，沒有可審查的內容。"
            )

        try:
            routed_files, supporting, class_findings = route_classifications(confirmed)
            report = review_routed(routed_files, supporting, class_findings, enable_llm=use_llm)
        except Exception as exc:  # noqa: BLE001 - 介面層需把解析/審查錯誤友善呈現
            log.exception("review failed")  # 完整堆疊寫檔（地端，不外送）
            raise HTTPException(status_code=400, detail=f"解析或審查失敗：{exc}") from exc

    return JSONResponse(_report_dict(report))


def _parse_confirmed_kinds(kinds: str, n_files: int) -> list[str]:
    """解析並驗證前端送來的 kinds JSON 陣列（長度須與檔案數相符）。"""
    try:
        confirmed_kinds = json.loads(kinds)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"kinds 格式錯誤：{exc}") from exc
    if not isinstance(confirmed_kinds, list):
        raise HTTPException(status_code=400, detail="kinds 應為陣列。")
    if len(confirmed_kinds) != n_files:
        raise HTTPException(status_code=400, detail="kinds 數量與檔案數不符。")
    return confirmed_kinds


def _run_review_blocking(
    payloads: list[tuple[str, bytes]],
    confirmed_kinds: list[str],
    use_llm: bool,
    emit: Callable[[dict], None],
) -> dict:
    """工作執行緒內跑：落地檔 → 路由 → 審查 → 回報告 dict。

    與 /api/review 同策略（TemporaryDirectory 用後即刪、basename 防逃逸、索引前綴防覆蓋），
    差別僅在落地與審查各階段透過 emit 串出進度事件（upload / parse / rules / llm）。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        # 先濾掉「忽略此檔」，再以原始索引落地（與 /api/review 的 idx 命名一致）
        to_write = [
            (idx, name, data, kind_value)
            for idx, ((name, data), kind_value) in enumerate(zip(payloads, confirmed_kinds))
            if kind_value != IGNORE
        ]
        confirmed: list[FileClassification] = []
        for done, (idx, name, data, kind_value) in enumerate(to_write, start=1):
            try:
                kind = FileKind(kind_value)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"未知判定 {kind_value!r}") from exc
            if kind not in _ASSIGNABLE:
                raise HTTPException(status_code=400, detail=f"不可指派的判定 {kind_value!r}")
            safe = Path(name).name or "file"
            dest = Path(tmpdir) / f"{idx}_{safe}"
            dest.write_bytes(data)
            confirmed.append(FileClassification(
                path=str(dest), filename=name, kind=kind, reason="使用者確認/修正",
            ))
            emit({"stage": "upload", "label": "落地檔案", "done": done, "total": len(to_write)})

        if not confirmed:
            raise HTTPException(
                status_code=400, detail="所有檔案都被標記為「忽略此檔」，沒有可審查的內容。"
            )

        routed_files, supporting, class_findings = route_classifications(confirmed)
        report = review_routed(
            routed_files, supporting, class_findings, enable_llm=use_llm, progress=emit,
        )
    return _report_dict(report)


@app.post("/api/review/stream")
async def review_stream(
    files: list[UploadFile],
    kinds: str = Form(...),
    enable_llm: bool | None = Form(default=None),
) -> StreamingResponse:
    """同 /api/review，但以 SSE（text/event-stream）即時串出各階段進度，最後一筆帶完整報告。

    阻塞式審查在工作執行緒跑，進度事件經 asyncio.Queue 跨執行緒回主事件圈再 yield 出去；
    用後即刪、地端不外送策略與 /api/review 完全一致。
    """
    if not files:
        raise HTTPException(status_code=400, detail="請上傳至少一個檔案。")
    confirmed_kinds = _parse_confirmed_kinds(kinds, len(files))
    # 入參驗證一律在串流開始前完成 → 回正規 HTTP 4xx（串流一旦開始 header 已送出，只能改用
    # in-band error 事件；與 /api/review 對齊，避免「壞輸入卻回 200」的契約漂移）。
    assignable_values = {k.value for k in _ASSIGNABLE}
    non_ignored = [kv for kv in confirmed_kinds if kv != IGNORE]
    for kv in non_ignored:
        if kv not in assignable_values:
            raise HTTPException(status_code=400, detail=f"不可指派的判定 {kv!r}")
    if not non_ignored:
        raise HTTPException(
            status_code=400, detail="所有檔案都被標記為「忽略此檔」，沒有可審查的內容。"
        )
    use_llm = bool(load_llm_config()["enabled"]) if enable_llm is None else bool(enable_llm)

    # UploadFile.read 是 async，必須在事件圈內先讀成 bytes，才能丟進工作執行緒。
    payloads: list[tuple[str, bytes]] = [(up.filename or "file", await up.read()) for up in files]

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    def emit(event: dict) -> None:
        # 工作執行緒 → 主事件圈：執行緒安全地把事件丟進 queue，順序保留。
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def worker() -> None:
        try:
            report = _run_review_blocking(payloads, confirmed_kinds, use_llm, emit)
            emit({"stage": "done", "label": "完成", "report": report})
        except HTTPException as exc:
            emit({"stage": "error", "message": str(exc.detail)})
        except Exception as exc:  # noqa: BLE001 - 介面層需把解析/審查錯誤友善呈現
            log.exception("review failed")  # 完整堆疊寫檔（地端，不外送）
            emit({"stage": "error", "message": f"解析或審查失敗：{exc}"})
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, sentinel)

    async def gen():
        task = asyncio.create_task(asyncio.to_thread(worker))
        try:
            while True:
                event = await queue.get()
                if event is sentinel:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            await task

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


# 靜態前端（CSS/JS/字型）。掛在最後，避免吃掉 /api 路由。
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
