"""批次檔案自動分類器：依 Excel 工作表名稱判定 F01/F02/F03/佐證/無法辨識。

分類純為**確定性規則**（讀檔列出分頁名稱，不解析內容、不打網路），屬規則側，
在 parse 之前先做路由。各表單的識別分頁名稱沿用既有單一真值來源：
  - F02：parsers.f02_parser.ASSESS_SHEET（最具辨識度，優先判）
  - F01/F03：review_config.yaml 的 f01.main_sheet / f03.sheet
如此分類器與 parser 永不會對分頁名稱失準。佐證（非 Excel）僅取檔名，沿用現況。
"""

from __future__ import annotations

import warnings
from enum import Enum
from pathlib import Path

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
    SUPPORTING = "supporting"  # 非 Excel 佐證，僅取檔名
    UNKNOWN = "unknown"        # Excel 但無已知表單分頁，或無法開啟


# 分類顯示用中文標籤（表格 / Finding 共用）
KIND_LABEL: dict[FileKind, str] = {
    FileKind.F01: "F01 系統資訊表",
    FileKind.F02: "F02 風險評鑑",
    FileKind.F03: "F03 上線檢核表",
    FileKind.SUPPORTING: "佐證文件",
    FileKind.UNKNOWN: "無法辨識",
}


class FileClassification(BaseModel):
    """單一檔案的分類結果（分類器本地模型，非跨階段契約，故不放 models/）。"""

    path: str | None = None            # 落地後的檔案路徑；UI 預覽階段可為 None
    filename: str                      # 顯示用原始檔名
    kind: FileKind
    matched_sheet: str | None = None   # 觸發判定的分頁名稱（稽核軌跡）
    reason: str                        # zh-TW 說明


def classify_file(path: str | Path, cfg: dict | None = None) -> FileClassification:
    """路徑版分類（給測試與 review_files）。"""
    path = Path(path)
    return _classify(path.suffix, path.name, str(path), lambda: _read_sheets(path), cfg)


def classify_fileobj(fileobj, filename: str, cfg: dict | None = None) -> FileClassification:
    """位元組/緩衝版分類（給 UI 預覽，免落地）。filename 提供副檔名與顯示名稱。"""
    suffix = Path(filename).suffix
    return _classify(suffix, filename, None, lambda: _read_sheets(fileobj), cfg)


def classify_files(paths: list[str | Path], cfg: dict | None = None) -> list[FileClassification]:
    """批次分類，順序保留；cfg 只載一次。"""
    cfg = cfg or load_review_config()
    return [classify_file(p, cfg) for p in paths]


def _classify(suffix: str, filename: str, path: str | None, sheet_lister, cfg) -> FileClassification:
    cfg = cfg or load_review_config()

    # 副檔名閘門：非 Excel 一律佐證，不開檔。
    if suffix.lower() not in _EXCEL_EXTS:
        return FileClassification(
            path=path, filename=filename, kind=FileKind.SUPPORTING,
            reason="非 Excel 檔，視為佐證文件",
        )

    try:
        sheets = set(sheet_lister())
    except Exception as exc:  # noqa: BLE001 - 壞檔不應讓整批崩潰
        return FileClassification(
            path=path, filename=filename, kind=FileKind.UNKNOWN,
            reason=f"無法開啟（{exc}）",
        )

    f01_sheet = cfg["f01"]["main_sheet"]
    f03_sheet = cfg["f03"]["sheet"]

    # 優先序 F02 → F01 → F03（最具辨識度者先；每檔只分類一次）。
    if ASSESS_SHEET in sheets:
        return FileClassification(
            path=path, filename=filename, kind=FileKind.F02, matched_sheet=ASSESS_SHEET,
            reason=f"含 F02 評估表分頁「{ASSESS_SHEET}」",
        )
    if f01_sheet in sheets:
        return FileClassification(
            path=path, filename=filename, kind=FileKind.F01, matched_sheet=f01_sheet,
            reason=f"含 F01 主表分頁「{f01_sheet}」",
        )
    if f03_sheet in sheets:
        return FileClassification(
            path=path, filename=filename, kind=FileKind.F03, matched_sheet=f03_sheet,
            reason=f"含 F03 檢核表分頁「{f03_sheet}」",
        )

    listed = "、".join(sorted(sheets)) if sheets else "（無分頁）"
    return FileClassification(
        path=path, filename=filename, kind=FileKind.UNKNOWN,
        reason=f"Excel 但無任何已知表單分頁（分頁：{listed}）",
    )


def _read_sheets(src) -> list[str]:
    """唯讀開檔只取分頁目錄後立即關閉；read_only 不解析儲存格、.xlsm 安全快速。"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wb = openpyxl.load_workbook(src, read_only=True, data_only=True, keep_links=False)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def route_classifications(
    results: list[FileClassification],
) -> tuple[dict[str, str], list[str], list[Finding]]:
    """把（可能經使用者更正 kind 的）分類清單轉成路由 dict + 佐證檔名 + 分類 Findings。

    回傳 (files, supporting, findings)：
      files：{"f01": path, ...}，可直接餵 review_submission。
      supporting：佐證檔名清單。
      findings：CLASSIFY.SUMMARY（INFO）+ 重複/無法辨識（WARN）。
    """
    files: dict[str, str] = {}
    supporting: list[str] = []
    findings: list[Finding] = []
    seen: dict[FileKind, FileClassification] = {}

    for r in results:
        if r.kind is FileKind.SUPPORTING:
            supporting.append(r.filename)
        elif r.kind is FileKind.UNKNOWN:
            findings.append(_unrecognized_finding(r))
        else:  # F01 / F02 / F03
            if r.kind in seen:
                findings.append(_duplicate_finding(r.kind, seen[r.kind], r))
                # 第一份勝出：保留 files[kind]，重複者只發 WARN。
            elif r.path is not None:
                seen[r.kind] = r
                files[r.kind.value] = r.path

    findings.insert(0, _summary_finding(results))
    return files, supporting, findings


def _summary_finding(results: list[FileClassification]) -> Finding:
    lines = "；".join(f"{r.filename} → {KIND_LABEL[r.kind]}" for r in results)
    return Finding(
        severity=Severity.INFO,
        code="CLASSIFY.SUMMARY",
        title="自動分類結果",
        message=f"共 {len(results)} 個檔案：{lines}。分類依工作表名稱，請人工覆核。",
        location="自動分類",
    )


def _duplicate_finding(kind: FileKind, first: FileClassification, dup: FileClassification) -> Finding:
    label = KIND_LABEL[kind]
    both = f"{first.filename}、{dup.filename}"
    return Finding(
        severity=Severity.WARN,
        code=f"CLASSIFY.DUPLICATE_{kind.value.upper()}",
        title=f"偵測到多份 {label}",
        message=(f"有多個檔案判定為 {label}（{both}）；採用第一份「{first.filename}」，"
                 f"請確認「{dup.filename}」是否誤放或為不同送件。"),
        location="自動分類",
        expected="每種表單一份",
        actual=both,
    )


def _unrecognized_finding(r: FileClassification) -> Finding:
    return Finding(
        severity=Severity.WARN,
        code="CLASSIFY.UNRECOGNIZED",
        title="無法辨識的 Excel 檔",
        message=(f"檔案「{r.filename}」是 Excel 但不含任何已知表單分頁，未納入審查。"
                 f"請確認是否為官方範本，或應改列為佐證文件。"),
        location=r.filename,
        actual=r.reason,
    )
