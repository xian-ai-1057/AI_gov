"""F02 計分引擎測試 —— 每個案例都有獨立算出的正確答案（ground truth）。

- 邊界：直接驗分級門檻。
- 真檔：官方空白範本經 Excel 算出的快取分級（低/0）即正確答案，重算須一致。
- 手算案例：依參照表獨立手算四域百分比與分級，斷言引擎吻合。
"""

import pytest

from govcheck.models import F02Form
from govcheck.parsers.f02_parser import parse_f02
from govcheck.scoring.f02_score import grade_for, load_config, recompute

CFG = load_config()


# ---- 邊界：分級門檻 <50 低 / <75 中 / 否則 高 ----
@pytest.mark.parametrize("overall,grade", [
    (0, "低"), (49.999, "低"), (50, "中"), (74.999, "中"), (75, "高"), (100, "高"),
])
def test_grade_thresholds(overall, grade):
    assert grade_for(overall, CFG) == grade


# ---- 真檔 ground truth：空白官方範本 → Excel 快取為 0/低 ----
def test_blank_official_matches_cached_grade(official_f02):
    form = parse_f02(official_f02)
    result = recompute(form)
    assert form.cached.grade == "低"
    assert result.grade == "低"
    assert result.overall == pytest.approx(0.0)


# Case A：手算 ground truth（無加成）
CASE_A = {
    "UC-01": "Y", "UC-02": "N", "UC-03": "N",
    "UC-04-01": "Y", "UC-04-02": "N", "UC-04-03": "N", "UC-04-04": "N",
    "UC-05-01": "Y", "UC-05-02": "N", "UC-05-03": "N", "UC-05-04": "N",
    "UC-06-01": "Y", "UC-06-02": "N", "UC-06-03": "N",
    "UC-07": "N",
    "D-01-01": "Y", "D-01-02": "N", "D-01-03": "N", "D-01-04": "N",
    "D-02-01": "Y", "D-02-02": "N", "D-02-03": "N", "D-02-04": "N",
    "D-03": "N", "D-04": "N", "M-01": "N", "M-02": "N",
    "M-03-01": "N", "M-03-02": "Y",
}
# 手算加成前總分：財8.5 / 作11.5 / 商13.0 / 合8.0；分母 9/16/13.5/9
CASE_A_PCT = {
    "finance": 8.5 / 9 * 100,        # 94.44
    "operation": 11.5 / 16 * 100,    # 71.875
    "reputation": 13.0 / 13.5 * 100, # 96.30
    "compliance": 8.0 / 9 * 100,     # 88.89
}


def test_case_a_no_uplift():
    result = recompute(F02Form(answers=CASE_A), CFG)
    for d, expected in CASE_A_PCT.items():
        assert getattr(result.percentages, d) == pytest.approx(expected, abs=0.01)
    assert result.overall == pytest.approx(96.296, abs=0.01)
    assert result.grade == "高"  # 96.3 ≥ 75


def test_case_a_with_uplift_factor1():
    # factor1=Y → 乘數 0.8，各域 ×0.8
    result = recompute(F02Form(answers=CASE_A, uplift={"factor1": "Y"}), CFG)
    assert getattr(result.percentages, "finance") == pytest.approx(8.5 * 0.8 / 9 * 100, abs=0.01)
    assert result.overall == pytest.approx(96.296 * 0.8, abs=0.05)
    assert result.grade == "高"


def test_reverse_scored_questions():
    # UC-07/D-04/M-01/M-02 在答 N 時計分；全 N 應有分，全 Y 應為 0
    n_form = F02Form(answers={"UC-07": "N", "D-04": "N", "M-01": "N", "M-02": "N"})
    y_form = F02Form(answers={"UC-07": "Y", "D-04": "Y", "M-01": "Y", "M-02": "Y"})
    assert recompute(n_form, CFG).overall > 0
    assert recompute(y_form, CFG).overall == pytest.approx(0.0)


def test_low_risk_single_question():
    result = recompute(F02Form(answers={"UC-01": "Y"}), CFG)
    # 財1/9, 商2/13.5, 合1/9 → max = 商 14.81 → 低
    assert result.overall == pytest.approx(2 / 13.5 * 100, abs=0.01)
    assert result.grade == "低"
