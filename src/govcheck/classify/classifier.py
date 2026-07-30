"""批次檔案自動分類器：依 Excel 工作表名稱判定 F01/F02/F03/佐證/無法辨識。

分類純為確定性規則（讀檔列出分頁名稱，不解析內容、不打網路），在 parse 之前先做路由。
各表單的識別分頁名稱沿用既有單一真值來源：

- F02：parsers.f02_parser.ASSESS_SHEET（最具辨識度，優先判）
- F01/F03：review_config.yaml 的 f01.main_sheet / f03.sheet
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import BinaryIO

import openpyxl
from pydantic import BaseModel

from govcheck.models import Finding, Severity
from govcheck.parsers.f02_parser import ASSESS_SHEET
from govcheck.review.config import load_review_config

# Excel 副檔名（含範本）；其餘一律視為佐證，不開檔。
_EXCEL_EXTS = {".xlsx", ".xlsm", ".xltx", ".xltm"}


class FileKind(str, Enum):
    """檔案分類；F01/F02/F03 的 value 對齊 review_submission 的路由鍵。"""

    F01 = "f01"
    F02 = "f02"
    F03 = "f03"
    SUPPORTING = "supporting"
    UNKNOWN = "unknown"
    IGNORED = "ignore"


KIND_LABEL: dict[FileKind, str] = {
    FileKind.F01: "F01 系統資訊表",
    FileKind.F02: "F02 風險評鑑",
    FileKind.F03: "F03 上線檢核表",
    FileKind.SUPPORTING: "佐證文件",
    FileKind.UNKNOWN: "無法辨識",
    FileKind.IGNORED: "忽略（未納入審查）",
}


class FileClassification(BaseModel):
    """單一檔案的分類結果。"""

    path: str | None = None
    filename: str
    kind: FileKind
    matched_sheet: str | None = None
    reason: str


def classify_file(path: str | Path, cfg: dict | None = None) -> FileClassification:
    """依落地檔案分類。"""
    path = Path(path)
    return _classify(path.suffix, path.name, str(path), lambda: _read_sheets(path), cfg)


def classify_fileobj(
    fileobj: BinaryIO,
    filename: str,
    cfg: dict | None = None,
) -> FileClassification:
    """依可 seek 的上傳檔物件分類，供 UI 預覽使用。"""
    return _classify(
        Path(filename).suffix,
        filename,
        None,
        lambda: _read_sheets(fileobj),
        cfg,
    )


def classify_files(
    paths: list[str | Path], cfg: dict | None = None
) -> list[FileClassification]:
    """批次分類並保留輸入順序。"""
    cfg = cfg or load_review_config()
    return [classify_file(path, cfg) for path in paths]


def _classify(
    suffix: str,
    filename: str,
    path: str | None,
    sheet_lister: Callable[[], list[str]],
    cfg: dict | None,
) -> FileClassification:
    cfg = cfg or load_review_config()

    if suffix.lower() not in _EXCEL_EXTS:
        return FileClassification(
            path=path,
            filename=filename,
            kind=FileKind.SUPPORTING,
            reason="非 Excel 檔，視為佐證文件",
        )

    try:
        sheets = set(sheet_lister())
    except Exception as exc:  # noqa: BLE001 - 壞檔不應讓整批崩潰
        return FileClassification(
            path=path,
            filename=filename,
            kind=FileKind.UNKNOWN,
            reason=f"無法開啟（{exc}）",
        )

    f01_sheet = cfg["f01"]["main_sheet"]
    f03_sheet = cfg["f03"]["sheet"]

    # 最具辨識度者優先；每檔只分類一次。
    if ASSESS_SHEET in sheets:
        return FileClassification(
            path=path,
            filename=filename,
            kind=FileKind.F02,
            matched_sheet=ASSESS_SHEET,
            reason=f"含 F02 評估表分頁「{ASSESS_SHEET}」",
        )
    if f01_sheet in sheets:
        return FileClassification(
            path=path,
            filename=filename,
            kind=FileKind.F01,
            matched_sheet=f01_sheet,
            reason=f"含 F01 主表分頁「{f01_sheet}」",
        )
    if f03_sheet in sheets:
        return FileClassification(
            path=path,
            filename=filename,
            kind=FileKind.F03,
            matched_sheet=f03_sheet,
            reason=f"含 F03 檢核表分頁「{f03_sheet}」",
        )

    listed = "、".join(sorted(sheets)) if sheets else "（無分頁）"
    return FileClassification(
        path=path,
        filename=filename,
        kind=FileKind.UNKNOWN,
        reason=f"Excel 但無任何已知表單分頁（分頁：{listed}）",
    )


def _read_sheets(src: str | Path | BinaryIO) -> list[str]:
    """以唯讀模式取得 Excel 分頁名稱後立即關閉。"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        workbook = openpyxl.load_workbook(
            src,
            read_only=True,
            data_only=True,
            keep_links=False,
        )
    try:
        return list(workbook.sheetnames)
    finally:
        workbook.close()


