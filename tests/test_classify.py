"""批次自動分類、路由與審查編排測試。

分類只看工作表名稱，不看檔名；非 Excel 一律視為佐證，無已知分頁或壞掉的
Excel 則標示為無法辨識。需要真的解析表單的端到端案例才使用官方範本。
"""

from __future__ import annotations

import io

import openpyxl
import pytest

from govcheck.classify import (
    FileClassification,
    FileKind,
    classify_file,
    classify_fileobj,
    classify_files,
    route_classifications,
)
from govcheck.parsers.f02_parser import ASSESS_SHEET
from govcheck.review.config import load_review_config
from govcheck.review.engine import review_files, review_routed
from tests.fixture_builder import (
    OFFICIAL,
    OFFICIAL_F01,
    OFFICIAL_F03,
    build_f01_fixture,
    build_f02_fixture,
    build_f03_fixture,
)

_CFG = load_review_config()
F01_SHEET = _CFG["f01"]["main_sheet"]
F03_SHEET = _CFG["f03"]["sheet"]

requires_templates = pytest.mark.skipif(
    not (OFFICIAL.exists() and OFFICIAL_F01.exists() and OFFICIAL_F03.exists()),
    reason="官方範本不存在（data/original 未連結）",
)


def _codes(report) -> set[str]:
    return {finding.code for finding in report.findings}


def _xlsx_with_sheets(path, *sheet_names: str):
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for sheet_name in sheet_names:
        workbook.create_sheet(sheet_name)
    workbook.save(path)
    return path


def _classification(
    kind: FileKind,
    name: str,
    path: str | None = None,
) -> FileClassification:
    return FileClassification(
        path=path if path is not None else f"/fake/{name}",
        filename=name,
        kind=kind,
        reason="測試資料",
    )


# ── 分類器：純規則、無須官方範本 ─────────────────────────────────────


@pytest.mark.parametrize("filename", ["R06_可解釋性說明.pdf", "委外技術文件.docx"])
def test_non_excel_is_supporting_without_opening_file(tmp_path, filename):
    path = tmp_path / filename
    path.write_bytes(b"not a real document")

    result = classify_file(path)

    assert result.kind is FileKind.SUPPORTING
    assert result.matched_sheet is None
    assert result.path == str(path)


def test_excel_without_known_sheet_is_unknown(tmp_path):
    path = _xlsx_with_sheets(tmp_path / "其他試算表.xlsx", "Sheet1")

    result = classify_file(path)

    assert result.kind is FileKind.UNKNOWN
    assert result.matched_sheet is None
    assert "Sheet1" in result.reason


def test_corrupt_excel_is_unknown_instead_of_raising(tmp_path):
    path = tmp_path / "bad.xlsx"
    path.write_text("this is not an excel file")

    result = classify_file(path)

    assert result.kind is FileKind.UNKNOWN
    assert "無法開啟" in result.reason


def test_classify_fileobj_uses_the_same_extension_gate():
    result = classify_fileobj(io.BytesIO(b"not opened"), "evidence.pdf")

    assert result.kind is FileKind.SUPPORTING
    assert result.path is None


def test_classify_fileobj_reads_excel_from_memory():
    buffer = io.BytesIO()
    workbook = openpyxl.Workbook()
    workbook.active.title = F03_SHEET
    workbook.save(buffer)
    buffer.seek(0)

    result = classify_fileobj(buffer, "看似佐證.xlsx")

    assert result.kind is FileKind.F03
    assert result.matched_sheet == F03_SHEET


@pytest.mark.parametrize(
    ("sheet_name", "misleading_name", "expected_kind"),
    [
        (F01_SHEET, "風險評鑑_最終版.xlsx", FileKind.F01),
        (ASSESS_SHEET, "F01_系統資訊表.xlsx", FileKind.F02),
        (F03_SHEET, "資料佐證.xlsx", FileKind.F03),
    ],
)
def test_known_form_is_classified_by_sheet_not_filename(
    tmp_path,
    sheet_name,
    misleading_name,
    expected_kind,
):
    path = _xlsx_with_sheets(tmp_path / misleading_name, sheet_name)

    result = classify_file(path)

    assert result.kind is expected_kind
    assert result.matched_sheet == sheet_name


def test_f02_sheet_has_priority_when_workbook_contains_multiple_known_sheets(tmp_path):
    path = _xlsx_with_sheets(
        tmp_path / "混合.xlsx",
        F03_SHEET,
        F01_SHEET,
        ASSESS_SHEET,
    )

    assert classify_file(path).kind is FileKind.F02


def test_classify_files_preserves_input_order(tmp_path):
    paths = [
        _xlsx_with_sheets(tmp_path / "a.xlsx", F03_SHEET),
        tmp_path / "b.pdf",
        _xlsx_with_sheets(tmp_path / "c.xlsx", F01_SHEET),
    ]
    paths[1].write_bytes(b"%PDF")

    results = classify_files(paths)

    assert [result.kind for result in results] == [
        FileKind.F03,
        FileKind.SUPPORTING,
        FileKind.F01,
    ]


# ── 路由邏輯 ──────────────────────────────────────────────────────────


def test_route_includes_summary_and_routes_supporting_and_unknown():
    results = [
        _classification(FileKind.F01, "f01.xlsx", "/t/f01.xlsx"),
        _classification(FileKind.SUPPORTING, "R07_公平性.pdf", "/t/R07.pdf"),
        FileClassification(
            filename="weird.xlsx",
            kind=FileKind.UNKNOWN,
            reason="無已知分頁",
        ),
    ]

    files, supporting, findings = route_classifications(results)

    assert files == {"f01": "/t/f01.xlsx"}
    assert supporting == ["R07_公平性.pdf"]
    assert findings[0].code == "CLASSIFY.SUMMARY"
    assert findings[0].severity.value == "info"
    assert "CLASSIFY.UNRECOGNIZED" in {finding.code for finding in findings}


