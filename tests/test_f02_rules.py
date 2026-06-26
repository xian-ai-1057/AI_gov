"""F02 規則檢查測試：每條規則一組正例（不該觸發）+ 反例（該觸發）。

fixture 由官方 .xlsm 程式化生成（fixture_builder），跑完整 engine，
比對回報的 Finding 代碼集合——「該錯在哪、該報什麼」皆已知。
"""

from govcheck.review.engine import review_f02
from tests.fixture_builder import build_f02_fixture

# 合規的低風險基線：每單選恰一 Y、複選有 Y、所有題已填 → 不應有任何 error
BASELINE = {
    "UC-01": "N", "UC-02": "N", "UC-03": "N",
    "UC-04-01": "N", "UC-04-02": "N", "UC-04-03": "N", "UC-04-04": "Y",
    "UC-05-01": "N", "UC-05-02": "N", "UC-05-03": "N", "UC-05-04": "Y",
    "UC-06-01": "N", "UC-06-02": "N", "UC-06-03": "Y",
    "UC-07": "Y",
    "D-01-01": "N", "D-01-02": "N", "D-01-03": "N", "D-01-04": "Y",
    "D-02-01": "Y", "D-02-02": "N", "D-02-03": "N", "D-02-04": "N",
    "D-03": "N", "D-04": "Y", "M-01": "Y", "M-02": "Y",
    "M-03-01": "Y", "M-03-02": "N",
}