def route_classifications(
    results: list[FileClassification],
) -> tuple[dict[str, str], list[str], list[Finding]]:
    """把確認後的分類清單轉成核心表路由、佐證檔名與分類 findings。"""
    files: dict[str, str] = {}
    supporting: list[str] = []
    findings: list[Finding] = []
    seen: dict[FileKind, FileClassification] = {}

    for result in results:
        if result.kind is FileKind.IGNORED:
            findings.append(_ignored_finding(result))
        elif result.kind is FileKind.UNKNOWN:
            findings.append(_unrecognized_finding(result))
        elif result.path is None:
            findings.append(_missing_path_finding(result))
        elif result.kind is FileKind.SUPPORTING:
            supporting.append(result.filename)
        elif result.kind in seen:
            findings.append(_duplicate_finding(result.kind, seen[result.kind], result))
        else:
            seen[result.kind] = result
            files[result.kind.value] = result.path

    findings.insert(0, _summary_finding(results))
    return files, supporting, findings


def _summary_finding(results: list[FileClassification]) -> Finding:
    lines = "；".join(f"{result.filename} → {KIND_LABEL[result.kind]}" for result in results)
    return Finding(
        severity=Severity.INFO,
        code="CLASSIFY.SUMMARY",
        title="送審分類結果",
        message=(
            f"共 {len(results)} 個檔案：{lines}。此為送審分類結果；"
            "自動建議依工作表名稱，請人工覆核。"
        ),
        location="自動分類",
    )


def _duplicate_finding(
    kind: FileKind,
    first: FileClassification,
    duplicate: FileClassification,
) -> Finding:
    label = KIND_LABEL[kind]
    both = f"{first.filename}、{duplicate.filename}"
    return Finding(
        severity=Severity.WARN,
        code=f"CLASSIFY.DUPLICATE_{kind.value.upper()}",
        title=f"偵測到多份 {label}",
        message=(
            f"有多個檔案判定為 {label}（{both}）；採用第一份「{first.filename}」，"
            f"請確認「{duplicate.filename}」是否誤放或為不同送件。"
        ),
        location="自動分類",
        expected="每種表單一份",
        actual=both,
    )


def _unrecognized_finding(result: FileClassification) -> Finding:
    return Finding(
        severity=Severity.WARN,
        code="CLASSIFY.UNRECOGNIZED",
        title="無法辨識的 Excel 檔",
        message=(
            f"檔案「{result.filename}」是 Excel 但不含任何已知表單分頁，未納入審查。"
            "請確認是否為官方範本，或應改列為佐證文件。"
        ),
        location=result.filename,
        actual=result.reason,
    )


def _ignored_finding(result: FileClassification) -> Finding:
    return Finding(
        severity=Severity.WARN,
        code="CLASSIFY.IGNORED",
        title="檔案已排除於本次審查",
        message=(
            f"檔案「{result.filename}」已被標記為忽略，未送入任何表單或佐證解析。"
            "請人工確認排除合理。"
        ),
        location=result.filename,
        expected="送入適當類別，或明確確認排除",
        actual="忽略",
    )


def _missing_path_finding(result: FileClassification) -> Finding:
    return Finding(
        severity=Severity.ERROR,
        code="CLASSIFY.MISSING_PATH",
        title="分類結果缺少可審查檔案",
        message=(
            f"檔案「{result.filename}」被指定為「{KIND_LABEL[result.kind]}」，"
            "但分類結果沒有實際檔案路徑，因此未納入審查。"
        ),
        location=result.filename,
        expected="可讀取的本機暫存路徑",
        actual="path=None",
    )