def test_route_duplicate_form_keeps_first_and_warns():
    results = [
        _classification(FileKind.F02, "first.xlsm", "/t/first.xlsm"),
        _classification(FileKind.F02, "second.xlsm", "/t/second.xlsm"),
    ]

    files, _, findings = route_classifications(results)

    assert files == {"f02": "/t/first.xlsm"}
    duplicate = next(
        finding for finding in findings if finding.code == "CLASSIFY.DUPLICATE_F02"
    )
    assert duplicate.severity.value == "warn"
    assert "first.xlsm" in duplicate.message
    assert "second.xlsm" in duplicate.message


def test_route_unknown_is_never_sent_to_a_parser():
    result = FileClassification(
        filename="x.xlsx",
        kind=FileKind.UNKNOWN,
        reason="無已知分頁",
    )

    files, supporting, findings = route_classifications([result])

    assert files == {}
    assert supporting == []
    assert {finding.code for finding in findings} == {
        "CLASSIFY.SUMMARY",
        "CLASSIFY.UNRECOGNIZED",
    }


@pytest.mark.parametrize("kind", [FileKind.F01, FileKind.SUPPORTING])
def test_route_pathless_assignable_file_is_not_silently_counted(kind):
    result = FileClassification(
        filename="preview-only.xlsx",
        kind=kind,
        reason="記憶體預覽",
    )

    files, supporting, findings = route_classifications([result])

    assert files == {}
    assert supporting == []
    assert "CLASSIFY.MISSING_PATH" in {finding.code for finding in findings}


def test_route_records_ignored_file():
    result = FileClassification(
        filename="unknown.xlsx",
        kind=FileKind.IGNORED,
        reason="使用者確認忽略",
    )

    files, supporting, findings = route_classifications([result])

    assert files == {}
    assert supporting == []
    ignored = next(finding for finding in findings if finding.code == "CLASSIFY.IGNORED")
    assert ignored.severity.value == "warn"
    assert "未送入" in ignored.message
    assert "共 1 個檔案" in findings[0].message


# ── 審查編排 ──────────────────────────────────────────────────────────


def test_review_files_returns_report_and_classifications(tmp_path):
    path = tmp_path / "佐證.pdf"
    path.write_bytes(b"%PDF")

    report, classifications = review_files([path])

    assert classifications[0].kind is FileKind.SUPPORTING
    assert "CLASSIFY.SUMMARY" in _codes(report)
    assert "DOC.MISSING_F01" in _codes(report)
    assert "DOC.MISSING_F03" in _codes(report)
    assert report.decision_status.value == "changes_required"


def test_review_files_unknown_excel_adds_warning_without_parsing_it(tmp_path):
    path = _xlsx_with_sheets(tmp_path / "其他.xlsx", "Sheet1")

    report, classifications = review_files([path])

    assert classifications[0].kind is FileKind.UNKNOWN
    assert "CLASSIFY.UNRECOGNIZED" in _codes(report)
    assert "F01.PARSE_ERROR" not in _codes(report)
    assert report.decision_status.value == "changes_required"


def test_review_routed_classification_warning_controls_decision_status():
    warning = route_classifications(
        [
            _classification(FileKind.F02, "first.xlsm", "/t/first.xlsm"),
            _classification(FileKind.F02, "second.xlsm", "/t/second.xlsm"),
        ]
    )[2]

    report = review_routed({}, class_findings=warning)

    assert "SUBMISSION.OK" not in _codes(report)
    assert report.decision_status.value == "changes_required"


# ── 官方範本端到端 ────────────────────────────────────────────────────


@requires_templates
def test_review_files_mixed_unordered_bundle_uses_current_decision_status(tmp_path):
    from tests.test_f02_rules import BASELINE

    evidence = tmp_path / "R06.txt"
    evidence.write_text("模型透明性與可解釋性佐證")
    paths = [
        build_f03_fixture(tmp_path / "f03.xlsx"),
        evidence,
        build_f02_fixture(tmp_path / "f02.xlsm", answers=BASELINE, cached_grade="低"),
        build_f01_fixture(tmp_path / "f01.xlsx"),
    ]

    report, results = review_files(paths)

    assert "CLASSIFY.SUMMARY" in _codes(report)
    assert "SUBMISSION.OK" in _codes(report)
    assert report.error_count == 0
    assert report.warn_count == 0
    # F03 的實質內容尚未跑 LLM，不可因規則全綠就標為 approved。
    assert report.decision_status.value == "manual_review"
    assert {result.kind for result in results} == {
        FileKind.F01,
        FileKind.F02,
        FileKind.F03,
        FileKind.SUPPORTING,
    }


@requires_templates
def test_review_files_duplicate_form_requires_changes(tmp_path):
    from tests.test_f02_rules import BASELINE

    paths = [
        build_f01_fixture(tmp_path / "f01.xlsx"),
        build_f02_fixture(tmp_path / "f02_a.xlsm", answers=BASELINE, cached_grade="低"),
        build_f02_fixture(tmp_path / "f02_b.xlsm", answers=BASELINE, cached_grade="低"),
        build_f03_fixture(tmp_path / "f03.xlsx"),
    ]

    report, _ = review_files(paths)

    assert "CLASSIFY.DUPLICATE_F02" in _codes(report)
    assert "SUBMISSION.OK" not in _codes(report)
    assert report.decision_status.value == "changes_required"
