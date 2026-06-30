"""送件包端到端編排測試 + 回歸保護。

用真檔 fixture 走完整 review_submission（parser + 缺件 + F01 必填 + F02 規則 + 跨表），
並確認 Phase 1 的 review_f02 入口不受影響、解析錯誤不丟例外。
"""

from govcheck.review.engine import review_f02, review_routed, review_submission
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


def test_progress_events_sequence(tmp_path):
    """三表齊全（不啟用 LLM）→ parse 與 rules 兩階段事件依序 emit，done 單調遞增至 total。"""
    events: list[dict] = []
    review_submission(_compliant_files(tmp_path), progress=events.append)

    parse = [e for e in events if e["stage"] == "parse"]
    rules = [e for e in events if e["stage"] == "rules"]
    assert [e["stage"] for e in events] == ["parse"] * 3 + ["rules"] * 5  # 階段順序：先解析後規則
    assert [e["done"] for e in parse] == [1, 2, 3] and all(e["total"] == 3 for e in parse)
    # 規則總步 = 缺件 + 跨表（恆跑）+ F01 + F02 + F03 檢核（三表皆在）= 5
    assert [e["done"] for e in rules] == [1, 2, 3, 4, 5] and all(e["total"] == 5 for e in rules)
    assert not [e for e in events if e["stage"] == "llm"]  # 預設不啟用 LLM


def test_progress_does_not_change_findings(tmp_path):
    """回歸保護：傳不傳 progress，審查結果（findings）必須完全一致（review_submission 與 review_routed 皆然）。"""
    files = _compliant_files(tmp_path)
    without = [(f.code, f.severity) for f in review_submission(files).findings]
    with_cb = [(f.code, f.severity) for f in review_submission(files, progress=lambda _ev: None).findings]
    assert without == with_cb

    routed_without = [(f.code, f.severity) for f in review_routed({"f02": files["f02"]}).findings]
    routed_events: list[dict] = []
    routed_with = [
        (f.code, f.severity)
        for f in review_routed({"f02": files["f02"]}, progress=routed_events.append).findings
    ]
    assert routed_without == routed_with
    assert routed_events  # review_routed 確實把 progress 透傳到底層
