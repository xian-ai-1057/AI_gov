"""把 findings 彙整成「缺陷 × 類型」的清單（報告頁預設視圖）。

**為什麼需要這一層**：一份真實送件包常見的情況是 15 個檢核項全部觸發同一條規則
（上線佐證整欄空白），逐列呈現等於把一個原因講了 15 次；而真正在變動的資訊
（哪幾項另有法規缺口、哪幾項佐證過短）反而被淹沒。缺陷模型把同一原因收成一列、
附上筆數與可核對的命中位置，讓治理人員先掃掉機械性缺漏，再進逐項覆核。

**單一真相**：這裡不做任何判定，只重組 ReviewReport 既有的 findings / roster /
item_assessments。新增一種檢查 = 在 `_CATALOG` 加一筆，UI 不必改。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from govcheck.models import (
    CommonFact,
    Defect,
    DefectHit,
    Finding,
    LayerStatus,
    Locator,
    ReviewReport,
    Verdict,
)
from govcheck.models.report_view import DefectSeverity, DefectSource, Layer

# finding.location 形如「項次 1-1」或「項次 1-1（管理議題）」（見 F03ChecklistItem.loc）。
# 前端原本用同一條 regex 解析中文字串；搬到後端後成為單一真相。
_ITEM_LOC_RE = re.compile(r"^項次\s*(\d+-\d+)")

# 整段判讀降級訊號：非合規結論，代表「這次語意判讀沒完整跑」。必須在主視圖顯著提示，
# 不可埋進單一項次列或收合區，也不該被當成一種「缺陷」。
NOTICE_CODES = frozenset({
    "F03.LLM_SKIPPED",
    "F03.PROMPT_UNAVAILABLE",
    # 超過上限而未送審的檢核項不會出現在任何列，只能靠這條主視圖提示讓人看見。
    "F03.ASSESSMENT_TRUNCATED",
    "REGULATION.MAP_UNAVAILABLE",
})


@dataclass(frozen=True)
class _Spec:
    """一種缺陷類型的登錄表項目。UI 照表渲染，不內建任何缺陷知識。"""

    code: str
    layer: Layer
    source: DefectSource
    severity: DefectSeverity          # count > 0 時的嚴重度；count == 0 一律降為 "ok"
    label: str
    basis: str
    confidence: str
    locator_label: str
    note: str
    codes: tuple[str, ...] = ()       # 精確比對的 finding code
    prefixes: tuple[str, ...] = ()    # 前綴比對（如 "DOC.MISSING_"、"CROSS."）
    suffixes: tuple[str, ...] = ()    # 後綴比對（如 ".PARSE_ERROR"）
    trial: bool = False               # 試辦：不計入待覆核總數，只在展開列出現
    # 本次沒命中時是否仍要列出來。「查過了、沒有」與「沒查」是兩件事——
    # 治理人員需要看得出差別，否則會誤以為這項根本沒檢查。
    always_show: bool = True
    per_item: bool = True             # 命中是否以「檢核項」為單位（決定共通事實帶的判定）
    hit_value: str = "actual"         # "actual" | "compare" | "count"


# ── 缺陷登錄表 ────────────────────────────────────────────────────────────
# 順序即畫面順序：先 L1（秒級、可立刻補件），再 L2（檢索），最後 L3（試辦）。
_CATALOG: tuple[_Spec, ...] = (
    _Spec(
        code="PARSE_ERROR", layer="L1", source="rule", severity="error",
        label="檔案無法解析",
        basis="表單非官方範本或檔案損毀，後續檢查失去依據",
        confidence="確定",
        locator_label="送件包表單",
        note="解析失敗的表單不會進入任何後續檢查；請先確認檔案為官方範本且未損毀。",
        suffixes=(".PARSE_ERROR",), codes=("F03.CHECKLIST_PARSE_ERROR",),
        per_item=False, always_show=False,
    ),
    _Spec(
        code="DOC_MISSING", layer="L1", source="rule", severity="error",
        label="送件包缺件",
        basis="治理辦法要求的文件未隨送件包提交",
        confidence="確定",
        locator_label="送件包清單",
        note="缺件會讓對應的檢查整段失去依據；補齊後需重跑審查。",
        prefixes=("DOC.MISSING_",), per_item=False,
    ),
    _Spec(
        code="FORM_REQUIRED_MISSING", layer="L1", source="rule", severity="error",
        label="表單必填未填",
        basis="F01／F02 的必填欄位或必答題留空",
        confidence="確定",
        locator_label="F01 主表 / F02 評鑑表",
        note="必填欄位缺漏會連帶影響風險計分與跨表比對；屬最優先補正項目。",
        codes=(
            "F01.NO_DATA_ROW", "F01.REQUIRED_MISSING",
            "F02.SERIES_INCOMPLETE", "F02.GENERAL_INCOMPLETE",
            "F02.SINGLE_CHOICE", "F02.MULTI_CHOICE",
            "F02.RESIDUAL_MISSING", "F02.RESIDUAL_SCORE_MISSING",
            "F02.TREATMENT_MISSING",
        ),
        per_item=False, hit_value="compare",
    ),
    _Spec(
        code="VALUE_MISMATCH", layer="L1", source="rule", severity="error",
        label="數值錯誤",
        basis="F02 檔內快取的分數／分級與依答案重算的結果不符",
        confidence="確定",
        locator_label="F02·計分表",
        note="合計為手動覆寫或公式被改動；風險分級可能因此誤判，連帶影響條件式佐證要求。",
        codes=("F02.SCORE_MISMATCH", "F02.GRADE_MISMATCH", "F02.CACHE_MISSING"),
        per_item=False, hit_value="compare",
    ),
    _Spec(
        code="CROSS_INCONSISTENT", layer="L1", source="rule", severity="error",
        label="前後不一致",
        basis="同一項資料在不同表單填得不一樣",
        confidence="確定",
        locator_label="F01 / F02 / F03 對應欄位",
        note="這類缺陷最該優先修——後面所有判讀都建立在同一份基本資料上。",
        prefixes=("CROSS.",), per_item=False, hit_value="compare",
    ),
    _Spec(
        code="BLANK_PROPOSAL", layer="L1", source="rule", severity="warn",
        label="提案佐證空白",
        basis="勾選「是（已完成檢核）」，但 I 欄「前項佐證說明（提案規劃階段）」未填",
        confidence="確定",
        locator_label="F03·檢核表 I 欄",
        note="勾了完成卻沒有佐證，無從確認該項是否真的做過；請補佐證或改回未完成。",
        codes=("F03.EVIDENCE_MISSING_PROPOSAL",),
    ),
    _Spec(
        code="BLANK_GOLIVE", layer="L1", source="rule", severity="warn",
        label="上線佐證空白",
        basis="J 欄「前項佐證說明（上線階段）」完全未填",
        confidence="確定",
        locator_label="F03·檢核表 J 欄",
        note="未交代上線後的實際做法；屬機械性缺漏，可直接列入補件通知。",
        codes=("F03.EVIDENCE_MISSING_GOLIVE",),
    ),
    _Spec(
        code="TOO_SHORT", layer="L1", source="rule", severity="warn",
        label="佐證過短",
        basis="佐證說明字數低於門檻，不足以檢核",
        confidence="確定",
        locator_label="F03·檢核表 I 欄",
        note="門檻可在 review_config.yaml 的 f03.quality 調整。字數是可核對的事實，"
             "勝過「過於草率」這種無法驗證的評語。",
        codes=("F03.EVIDENCE_TOO_SHORT",),
    ),
    _Spec(
        code="COPIED_FROM_PROPOSAL", layer="L1", source="rule", severity="warn",
        label="照抄提案",
        basis="上線階段佐證與提案階段逐字相同",
        confidence="確定",
        locator_label="F03·檢核表 J 欄",
        note="上線佐證為空白時此項不成立（另由「上線佐證空白」涵蓋）；補件後需重跑。",
        codes=("F03.EVIDENCE_COPIED",),
    ),
    _Spec(
        code="NA_NO_REASON", layer="L1", source="rule", severity="warn",
        label="不適用未填理由",
        basis="標示「不適用」但佐證欄空白",
        confidence="確定",
        locator_label="F03·檢核表 H/I 欄",
        note="不適用的項次不進語意判讀，人工是唯一把關；沒有理由就無從確認主張是否成立。",
        codes=("F03.NOT_APPLICABLE_NO_REASON",),
    ),
    _Spec(
        code="TEMPLATE_MODIFIED", layer="L1", source="rule", severity="warn",
        label="檢核表被改動",
        basis="上傳檔的檢查項目或規範參考與官方範本不符",
        confidence="確定",
        locator_label="F03·檢核表 D/L 欄",
        note="範本被改動會讓判讀基準失準；請確認是否使用最新版官方範本。",
        codes=("F03.TEMPLATE_REF_MODIFIED",),
    ),
    _Spec(
        code="REG_GAP", layer="L2", source="rag", severity="warn",
        label="法規符合性缺口",
        basis="佐證未涵蓋治理辦法對應條文的要求",
        confidence="高 · 可逐條核對",
        locator_label="治理辦法對應條文",
        note="缺哪幾條可以逐條列舉，覆核者能自行核對條文原文，不必信任模型的總評。",
        codes=("F03.REGULATION_GAP",),
    ),
    _Spec(
        code="EVIDENCE_UNSUPPORTED", layer="L2", source="rag", severity="warn",
        label="佐證未支持敘述",
        basis="佐證文件中找不到印證該項敘述的內容",
        confidence="中 · 待確認",
        locator_label="佐證文件",
        note="佐證說明是提案單位的自述，不構成佐證；此項標示的是「找不到文件佐證」，"
             "不等於敘述不實。",
        codes=("F03.EVIDENCE_NOT_SUPPORTED",),
    ),
    _Spec(
        code="EVIDENCE_CONFLICT", layer="L2", source="rag", severity="error",
        label="佐證互相衝突",
        basis="佐證文件與敘述牴觸，或兩份佐證彼此矛盾",
        confidence="高 · 附引用",
        locator_label="佐證文件",
        note="衝突比「找不到佐證」更值得優先處理；展開可看到牴觸的來源片段。",
        codes=("F03.EVIDENCE_CONTRADICTED", "F03.EVIDENCE_SOURCE_CONFLICT"),
    ),
    _Spec(
        code="RUBRIC_UNMET", layer="L3", source="llm", severity="warn",
        label="敘述充分性不足",
        basis="審查員檢核句未全數滿足",
        confidence="中 · 待確認",
        locator_label="逐項附引用",
        note="試辦中，不計入「待覆核」總數。模型只判「說明有沒有回應這一句」，不下結論；"
             "未打勾的句子可直接貼進補件通知。",
        codes=(
            "F03.ASSESSMENT_INCOMPLETE",
            "F03.ASSESSMENT_VAGUE",
            "F03.ASSESSMENT_UNREASONABLE",
        ),
        trial=True,
    ),
    _Spec(
        code="UNDETERMINED", layer="L3", source="llm", severity="info",
        label="判讀未完成",
        basis="模型未給出判讀，或信心低於門檻",
        confidence="—",
        locator_label="逐項",
        note="這是「AI 判不了」不是「判出問題」；不計入待覆核總數，但必須人工看過。",
        codes=("F03.ASSESSMENT_UNDETERMINED", "F03.ASSESSMENT_LOW_CONFIDENCE"),
        trial=True, always_show=False,
    ),
)

_LAYER_LABELS: tuple[tuple[Layer, str], ...] = (
    ("L1", "規則檢查"),
    ("L2", "法規／佐證檢索"),
    ("L3", "敘述充分性判讀"),
)


def parse_item_id(location: str | None) -> str | None:
    """從 finding.location 取出 F03 項次；非項次定位（F01/F02/DOC/CROSS）回 None。"""
    if not location:
        return None
    match = _ITEM_LOC_RE.match(location)
    return match.group(1) if match else None


def _matches(spec: _Spec, code: str) -> bool:
    return (
        code in spec.codes
        or any(code.startswith(prefix) for prefix in spec.prefixes)
        or any(code.endswith(suffix) for suffix in spec.suffixes)
    )


@dataclass
class _Bucket:
    findings: list[Finding] = field(default_factory=list)


def _cell(column: str | None, row_index: int | None) -> str | None:
    return f"{column}{row_index}" if column and row_index else None


def _citation_locator(citation) -> Locator:
    """把已驗證的 assessment citation 轉成缺陷定位，不猜未知 locator type。"""
    locator_type = str(citation.locator_type or "").casefold()
    locator_value = str(citation.locator_value or "").strip()
    locator = Locator(file=citation.document_id or None)
    if locator_type == "page":
        try:
            locator.page = int(locator_value)
        except ValueError:
            locator.section = locator_value or None
    elif locator_type == "sheet":
        locator.sheet = locator_value or None
    elif locator_type in {"section", "heading"}:
        locator.section = locator_value or None
    elif locator_value:
        locator.section = f"{citation.locator_type}:{locator_value}"
    return locator


def _locator_for(
    item_id: str | None,
    spec: _Spec,
    roster_by_id: dict,
    columns: dict,
    *,
    is_golive: bool,
    assessment=None,
) -> Locator | None:
    """項次 → 最接近判定依據的可稽核座標；資訊不足時不猜格號。"""
    if item_id is None:
        return None
    if spec.code == "REG_GAP" and assessment and assessment.regulation_refs:
        return Locator(file="治理辦法", section="、".join(assessment.regulation_refs))
    if spec.layer in {"L2", "L3"} and assessment and assessment.citations:
        return _citation_locator(assessment.citations[0])

    entry = roster_by_id.get(item_id)
    row_index = getattr(entry, "row_index", None) if entry is not None else None
    column = {
        "BLANK_PROPOSAL": columns.get("proposal"),
        # 佐證品質檢查跟著流程階段走：提案看 I，上線看 J。
        "TOO_SHORT": columns.get("golive" if is_golive else "proposal"),
        "NA_NO_REASON": columns.get("proposal"),
        "BLANK_GOLIVE": columns.get("golive"),
        "COPIED_FROM_PROPOSAL": columns.get("golive"),
        "TEMPLATE_MODIFIED": columns.get("regulation_ref"),
        # 沒有引用可指時，L2/L3 至少回到本次被判讀的佐證說明格，而不是只寫一個
        # 無法核對的「F03」。
        "REG_GAP": columns.get("golive" if is_golive else "proposal"),
        "EVIDENCE_UNSUPPORTED": columns.get("golive" if is_golive else "proposal"),
        "EVIDENCE_CONFLICT": columns.get("golive" if is_golive else "proposal"),
        "RUBRIC_UNMET": columns.get("golive" if is_golive else "proposal"),
        "UNDETERMINED": columns.get("golive" if is_golive else "proposal"),
    }.get(spec.code)
    return Locator(file="F03", sheet=columns.get("sheet"), cell=_cell(column, row_index))


def _locator_label(spec: _Spec, columns: dict, *, is_golive: bool) -> str:
    """缺陷類型的一行座標；階段性欄位不可在上線報告仍寫死成 I 欄。"""
    if spec.code != "TOO_SHORT":
        return spec.locator_label
    column = columns.get("golive" if is_golive else "proposal")
    sheet = columns.get("sheet")
    if column and sheet:
        return f"F03·{sheet} {column} 欄"
    return spec.locator_label


def _hit_value(finding: Finding, spec: _Spec, assessment=None) -> str:
    """命中值一律是可核對的事實；沒有 expected/actual 就退回 finding 標題。"""
    if spec.code == "REG_GAP" and assessment and assessment.regulation_refs:
        return "、".join(assessment.regulation_refs)
    if spec.hit_value == "compare" and (finding.expected or finding.actual):
        return f"{finding.expected or '—'} ≠ {finding.actual or '—'}"
    if finding.actual:
        return finding.actual
    return finding.title


def _hit_key(finding: Finding, item_id: str | None) -> str:
    if item_id:
        return item_id
    return finding.location or finding.title


def _column_map(cfg: dict | None) -> dict:
    checklist = ((cfg or {}).get("f03") or {}).get("checklist") or {}
    return {
        "sheet": ((cfg or {}).get("f03") or {}).get("sheet"),
        "proposal": checklist.get("evidence_proposal_col"),
        "golive": checklist.get("evidence_golive_col"),
        "regulation_ref": checklist.get("regulation_ref_col"),
    }


def _basis_for(spec: _Spec, findings: list[Finding]) -> str:
    """需要設定值的判定依據由 finding 帶回，避免畫面寫死預設門檻。"""
    if spec.code != "TOO_SHORT":
        return spec.basis
    for finding in findings:
        match = re.search(r"至少\s*(\d+)\s*字", finding.expected or "")
        if match:
            return f"佐證說明字數低於 {match.group(1)} 字，不足以檢核"
    return spec.basis


def _unique_item_findings(findings: list[Finding], spec: _Spec) -> list[Finding]:
    """Item-level 缺陷一個項次只算一次；無法解析項次者仍逐筆保留。"""
    if not spec.per_item:
        return list(findings)
    output: list[Finding] = []
    seen: set[str] = set()
    for index, finding in enumerate(findings):
        item_id = parse_item_id(finding.location)
        key = f"item:{item_id}" if item_id else f"finding:{index}"
        if key in seen:
            continue
        seen.add(key)
        output.append(finding)
    return output


def _rubric_hits(
    report: ReviewReport,
    spec: _Spec,
    roster_by_id: dict,
    columns: dict,
    *,
    is_golive: bool,
) -> tuple[list[DefectHit], set[str]]:
    """由逐句 rubric 直接產生 L3 缺陷，不再以多條概括 finding 重複計數。"""
    hits: list[DefectHit] = []
    item_ids: set[str] = set()
    for assessment in report.item_assessments:
        lines = assessment.rubric
        if assessment.item_id in item_ids or not lines or all(line.met for line in lines):
            continue
        item_ids.add(assessment.item_id)
        met = sum(1 for line in lines if line.met)
        hits.append(DefectHit(
            key=assessment.item_id,
            value=f"{len(lines)} 句中 {met} 句滿足",
            item_id=assessment.item_id,
            locator=_locator_for(
                assessment.item_id,
                spec,
                roster_by_id,
                columns,
                is_golive=is_golive,
                assessment=assessment,
            ),
        ))
    return hits, item_ids


def build_defects(
    report: ReviewReport,
    cfg: dict | None = None,
    max_hits: int = 40,
) -> list[Defect]:
    """把 report.findings 依缺陷登錄表彙整成缺陷清單。

    max_hits：單一缺陷最多列出的命中數。整份 payload 不該因為某一類缺陷命中上百次
    就膨脹到不可讀——超出的部分以「另 N 項」一筆帶過。
    """
    columns = _column_map(cfg)
    roster_by_id = {entry.item_id: entry for entry in report.checklist_roster}
    roster_ids = set(roster_by_id)
    assessments_by_id = {
        assessment.item_id: assessment for assessment in report.item_assessments
    }
    is_golive = report.review_run_meta.workflow_stage.value != "proposal"
    layer_states = {layer.id: layer.state for layer in build_layers(report)}

    buckets: dict[str, _Bucket] = {spec.code: _Bucket() for spec in _CATALOG}
    for finding in report.findings:
        if finding.code in NOTICE_CODES:
            continue  # 降級訊號另走 build_notices，不是缺陷
        for spec in _CATALOG:
            if _matches(spec, finding.code):
                buckets[spec.code].findings.append(finding)
                break  # 一條 finding 只歸一種缺陷，避免同一件事被數兩次

    defects: list[Defect] = []
    for spec in _CATALOG:
        findings = buckets[spec.code].findings
        if spec.code == "RUBRIC_UNMET":
            all_hits, item_ids = _rubric_hits(
                report,
                spec,
                roster_by_id,
                columns,
                is_golive=is_golive,
            )
            count = len(item_ids)
            codes = sorted({
                finding.code
                for finding in findings
                if parse_item_id(finding.location) in item_ids
            })
        else:
            counted_findings = _unique_item_findings(findings, spec)
            count = len(counted_findings)
            item_ids = {parse_item_id(f.location) for f in counted_findings} - {None}
            all_hits = []
            for finding in counted_findings:
                item_id = parse_item_id(finding.location)
                assessment = assessments_by_id.get(item_id)
                all_hits.append(DefectHit(
                    key=_hit_key(finding, item_id),
                    value=_hit_value(finding, spec, assessment),
                    item_id=item_id,
                    locator=_locator_for(
                        item_id,
                        spec,
                        roster_by_id,
                        columns,
                        is_golive=is_golive,
                        assessment=assessment,
                    ),
                ))
            codes = sorted({f.code for f in findings})

        if not count and not spec.always_show:
            continue
        # 「0」只能表示這一層真的跑完且查無命中。running/todo/skipped 時直接不列，避免
        # 把「尚未檢查」畫成綠色的「查過了、沒有」。
        if not count and layer_states.get(spec.layer) != "done":
            continue

        hits = all_hits[:max_hits]
        if count > max_hits:
            hits.append(DefectHit(key="其他", value=f"另 {count - max_hits} 項"))

        is_common = bool(
            spec.per_item and roster_ids and item_ids == roster_ids
        )
        defects.append(Defect(
            code=spec.code,
            layer=spec.layer,
            source=spec.source,
            # count == 0 一律 "ok"：本次查過了、沒有。與「沒查」必須看得出差別。
            severity=spec.severity if count else "ok",
            label=spec.label,
            count=count,
            basis=_basis_for(spec, findings),
            confidence=spec.confidence,
            hits=hits,
            locator_label=_locator_label(spec, columns, is_golive=is_golive),
            note=spec.note,
            trial=spec.trial,
            is_common=is_common,
            codes=codes,
        ))
    return defects


def build_notices(findings: Iterable[Finding]) -> list[Finding]:
    """整段降級訊號（判讀略過／Prompt 未就緒／截斷／缺法規對照表）。

    這些不是「缺陷」，是「這次審查有一部分沒跑」。混進缺陷清單會讓治理人員
    誤以為送件包多了一項問題，而真正的問題是報告本身不完整。
    """
    return [f for f in findings if f.code in NOTICE_CODES]


def build_common_facts(defects: Iterable[Defect], total_items: int) -> list[CommonFact]:
    """全表同值的缺陷 → 共通事實帶。把 N 列同一句話收成一行。"""
    return [
        CommonFact(
            code=defect.code,
            text=f"{defect.count}/{total_items} 項{defect.label}",
            count=defect.count,
        )
        for defect in defects
        if defect.is_common and defect.count
    ]


def build_uniform_columns(report: ReviewReport) -> list[str]:
    """總覽表中「全表同值」而該收起的欄位。

    一欄的每一列都是同一個值時，那一欄不帶任何資訊，只是把真正在變動的欄位擠窄。
    收起後在表頭標示收了哪幾欄與其共同值，資訊不損失。
    """
    roster = report.checklist_roster
    if len(roster) < 2:
        return []

    columns: list[str] = []
    stage_is_golive = report.review_run_meta.workflow_stage.value != "proposal"

    filled = {bool(entry.evidence_proposal_text) for entry in roster}
    if filled == {True}:
        columns.append("提案佐證（全部已填寫）")
    elif filled == {False}:
        columns.append("提案佐證（全部空白）")

    if stage_is_golive:
        golive_filled = {bool(entry.evidence_golive_text) for entry in roster}
        if golive_filled == {False}:
            columns.append("上線佐證（全部空白）")
            # 上線佐證整欄空白時「兩段差異」必然是「有差異」，屬可推導欄位。
            columns.append("兩段差異（可由上線佐證推導）")
        elif golive_filled == {True}:
            columns.append("上線佐證（全部已填寫）")
            diffs = {entry.golive_diff for entry in roster}
            if len(diffs) == 1:
                columns.append(f"兩段差異（全部「{diffs.pop()}」）")

    assessments = report.item_assessments
    roster_ids = {entry.item_id for entry in roster}
    assessment_ids = {assessment.item_id for assessment in assessments}
    if assessments and assessment_ids == roster_ids:
        gaps = {a.regulation_gap for a in assessments}
        if len(gaps) == 1:
            columns.append(f"法規符合（全部「{gaps.pop()}」）")
    return columns


def build_layers(report: ReviewReport) -> list[LayerStatus]:
    """報告已帶分層狀態就直接用；沒有（非串流路徑）則由結果反推。"""
    if report.layers:
        return list(report.layers)
    llm_ran = bool(report.item_assessments)
    return [
        LayerStatus(id="L1", label="規則檢查", state="done"),
        LayerStatus(id="L2", label="法規／佐證檢索", state="done" if llm_ran else "skipped"),
        LayerStatus(id="L3", label="敘述充分性判讀", state="done" if llm_ran else "skipped"),
    ]


def _rule_layer_clean(defects: Iterable[Defect]) -> bool:
    return not any(d.layer == "L1" and d.count for d in defects)


def build_verdict(report: ReviewReport, defects: list[Defect]) -> Verdict:
    """報告頂端的判定句。

    **標題就是判定**，不是「規則通過」——那只描述了 L1 這一層，會與同頁「15/15 待確認」
    自相矛盾。判定以 decision_status 為準（它已經把 error/warn/文件無法解析/判讀未完整
    都算進去了），缺陷數量只負責把「為什麼」講清楚。
    """
    status = report.decision_status.value
    has_error = any(d.severity == "error" and d.count for d in defects)
    if status == "changes_required" or has_error:
        code, label = "blocked", "初判：不可放行 · 需補件"
    elif status == "approved":
        code, label = "passed", "初判：可進入覆核"
    else:
        code, label = "review", "初判：需人工覆核後放行"
    return Verdict(code=code, label=label, because=_because(report, defects))


def _because(report: ReviewReport, defects: list[Defect]) -> str:
    """一句話講清楚規模與主因；沒有缺陷時也要明講「規則層乾淨」。"""
    total = len(report.checklist_roster)
    by_code = {d.code: d for d in defects}
    parts: list[str] = []

    for defect in defects:
        if not defect.count or defect.trial:
            continue
        if defect.is_common and total:
            parts.append(f"{total} 個檢核項全部{defect.label}")
        else:
            # 以檢核項為單位的缺陷用「項」，表單／文件層級的用「處」。
            unit = "項" if any(hit.item_id for hit in defect.hits) else "處"
            parts.append(f"{defect.count} {unit}{defect.label}")
        if len(parts) == 3:
            break

    trial = [d for d in defects if d.trial and d.count]
    if not parts:
        base = "規則檢查與檢索比對均未發現缺陷。"
        if trial:
            base += f"語意判讀另標示 {trial[0].count} 項待確認（試辦中，不計入待覆核）。"
        return base + "仍須人工覆核實質內容。"

    sentence = "；".join(parts) + "。"
    if _rule_layer_clean(by_code.values()):
        sentence += "規則層本身沒有錯誤。"
    if trial:
        sentence += f"另有 {trial[0].count} 項{trial[0].label}（試辦中，不計入待覆核）。"
    return sentence


def build_layer_labels() -> list[LayerStatus]:
    """尚未開始執行時的分層骨架（串流的第一幀）。"""
    return [LayerStatus(id=layer, label=label, state="todo") for layer, label in _LAYER_LABELS]


def attention_item_ids(report: ReviewReport, defects: list[Defect]) -> list[str]:
    """需要逐項覆核的檢核項。

    試辦層（L3）的缺陷刻意不計入——準確率還沒被驗證的檢查不該撐大「待覆核」數字，
    否則治理人員會為了消化試辦誤報而失去對這個數字的信任。
    """
    del report  # 保留參數以維持呼叫端可讀性；目前僅需 defects
    ids: set[str] = set()
    for defect in defects:
        if defect.trial:
            continue
        for hit in defect.hits:
            if hit.item_id:
                ids.add(hit.item_id)
    return sorted(ids, key=lambda x: [int(p) for p in x.split("-")])
