"""跨表一致性測試：owner（F01.H↔F03.B1）/ 單位（F01.A↔F02.N2）/ F01 內部。

直接以模型驅動 cross_consistency.run_all。重點：任一側空一律 skip（避免模板示範列誤報），
字串正規化（去空白、casefold）後比對。
"""

from govcheck.checks.rule import cross_consistency
from govcheck.models import F01ApplicationRow, F01Form, F01SubRef, F03Identity, Submission


def codes(sub) -> set[str]:
    return {f.code for f in cross_consistency.run_all(sub)}


def _f01(owner=None, unit=None, project=None, sub_refs=None):
    row = F01ApplicationRow(row_index=4, system_owner=owner, filing_unit=unit, project_name=project)
    return F01Form(subject=project, rows=[row], sub_refs=sub_refs or [])


# ---- owner：F01.H ↔ F03.B1 ----
def test_owner_match_no_finding():
    sub = Submission(f01=_f01(owner="李大華"), f03=F03Identity(system_owner="李大華"))
    assert "CROSS.OWNER_MISMATCH" not in codes(sub)


def test_owner_mismatch():
    sub = Submission(f01=_f01(owner="李大華"), f03=F03Identity(system_owner="王五"))
    assert "CROSS.OWNER_MISMATCH" in codes(sub)


def test_owner_empty_side_skipped():
    sub = Submission(f01=_f01(owner="李大華"), f03=F03Identity(system_owner=None))
    assert "CROSS.OWNER_MISMATCH" not in codes(sub)


def test_owner_normalization_whitespace():
    sub = Submission(f01=_f01(owner="客服 小幫手"), f03=F03Identity(system_owner="客服小幫手"))
    assert "CROSS.OWNER_MISMATCH" not in codes(sub)


# ---- 單位：F01.A ↔ F02 N2 ----
def test_unit_mismatch():
    sub = Submission(f01=_f01(unit="甲單位"), f02_filing_unit="乙單位")
    assert "CROSS.UNIT_MISMATCH" in codes(sub)


def test_unit_f02_missing_skipped():
    sub = Submission(f01=_f01(unit="甲單位"), f02_filing_unit=None)
    assert "CROSS.UNIT_MISMATCH" not in codes(sub)


# ---- F01 內部：主表 D ↔ 附屬表對應欄 ----
def test_f01_internal_mismatch():
    refs = [F01SubRef(sheet="資料", row_index=5, corr_project_name="別的系統")]
    sub = Submission(f01=_f01(project="智能客服小幫手", sub_refs=refs))
    assert "CROSS.F01_INTERNAL_NAME" in codes(sub)


def test_f01_internal_empty_corr_skipped():
    # 模板示範列：對應欄空 → parser 不收 → 不報（關鍵回歸）
    sub = Submission(f01=_f01(project="智能客服小幫手", sub_refs=[]))
    assert "CROSS.F01_INTERNAL_NAME" not in codes(sub)


def test_f01_internal_match_no_finding():
    refs = [F01SubRef(sheet="模型", row_index=5, corr_project_name="智能客服小幫手")]
    sub = Submission(f01=_f01(project="智能客服小幫手", sub_refs=refs))
    assert "CROSS.F01_INTERNAL_NAME" not in codes(sub)


# ---- 多應用（多列）回歸：不可只看 rows[0] ----
def _f01_multi(rows_kwargs, sub_refs=None):
    rows = [F01ApplicationRow(row_index=4 + i, **kw) for i, kw in enumerate(rows_kwargs)]
    return F01Form(subject=rows[0].project_name, rows=rows, sub_refs=sub_refs or [])


def test_owner_multirow_f03_matches_one():
    # F03 owner 對得上第二個應用 → 不應誤報
    f01 = _f01_multi([{"system_owner": "李大華"}, {"system_owner": "王五"}])
    sub = Submission(f01=f01, f03=F03Identity(system_owner="王五"))
    assert "CROSS.OWNER_MISMATCH" not in codes(sub)


def test_owner_multirow_f03_matches_none():
    f01 = _f01_multi([{"system_owner": "李大華"}, {"system_owner": "王五"}])
    sub = Submission(f01=f01, f03=F03Identity(system_owner="陳七"))
    assert "CROSS.OWNER_MISMATCH" in codes(sub)


def test_f01_internal_multirow_matches_second_app():
    # 附屬表對應第二個應用名稱 → 應對得上、不誤報
    f01 = _f01_multi(
        [{"project_name": "客服小幫手"}, {"project_name": "風控模型"}],
        sub_refs=[F01SubRef(sheet="模型", row_index=5, corr_project_name="風控模型")],
    )
    assert "CROSS.F01_INTERNAL_NAME" not in codes(Submission(f01=f01))