# 高風險答案集（手算 grade=高），用於續填規則
HIGH_RISK = {
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


def codes(path) -> set[str]:
    return {f.code for f in review_f02(path).findings}


def make(tmp_path, name, **kwargs):
    answers = {**BASELINE, **kwargs.pop("answers", {})}
    return build_f02_fixture(tmp_path / f"{name}.xlsm", answers=answers, **kwargs)


# ---------- 正例：合規基線無 error ----------
def test_baseline_passes(tmp_path):
    # 模擬正常 Excel 存檔（含快取分級「低」），避免無快取的 CACHE_MISSING 提醒
    f = make(tmp_path, "baseline", cached_grade="低")
    found = codes(f)
    other = {c for c in found if not c.endswith(".OK")}
    # 基線不應有任何 error/warn（只允許 INFO 的 F02.OK）
    assert "F02.OK" in found
    assert other == set()


# ---------- 單選唯一性 ----------
def test_single_choice_two_yes_negative(tmp_path):
    f = make(tmp_path, "sc_two", answers={"UC-04-01": "Y", "UC-04-04": "Y"})
    assert "F02.SINGLE_CHOICE" in codes(f)


def test_single_choice_zero_yes_negative(tmp_path):
    # 四項皆 N（已填、非漏填）→ 0 個 Y 違反單選
    f = make(tmp_path, "sc_zero",
             answers={"UC-04-01": "N", "UC-04-02": "N", "UC-04-03": "N", "UC-04-04": "N"})
    assert "F02.SINGLE_CHOICE" in codes(f)


def test_single_choice_one_yes_positive(tmp_path):
    f = make(tmp_path, "sc_one")  # baseline UC-04-04=Y 恰一個
    assert "F02.SINGLE_CHOICE" not in codes(f)


# ---------- 複選非空 ----------
def test_multi_choice_all_no_negative(tmp_path):
    f = make(tmp_path, "mc_none",
             answers={"D-02-01": "N", "D-02-02": "N", "D-02-03": "N", "D-02-04": "N"})
    assert "F02.MULTI_CHOICE" in codes(f)


def test_multi_choice_positive(tmp_path):
    f = make(tmp_path, "mc_ok")  # baseline D-02-01=Y
    assert "F02.MULTI_CHOICE" not in codes(f)


# ---------- 系列題完整性 ----------
def test_series_incomplete_negative(tmp_path):
    # 留空 UC-05-03（部分答案集，不覆寫該格）
    partial = {k: v for k, v in BASELINE.items() if k != "UC-05-03"}
    f = build_f02_fixture(tmp_path / "series_gap.xlsm", answers=partial)
    assert "F02.SERIES_INCOMPLETE" in codes(f)


def test_series_complete_positive(tmp_path):
    f = make(tmp_path, "series_ok")
    assert "F02.SERIES_INCOMPLETE" not in codes(f)


# ---------- 一般題完整性 ----------
def test_general_incomplete_negative(tmp_path):
    # 留空一般題 M-02 → 應報缺漏（M-02 為必填、反向計分，留空會低估風險）
    partial = {k: v for k, v in BASELINE.items() if k != "M-02"}
    f = build_f02_fixture(tmp_path / "gen_gap.xlsm", answers=partial)
    assert "F02.GENERAL_INCOMPLETE" in codes(f)


def test_general_complete_positive(tmp_path):
    f = make(tmp_path, "gen_ok")  # baseline 一般題皆已填
    assert "F02.GENERAL_INCOMPLETE" not in codes(f)


def test_reverse_scored_m01_m02_no_false_positive(tmp_path):
    # 回歸：M-01=N 且 M-02=N 是合法常見組合（服務外購、模型亦非自建），
    # 不應產生任何條件式誤判；舊 F02.CONDITIONAL 規則已移除
    answers = {**BASELINE, "M-01": "N", "M-02": "N"}
    f = build_f02_fixture(tmp_path / "rev_ok.xlsm", answers=answers, cached_grade="低")
    found = codes(f)
    assert "F02.CONDITIONAL" not in found
    assert "F02.GENERAL_INCOMPLETE" not in found


# ---------- 計分比對 ----------
def test_score_mismatch_negative(tmp_path):
    # 基線重算為「低」，但偽造檔內快取分級為「高」
    f = build_f02_fixture(tmp_path / "score_bad.xlsm", answers=BASELINE, cached_grade="高")
    assert "F02.GRADE_MISMATCH" in codes(f)


def test_score_consistent_positive(tmp_path):
    # 設定與重算一致的快取分級「低」
    f = build_f02_fixture(tmp_path / "score_ok.xlsm", answers=BASELINE, cached_grade="低")
    assert "F02.GRADE_MISMATCH" not in codes(f)


# ---------- 續填：剩餘風險評鑑 ----------
def test_residual_missing_negative(tmp_path):
    f = build_f02_fixture(tmp_path / "resid_miss.xlsm", answers=HIGH_RISK)
    assert "F02.RESIDUAL_MISSING" in codes(f)


def test_residual_filled_positive(tmp_path):
    # 高風險但已填剩餘風險表（分數 ≤6，不需處理計畫）
    f = build_f02_fixture(
        tmp_path / "resid_ok.xlsm", answers=HIGH_RISK,
        residual_rows=[["控管要求A", "完整", "已設存取控管", "強", 12, 4]],
    )
    found = codes(f)
    assert "F02.RESIDUAL_MISSING" not in found
    assert "F02.TREATMENT_MISSING" not in found


# ---------- 續填：風險處理計畫 ----------
def test_treatment_missing_negative(tmp_path):
    # 高風險 + 剩餘分數 8 (>6) 但未填處理計畫
    f = build_f02_fixture(
        tmp_path / "treat_miss.xlsm", answers=HIGH_RISK,
        residual_rows=[["控管要求A", "部分", "說明", "中", 12, 8]],
    )
    assert "F02.TREATMENT_MISSING" in codes(f)


def test_treatment_filled_positive(tmp_path):
    f = build_f02_fixture(
        tmp_path / "treat_ok.xlsm", answers=HIGH_RISK,
        residual_rows=[["控管要求A", "部分", "說明", "中", 12, 8]],
        treatment_rows=[[12, "控管要求A", "現狀", "中", 8, "降低", "導入X控制", "資安部", "2026-12-31", "進行中"]],
    )
    assert "F02.TREATMENT_MISSING" not in codes(f)
