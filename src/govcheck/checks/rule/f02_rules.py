"""F02 規則檢查（確定性、零 LLM）。

每個函式對一條規則，回傳 0..N 個 Finding。run_all() 把它們串起來，
供 engine 呼叫。所有判斷只依賴 F02Form 與 config，可單元測試、不需網路。
"""

from __future__ import annotations

from govcheck.models import F02Form, Finding, Severity
from govcheck.scoring.f02_score import load_config, recompute

# 重算 vs 快取 比對的容許誤差（百分比）
SCORE_TOLERANCE = 0.5
# 剩餘風險分數續填處理計畫的門檻（剩餘風險 ≥ 此值即須續填，依附件四〈四、風險分數說明〉）
TREATMENT_THRESHOLD = 6


def run_all(form: F02Form, cfg: dict | None = None) -> list[Finding]:
    cfg = cfg or load_config()
    findings: list[Finding] = []
    findings += check_series_completeness(form, cfg)
    findings += check_general_completeness(form, cfg)
    findings += check_single_choice(form, cfg)
    findings += check_multi_choice(form, cfg)
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


def check_general_completeness(form: F02Form, cfg: dict) -> list[Finding]:
    """一般題（UC-01/02/03/07、D-03/04、M-01/02）每題都須有 Y/N，不可留空。

    這些題未納入單/複選群組，但屬必填；留空除了漏填，對反向計分題（答 N 計分）
    還會讓重算分數被低估，故一律標記。
    """
    missing = [qid for qid in cfg["groups"]["general"] if form.answers.get(qid) is None]
    if not missing:
        return []
    return [Finding(
        severity=Severity.ERROR,
        code="F02.GENERAL_INCOMPLETE",
        title="一般題有未填",
        message=f"一般題應每題皆填 Y 或 N，未填：{', '.join(missing)}。留空會導致風險分數被低估。",
        location="一般題",
        expected="每題皆 Y/N",
        actual=f"未填 {len(missing)} 題",
    )]


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


def check_score_consistency(form: F02Form, cfg: dict) -> list[Finding]:
    """重算四域百分比與分級，與檔內快取值比對。

    若檔內無快取分級（如非 Excel 工具產生、未經試算的檔），無法比對，發 WARN
    提醒人工確認，避免靜默通過而誤以為「計分一致」。
    """
    findings = []
    result = recompute(form, cfg)

    cached = form.cached
    # 無快取分級 → 無法驗證，提醒人工
    if not cached.grade:
        findings.append(Finding(
            severity=Severity.WARN,
            code="F02.CACHE_MISSING",
            title="無法比對檔內計分（缺快取值）",
            message=(f"檔內未提供 Excel 已算好的分級/分數（可能由非 Excel 工具產生或未試算），"
                     f"無法與重算比對。重算結果為「{result.grade}」(分數 {result.overall:.1f})，請人工確認。"),
            location="分級",
            expected="檔內含 Excel 快取分級",
            actual="無快取",
        ))
        return findings

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
    """中/高風險須填剩餘風險評鑑；剩餘分數 ≥6 須填處理計畫。"""
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
        elif form.residual_max_score is not None and form.residual_max_score >= TREATMENT_THRESHOLD:
            if not form.treatment_filled:
                findings.append(Finding(
                    severity=Severity.ERROR,
                    code="F02.TREATMENT_MISSING",
                    title="缺『AI系統風險處理計畫表』",
                    message=(f"剩餘風險分數 {form.residual_max_score:g} 大於等於 {TREATMENT_THRESHOLD}，"
                             f"依規定須續填風險處理計畫表，目前為空。"),
                    location="AI系統風險處理計畫表",
                    expected="已填",
                    actual="未填",
                ))
    return findings
