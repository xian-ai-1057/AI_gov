"""F02 規則檢查（確定性、零 LLM）。

每個函式對一條規則，回傳 0..N 個 Finding。run_all() 把它們串起來，
供 engine 呼叫。所有判斷只依賴 F02Form 與 config，可單元測試、不需網路。
"""

from __future__ import annotations

from govcheck.models import F02Form, Finding, Severity
from govcheck.scoring.f02_score import load_config, recompute

# 重算 vs 快取 比對的容許誤差（百分比）
SCORE_TOLERANCE = 0.5
# 剩餘風險分數續填處理計畫的門檻
TREATMENT_THRESHOLD = 6


def run_all(form: F02Form, cfg: dict | None = None) -> list[Finding]:
    cfg = cfg or load_config()
    findings: list[Finding] = []
    findings += check_series_completeness(form, cfg)
    findings += check_single_choice(form, cfg)
    findings += check_multi_choice(form, cfg)
    findings += check_conditional(form, cfg)
    findings += check_score_consistency(form, cfg)
    findings += check_followup_sheets(form, cfg)
    return findings


def _group_members(prefix: str, count: int, cfg: dict) -> list[str]:
    """單/複選題的子項題號清單，如 UC-04 → UC-04-01..UC-04-04。"""
    return [f"{prefix}-{i:02d}" for i in range(1, count + 1)]


def check_series_completeness(form: F02Form, cfg: dict) -> list[Finding]:
    """系列題（單選/複選）每個子項都要有 Y/N，不可留空。"""
    findings = []
    groups = {**cfg["groups"]["single_choice"], **cfg["groups"]["multi_choice"]}
    for prefix, count in groups.items():
        members = _group_members(prefix, count, cfg)
        missing = [m for m in members if form.answers.get(m) is None]
        if missing:
            findings.append(Finding(
                severity=Severity.ERROR,
                code="F02.SERIES_INCOMPLETE",
                title=f"{prefix} 系列題有子項未填",
                message=f"{prefix} 應每個子項都填 Y 或 N，未填：{', '.join(missing)}。",
                location=prefix,
                expected="每子項皆 Y/N",
                actual=f"未填 {len(missing)} 項",
            ))
    return findings


def check_single_choice(form: F02Form, cfg: dict) -> list[Finding]:
    """單選題：子項中 Y 的數量必須恰好為 1。"""
    findings = []
    for prefix, count in cfg["groups"]["single_choice"].items():
        members = _group_members(prefix, count, cfg)
        yes = [m for m in members if form.answers.get(m) == "Y"]
        # 全部未填的情況留給 completeness 規則，避免重複報
        if all(form.answers.get(m) is None for m in members):
            continue
        if len(yes) != 1:
            findings.append(Finding(
                severity=Severity.ERROR,
                code="F02.SINGLE_CHOICE",
                title=f"{prefix} 單選題違規",
                message=(f"{prefix} 為單選題，只能有一個 Y；"
                         f"目前有 {len(yes)} 個 Y（{', '.join(yes) or '無'}）。"),
                location=prefix,
                expected="恰 1 個 Y",
                actual=f"{len(yes)} 個 Y",
            ))
    return findings


def check_multi_choice(form: F02Form, cfg: dict) -> list[Finding]:
    """複選題：至少要有一個 Y。"""
    findings = []
    for prefix, count in cfg["groups"]["multi_choice"].items():
        members = _group_members(prefix, count, cfg)
        if all(form.answers.get(m) is None for m in members):
            continue  # 完整性規則會報
        yes = [m for m in members if form.answers.get(m) == "Y"]
        if not yes:
            findings.append(Finding(
                severity=Severity.ERROR,
                code="F02.MULTI_CHOICE",
                title=f"{prefix} 複選題未選任何項",
                message=f"{prefix} 為複選題，至少需有一個 Y，目前全為 N。",
                location=prefix,
                expected="≥ 1 個 Y",
                actual="0 個 Y",
            ))
    return findings


