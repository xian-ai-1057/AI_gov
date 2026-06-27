"""缺件檢查測試：核心三表 + 條件式佐證（檔名關鍵字）。

直接以 Submission 模型驅動 missing_docs.run_all，ground truth 為哪些 DOC.* 該觸發。
"""

from govcheck.checks.rule import missing_docs
from govcheck.models import FilePresence, Severity, Submission


def codes(sub) -> set[str]:
    return {f.code for f in missing_docs.run_all(sub)}


def _sub(f01=True, f02=True, f03=True, risk=None, docs=None):
    return Submission(
        presence=FilePresence(f01=f01, f02=f02, f03=f03),
        risk_grade=risk,
        supporting_docs=docs or [],
    )


def test_all_core_present_no_missing():
    assert codes(_sub()) == set()  # risk None → 無條件式佐證


def test_missing_f01_error():
    fd = [f for f in missing_docs.run_all(_sub(f01=False)) if f.code == "DOC.MISSING_F01"]
    assert fd and fd[0].severity is Severity.ERROR


def test_missing_f03_error():
    assert "DOC.MISSING_F03" in codes(_sub(f03=False))


def test_missing_f02_warn():
    fd = [f for f in missing_docs.run_all(_sub(f02=False)) if f.code == "DOC.MISSING_F02"]
    assert fd and fd[0].severity is Severity.WARN


def test_high_risk_missing_conditional_docs():
    c = codes(_sub(risk="高"))
    assert "DOC.MISSING_R06" in c and "DOC.MISSING_R07" in c


def test_conditional_docs_satisfied_by_filename():
    c = codes(_sub(risk="高", docs=["公平性測試報告.pdf", "可解釋性說明.docx"]))
    assert "DOC.MISSING_R06" not in c and "DOC.MISSING_R07" not in c


def test_low_risk_no_conditional():
    c = codes(_sub(risk="低"))
    assert not any(x.startswith("DOC.MISSING_R") for x in c)


def test_unknown_risk_no_conditional():
    # 缺 F02（risk None）→ 不報條件式佐證（無法判風險）
    c = codes(_sub(f02=False, risk=None))
    assert not any(x.startswith("DOC.MISSING_R") for x in c)


def test_r05_on_mid_high_risk():
    # R05（委外）不再依不可靠的「串接」訊號，改與 R06/R07 一致依中/高風險
    assert "DOC.MISSING_R05" in codes(_sub(risk="高"))
    assert "DOC.MISSING_R05" not in codes(_sub(risk="低"))


def test_r05_satisfied_by_filename():
    assert "DOC.MISSING_R05" not in codes(_sub(risk="高", docs=["委外受託機構技術文件.pdf"]))
