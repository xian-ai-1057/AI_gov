"""送件包端到端編排測試 + 回歸保護。

用真檔 fixture 走完整 review_submission（parser + 缺件 + F01 必填 + F02 規則 + 跨表），
並確認 Phase 1 的 review_f02 入口不受影響、解析錯誤不丟例外。
"""

from govcheck.review.engine import review_f02, review_submission
from tests.fixture_builder import (
    F01_DEFAULT_ROW,
    build_f01_fixture,
    build_f02_fixture,
    build_f03_fixture,
)
from tests.test_f02_rules import BASELINE  # 重用合規低風險答案集


def codes(report) -> set[str]:
    return {f.code for f in report.findings}


def _compliant_files(tmp_path):
    return {
        "f01": build_f01_fixture(tmp_path / "f01.xlsx"),
        "f02": build_f02_fixture(tmp_path / "f02.xlsm", answers=BASELINE, cached_grade="低"),
        "f03": build_f03_fixture(tmp_path / "f03.xlsx"),  # owner 預設 李大華 = F01 H
    }


def test_compliant_submission_ok(tmp_path):
    report = review_submission(_compliant_files(tmp_path))
    assert codes(report) == {"SUBMISSION.OK"}


def test_missing_f02_and_f01_required(tmp_path):
    row = dict(F01_DEFAULT_ROW)
    row["D"] = None  # F01 缺專案名稱
    files = {
        "f01": build_f01_fixture(tmp_path / "f01.xlsx", rows=[row]),
        "f03": build_f03_fixture(tmp_path / "f03.xlsx"),
    }  # 刻意缺 F02
    c = codes(review_submission(files))
    assert "DOC.MISSING_F02" in c
    assert "F01.REQUIRED_MISSING" in c
    assert "DOC.MISSING_F01" not in c  # F01 有上傳


def test_parse_error_does_not_crash(tmp_path):
    bad = tmp_path / "bad.xlsx"
    bad.write_text("this is not an excel file")
    report = review_submission({"f01": bad})  # 不應丟例外
    assert "F01.PARSE_ERROR" in codes(report)


def test_review_f02_regression(tmp_path):
    # Phase 1 入口不受影響
    f02 = build_f02_fixture(tmp_path / "f02.xlsm", answers=BASELINE, cached_grade="低")
    assert "F02.OK" in {f.code for f in review_f02(f02).findings}


def test_f03_owner_placeholder_filtered(tmp_path):
    # F03 B1 留範本預設 "XXX" → parser 過濾為 None → 不誤報跨表 owner 不一致
    from govcheck.parsers.f03_parser import parse_f03_identity
    f03 = build_f03_fixture(tmp_path / "f03.xlsx", system_owner=None)  # 不覆寫 B1
    assert parse_f03_identity(f03).system_owner is None


def test_f02_filing_unit_read_into_model(tmp_path):
    # parse_f02 直接讀 N2，免 engine 重複開檔
    from govcheck.parsers.f02_parser import parse_f02
    f02 = build_f02_fixture(
        tmp_path / "f02.xlsm", answers=BASELINE, cached_grade="低", filing_unit="數位金融處/AI應用部",
    )
    assert parse_f02(f02).filing_unit == "數位金融處/AI應用部"