def check_conditional(form: F02Form, cfg: dict) -> list[Finding]:
    """條件式依賴：如 M-02 僅在 M-01=Y 時才有意義。"""
    findings = []
    for qid, rule in cfg.get("conditional", {}).items():
        dep, expect = rule["depends_on"], rule["expect"]
        dep_ans = form.answers.get(dep)
        this_ans = form.answers.get(qid)
        if dep_ans == expect and this_ans is None:
            findings.append(Finding(
                severity=Severity.WARN,
                code="F02.CONDITIONAL",
                title=f"{qid} 依賴 {dep} 但未填",
                message=f"{dep}={expect} 時，{qid} 應一併填寫，目前為空。",
                location=qid,
                expected=f"{dep}={expect} 時須填 {qid}",
                actual="未填",
            ))
        elif dep_ans is not None and dep_ans != expect and this_ans is not None:
            findings.append(Finding(
                severity=Severity.WARN,
                code="F02.CONDITIONAL",
                title=f"{qid} 與前題 {dep} 不一致",
                message=f"{dep}={dep_ans}（非 {expect}）時，{qid} 通常無需填寫，但目前填了 {this_ans}。",
                location=qid,
                expected=f"{dep}≠{expect} 時 {qid} 留空",
                actual=f"{qid}={this_ans}",
            ))
    return findings


def check_score_consistency(form: F02Form, cfg: dict) -> list[Finding]:
    """重算四域百分比與分級，與檔內快取值比對。"""
    findings = []
    result = recompute(form, cfg)

    cached = form.cached
    # 分級比對
    if cached.grade and cached.grade != result.grade:
        findings.append(Finding(
            severity=Severity.ERROR,
            code="F02.GRADE_MISMATCH",
            title="風險分級與重算不一致",
            message=(f"依答案重算分級為「{result.grade}」(分數 {result.overall:.1f})，"
                     f"但檔內為「{cached.grade}」。可能填寫錯誤或公式被改，請人工確認。"),
            location="分級",
            expected=f"重算 {result.grade}",
            actual=f"檔內 {cached.grade}",
        ))
    # 各域百分比比對
    if cached.percentages is not None:
        for d in cfg["domains"]:
            recomputed = getattr(result.percentages, d)
            stored = getattr(cached.percentages, d)
            if abs(recomputed - stored) > SCORE_TOLERANCE:
                label = cfg["domain_labels"][d]
                findings.append(Finding(
                    severity=Severity.ERROR,
                    code="F02.SCORE_MISMATCH",
                    title=f"{label}風險分數不一致",
                    message=(f"{label}域重算 {recomputed:.1f}%，檔內 {stored:.1f}%，"
                             f"差距超過容許值，請人工確認。"),
                    location=f"{label}域百分比",
                    expected=f"{recomputed:.1f}%",
                    actual=f"{stored:.1f}%",
                ))
    return findings


def check_followup_sheets(form: F02Form, cfg: dict) -> list[Finding]:
    """中/高風險須填剩餘風險評鑑；剩餘分數>6 須填處理計畫。"""
    findings = []
    grade = recompute(form, cfg).grade

    if grade in {"中", "高"}:
        if not form.residual_filled:
            findings.append(Finding(
                severity=Severity.ERROR,
                code="F02.RESIDUAL_MISSING",
                title="缺『剩餘風險評鑑表』",
                message=f"分級為「{grade}」，依規定須續填剩餘風險評鑑表，目前為空。",
                location="AI系統剩餘風險評鑑表",
                expected="已填",
                actual="未填",
            ))
        elif form.residual_max_score is not None and form.residual_max_score > TREATMENT_THRESHOLD:
            if not form.treatment_filled:
                findings.append(Finding(
                    severity=Severity.ERROR,
                    code="F02.TREATMENT_MISSING",
                    title="缺『AI系統風險處理計畫表』",
                    message=(f"剩餘風險分數 {form.residual_max_score:g} 大於 {TREATMENT_THRESHOLD}，"
                             f"依規定須續填風險處理計畫表，目前為空。"),
                    location="AI系統風險處理計畫表",
                    expected="已填",
                    actual="未填",
                ))
    return findings
