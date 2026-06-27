"""F01 必填規則測試：正例（合規列不觸發）+ 反例（缺欄／空表觸發）。

fixture 由官方 .xlsx 程式化生成，跑 parse_f01 → f01_rules.run_all，
比對回報的 Finding 代碼集合——ground truth 皆已知。
"""

from govcheck.checks.rule import f01_rules
from govcheck.parsers.f01_parser import parse_f01
from tests.fixture_builder import F01_DEFAULT_ROW, build_f01_fixture


def codes(path) -> set[str]:
    return {f.code for f in f01_rules.run_all(parse_f01(path))}


def test_full_row_passes(tmp_path):
    f = build_f01_fixture(tmp_path / "ok.xlsx")  # 預設 A~I 皆填
    assert codes(f) == set()


def test_required_missing_negative(tmp_path):
    row = dict(F01_DEFAULT_ROW)
    row["H"] = None  # System Owner 留空
    row["I"] = None  # AP Owner 留空
    f = build_f01_fixture(tmp_path / "miss.xlsx", rows=[row])
    found = [fd for fd in f01_rules.run_all(parse_f01(f)) if fd.code == "F01.REQUIRED_MISSING"]
    assert found, "應報 F01.REQUIRED_MISSING"
    assert "System Owner" in found[0].actual and "AP Owner" in found[0].actual


def test_no_data_row_negative(tmp_path):
    f = build_f01_fixture(tmp_path / "empty.xlsx", rows=[])  # 主表全空
    assert "F01.NO_DATA_ROW" in codes(f)


def test_multi_row_only_offending_reported(tmp_path):
    full = dict(F01_DEFAULT_ROW)
    bad = dict(F01_DEFAULT_ROW)
    bad["D"] = None  # 第二列缺專案名稱
    f = build_f01_fixture(tmp_path / "multi.xlsx", rows=[full, bad])
    findings = [fd for fd in f01_rules.run_all(parse_f01(f)) if fd.code == "F01.REQUIRED_MISSING"]
    assert len(findings) == 1
    assert "第 5 列" in findings[0].location  # 第二列 = Excel row 5


def test_optional_columns_blank_no_error(tmp_path):
    row = dict(F01_DEFAULT_ROW)
    row["J"] = row["K"] = row["L"] = None  # J/K/L 非必填
    f = build_f01_fixture(tmp_path / "opt.xlsx", rows=[row])
    assert "F01.REQUIRED_MISSING" not in codes(f)
