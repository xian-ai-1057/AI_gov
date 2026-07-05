"""F03 RAG 符合性判讀（f03_rag.run_all）測試。

全部離線 mock — FakeClient 直接消費 specs/p3-03-rag-checks/contracts/fixtures/canned_llm_responses/；
findings 與 expected_findings/*.yaml 逐筆吻合。

AC coverage（p3-03 spec §6）：
- AC-1  : canned → expected 逐筆（gap / covered / undetermined / missing_item / illegal_verdict）
- AC-2  : 摘要計數（各案 RAG_SUMMARY.message 含 summary_contains 字串）
- AC-3  : not_json 容錯 → RAG_ITEM_ERROR + 表 + 摘要；absent_codes（RAG_ERROR）不出現
- AC-4  : illegal_verdict 白名單容錯（"maybe" → undetermined）
- AC-5  : 連錯 3 批中止（client.calls == 3；後續批不再呼叫）
- AC-6  : 單批失敗續審（1 失敗 + 其餘正常）
- AC-7  : prompt 預算超限降批（caplog 不含摘錄/佐證）
- AC-8  : TEMPLATE_REF_MODIFIED 正例
- AC-9  : TEMPLATE_REF_MODIFIED 反例（上傳與 canonical 相符 → 不出現）
- AC-17 : 隱私 log（batch fail 只記型別，不記訊息本文）
- AC-18 : 絕不 raise（client=None、壞 client）
- AC-19 : F03RagBatchResponse.model_validate(gap.json) 成功；extra key → ValidationError
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from govcheck.checks.llm import f03_rag
from govcheck.llm.client import LLMError
from govcheck.logging_setup import load_log_config, setup_logging
from govcheck.models import F03Checklist, F03ChecklistItem
from govcheck.rag.models import (
    F03ItemRetrieval,
    F03RagBatchResponse,
    RagConfig,
    RetrievedSection,
    RetrievalMap,
)

# ── Fixture 目錄 ──────────────────────────────────────────────────────────────
FIXTURE_DIR = (
    Path(__file__).resolve().parents[1]
    / "specs" / "p3-03-rag-checks" / "contracts" / "fixtures"
)
CANNED = FIXTURE_DIR / "canned_llm_responses"
EXPECTED = FIXTURE_DIR / "expected_findings"

# ── 預設測試用 RagConfig ───────────────────────────────────────────────────────
DEFAULT_CFG = RagConfig(batch_size=10, max_items=30, max_sections_per_item=3, max_excerpt_chars=300)


# ─────────────────────────────────────────────────────────────────────────────
# 測試基礎建設
# ─────────────────────────────────────────────────────────────────────────────


class FakeClient:
    """依序回傳預先準備的輸出；Exception 實例 → raise（鏡射 test_f03_evidence_llm.py 範式）。"""

    def __init__(self, outputs: list):
        self._outputs = list(outputs)
        self.calls = 0

    def chat(self, messages, **kw) -> str:
        self.calls += 1
        out = self._outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _make_section(reg_code: str = "R03", section_path: str = "五/(二)/2",
                  title: str = "示例條（synthetic）",
                  excerpt: str = "（示例摘錄，非真實法規內容）") -> RetrievedSection:
    return RetrievedSection(
        reg_code=reg_code, section_path=section_path,
        title=title, excerpt=excerpt, score=None, origin="curated",
    )


def _make_f03_item(item_id: str = "1-1",
                   description: str | None = None,
                   ref_raw: str | None = "R03&R07/五/(二)/2",
                   sections: list[RetrievedSection] | None = None) -> F03ItemRetrieval:
    if description is None:
        description = f"示例檢查項目描述{item_id}（synthetic）"
    if sections is None:
        sections = [
            _make_section("R03", "五/(二)/2"),
            _make_section("R07", "五/(二)/2", title="示例條2（synthetic）"),
        ]
    return F03ItemRetrieval(
        item_id=item_id,
        canonical_topic="示例議題A",
        canonical_description=description,
        canonical_ref_raw=ref_raw,
        sections=sections,
    )


def _make_retrieval_map(
    f03_ids: list[str] | None = None,
    extra_sections: dict[str, list[RetrievedSection]] | None = None,
    descriptions: dict[str, str] | None = None,
) -> RetrievalMap:
    """建構測試用 RetrievalMap；f03_ids 預設含 1-1 / 1-2。
    descriptions 讓各 item 的 canonical_description 與 checklist 保持一致，避免誤觸 TEMPLATE_REF_MODIFIED。
    """
    if f03_ids is None:
        f03_ids = ["1-1", "1-2"]
    items = {}
    for fid in f03_ids:
        secs = (extra_sections or {}).get(fid)
        desc = (descriptions or {}).get(fid, f"示例檢查項目描述{fid}（synthetic）")
        items[fid] = _make_f03_item(item_id=fid, description=desc, sections=secs)
    return RetrievalMap(
        schema_version=1, built_at="2026-01-01T00:00:00Z",
        embedding_model="fake-embedding-model",
        f03_items=items, f02_questions={},
    )


def _make_checklist(
    items: list[F03ChecklistItem] | None = None,
) -> F03Checklist:
    """建構測試用 F03Checklist（預設 1-1 / 1-2 各有兩段佐證）。

    description 與 _make_retrieval_map 預設一致（避免誤觸 TEMPLATE_REF_MODIFIED）。
    """
    if items is None:
        items = [
            F03ChecklistItem(
                item_id="1-1", topic="公平性", description="示例檢查項目描述1-1（synthetic）",
                evidence_proposal="提案佐證A（synthetic）",
                evidence_golive="上線佐證A（synthetic）",
            ),
            F03ChecklistItem(
                item_id="1-2", topic="透明性", description="示例檢查項目描述1-2（synthetic）",
                evidence_proposal="提案佐證B（synthetic）",
                evidence_golive="上線佐證B（synthetic）",
            ),
        ]
    return F03Checklist(items=items)


def _load_expected(name: str) -> dict:
    return yaml.safe_load((EXPECTED / f"{name}.yaml").read_text(encoding="utf-8"))


def _load_canned(name: str) -> str:
    p = CANNED / name
    return p.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# AC-19：F03RagBatchResponse contract 驗證
# ─────────────────────────────────────────────────────────────────────────────


def test_f03_rag_batch_response_validate_gap_json():
    """AC-19：F03RagBatchResponse.model_validate(gap.json) 成功。"""
    data = json.loads(_load_canned("gap.json"))
    resp = F03RagBatchResponse.model_validate(data)
    assert any(r.verdict == "gap" for r in resp.results)
    assert any(r.verdict == "covered" for r in resp.results)


def test_f03_rag_batch_response_extra_key_raises():
    """AC-19：注入未知鍵 → ValidationError（extra="forbid"）。"""
    data = json.loads(_load_canned("gap.json"))
    data["unexpected_field"] = "oops"
    with pytest.raises(ValidationError):
        F03RagBatchResponse.model_validate(data)


# ─────────────────────────────────────────────────────────────────────────────
# 共用 helper：逐筆 AC-1 + AC-2 驗證
# ─────────────────────────────────────────────────────────────────────────────


def _assert_findings_match(findings, expected_data: dict):
    """逐筆對比 code/severity/source，以及 location_contains（若有）。"""
    exp_list = expected_data["findings"]
    actual_list = [(f.code, f.severity.value, f.source) for f in findings]
    exp_tuples = [(e["code"], e["severity"], e["source"]) for e in exp_list]
    assert actual_list == exp_tuples, (
        f"findings mismatch:\n  actual={actual_list}\n  expected={exp_tuples}"
    )
    # location_contains
    for e in exp_list:
        if "location_contains" in e:
            match = next(
                (f for f in findings if f.code == e["code"] and
                 f.location is not None and e["location_contains"] in f.location),
                None,
            )
            assert match is not None, (
                f"no finding with code={e['code']} and location containing '{e['location_contains']}'"
            )


def _assert_summary_contains(findings, strings: list[str]):
    """AC-2：RAG_SUMMARY.message 含所有指定字串。"""
    summary = next((f for f in findings if f.code == "F03.RAG_SUMMARY"), None)
    assert summary is not None, "RAG_SUMMARY not found"
    for s in strings:
        assert s in summary.message, f"summary missing '{s}': {summary.message!r}"


# ─────────────────────────────────────────────────────────────────────────────
# AC-1 / AC-2：gap 案例
# ─────────────────────────────────────────────────────────────────────────────


def test_gap_canned_findings_and_summary():
    """AC-1 AC-2：gap → RAG_GAP(1-1) + TABLE + SUMMARY；summary 含 '審閱 2 項'/'缺口 1 項'。"""
    expected = _load_expected("gap")
    client = FakeClient([_load_canned("gap.json")])
    mapping = _make_retrieval_map()
    checklist = _make_checklist()

    findings = f03_rag.run_all(checklist, mapping, client, DEFAULT_CFG)
    _assert_findings_match(findings, expected)
    _assert_summary_contains(findings, expected["summary_contains"])


# ─────────────────────────────────────────────────────────────────────────────
# AC-1 / AC-2：covered 案例
# ─────────────────────────────────────────────────────────────────────────────


def test_covered_canned_findings_and_summary():
    """AC-1 AC-2：covered → TABLE + SUMMARY；summary 含 '審閱 2 項'/'缺口 0 項'。"""
    expected = _load_expected("covered")
    client = FakeClient([_load_canned("covered.json")])
    mapping = _make_retrieval_map()
    checklist = _make_checklist()

    findings = f03_rag.run_all(checklist, mapping, client, DEFAULT_CFG)
    _assert_findings_match(findings, expected)
    _assert_summary_contains(findings, expected["summary_contains"])


# ─────────────────────────────────────────────────────────────────────────────
# AC-1 / AC-2：undetermined 案例
# ─────────────────────────────────────────────────────────────────────────────


def test_undetermined_canned_findings_and_summary():
    """AC-1 AC-2：undetermined → RAG_UNDETERMINED(1-1) + TABLE + SUMMARY。"""
    expected = _load_expected("undetermined")
    client = FakeClient([_load_canned("undetermined.json")])
    mapping = _make_retrieval_map()
    checklist = _make_checklist()

    findings = f03_rag.run_all(checklist, mapping, client, DEFAULT_CFG)
    _assert_findings_match(findings, expected)
    _assert_summary_contains(findings, expected["summary_contains"])


# ─────────────────────────────────────────────────────────────────────────────
# AC-1 / AC-2：missing_item 案例
# ─────────────────────────────────────────────────────────────────────────────


def test_missing_item_canned_findings_and_summary():
    """AC-1 AC-2：missing_item → RAG_ITEM_ERROR(1-2) + TABLE + SUMMARY；'1 項判讀失敗'。"""
    expected = _load_expected("missing_item")
    client = FakeClient([_load_canned("missing_item.json")])
    mapping = _make_retrieval_map()
    checklist = _make_checklist()

    findings = f03_rag.run_all(checklist, mapping, client, DEFAULT_CFG)
    _assert_findings_match(findings, expected)
    _assert_summary_contains(findings, expected["summary_contains"])


# ─────────────────────────────────────────────────────────────────────────────
# AC-1 / AC-2 / AC-4：illegal_verdict 案例（白名單容錯）
# ─────────────────────────────────────────────────────────────────────────────


def test_illegal_verdict_downgraded_to_undetermined():
    """AC-1 AC-2 AC-4："maybe" → undetermined → RAG_UNDETERMINED（非拋例外、非丟整批）。"""
    expected = _load_expected("illegal_verdict")
    client = FakeClient([_load_canned("illegal_verdict.json")])
    mapping = _make_retrieval_map()
    checklist = _make_checklist()

    findings = f03_rag.run_all(checklist, mapping, client, DEFAULT_CFG)
    _assert_findings_match(findings, expected)
    _assert_summary_contains(findings, expected["summary_contains"])
    # 不出現 RAG_ERROR（整批未被丟棄）
    codes = [f.code for f in findings]
    assert "F03.RAG_ERROR" not in codes


# ─────────────────────────────────────────────────────────────────────────────
# AC-3：not_json 容錯
# ─────────────────────────────────────────────────────────────────────────────


def test_not_json_produces_item_error_not_abort():
    """AC-3：非 JSON 回應 → RAG_ITEM_ERROR + TABLE + SUMMARY；不出 RAG_ERROR。"""
    expected = _load_expected("not_json")
    client = FakeClient([_load_canned("not_json.txt")])
    mapping = _make_retrieval_map()
    checklist = _make_checklist()

    findings = f03_rag.run_all(checklist, mapping, client, DEFAULT_CFG)
    _assert_findings_match(findings, expected)
    _assert_summary_contains(findings, expected["summary_contains"])
    codes = [f.code for f in findings]
    for absent in expected.get("absent_codes", []):
        assert absent not in codes, f"should not have {absent}"


# ─────────────────────────────────────────────────────────────────────────────
# AC-5：連錯 3 批中止
# ─────────────────────────────────────────────────────────────────────────────


def test_consecutive_errors_abort_after_3():
    """AC-5：連續 3 批 LLMError → RAG_ERROR（WARN），client.calls == 3，後續批不再呼叫。"""
    cfg = RagConfig(batch_size=1, max_items=30, max_sections_per_item=3, max_excerpt_chars=300)
    # 5 個項目（batch_size=1 → 5 批），前 3 批全失敗
    items = [
        F03ChecklistItem(item_id=f"1-{i}", topic="t", description="d",
                         evidence_proposal=f"prop{i}")
        for i in range(5)
    ]
    mapping = _make_retrieval_map(f03_ids=[f"1-{i}" for i in range(5)])
    client = FakeClient([LLMError("refused")] * 5)

    findings = f03_rag.run_all(F03Checklist(items=items), mapping, client, cfg)
    codes = [f.code for f in findings]

    assert client.calls == 3, f"expected 3 calls, got {client.calls}"
    assert "F03.RAG_ERROR" in codes
    assert "F03.RAG_SUMMARY" in codes


# ─────────────────────────────────────────────────────────────────────────────
# AC-6：單批失敗續審
# ─────────────────────────────────────────────────────────────────────────────


def test_single_batch_failure_continues():
    """AC-6：3 批中第 1 批 LLMError，其餘正常 → RAG_ITEM_ERROR，不出 RAG_ERROR，續審完。"""
    cfg = RagConfig(batch_size=1, max_items=30, max_sections_per_item=3, max_excerpt_chars=300)
    items = [
        F03ChecklistItem(item_id=f"1-{i}", evidence_proposal=f"prop{i}")
        for i in range(3)
    ]
    mapping = _make_retrieval_map(f03_ids=["1-0", "1-1", "1-2"])
    # 第 1 批失敗，第 2 / 3 批正常（covered）
    ok_resp = json.dumps({"results": [{"item_id": "1-1", "verdict": "covered", "gap_refs": [], "reason": ""}]})
    ok_resp2 = json.dumps({"results": [{"item_id": "1-2", "verdict": "covered", "gap_refs": [], "reason": ""}]})
    client = FakeClient([LLMError("batch too long"), ok_resp, ok_resp2])

    findings = f03_rag.run_all(F03Checklist(items=items), mapping, client, cfg)
    codes = [f.code for f in findings]

    assert client.calls == 3
    assert "F03.RAG_ITEM_ERROR" in codes
    assert "F03.RAG_ERROR" not in codes
    summary = next(f for f in findings if f.code == "F03.RAG_SUMMARY")
    assert "審閱 2 項" in summary.message
    assert "1 項判讀失敗" in summary.message


# ─────────────────────────────────────────────────────────────────────────────
# AC-7：prompt 預算超限降批
# ─────────────────────────────────────────────────────────────────────────────


def test_budget_split_with_long_excerpts(caplog):
    """AC-7：超大批次使估算超過 _MAX_PROMPT_TOKENS → 批數增加；caplog 不含摘錄/佐證內容。

    透過製造超長 evidence_proposal（佐證欄）使整批的 CJK token 估算超限。
    payload_item 只截 excerpt，不截佐證 → 超長佐證讓整批超出預算 → 自動降批。
    """
    import logging

    # 每項佐證超長（全 CJK，每字 ≈ 1 token）→ 2 項批次估算必然超過 _MAX_PROMPT_TOKENS(6000)
    # 每項佐證 4000 CJK → 2 項 = 8000 CJK tokens，加 template overhead 必超 6000
    long_cjk = "說" * 4000
    items = [
        F03ChecklistItem(
            item_id="1-1", topic="公平性",
            description="示例檢查項目描述1-1（synthetic）",
            evidence_proposal=long_cjk,
            evidence_golive="短上線佐證",
        ),
        F03ChecklistItem(
            item_id="1-2", topic="透明性",
            description="示例檢查項目描述1-2（synthetic）",
            evidence_proposal=long_cjk,
            evidence_golive="短上線佐證",
        ),
    ]
    mapping = _make_retrieval_map()  # 1-1 / 1-2 各有 canonical
    cfg = RagConfig(batch_size=2, max_items=30, max_sections_per_item=3, max_excerpt_chars=300)
    ok_1 = json.dumps({"results": [{"item_id": "1-1", "verdict": "covered", "gap_refs": [], "reason": ""}]})
    ok_2 = json.dumps({"results": [{"item_id": "1-2", "verdict": "covered", "gap_refs": [], "reason": ""}]})
    client = FakeClient([ok_1, ok_2])

    with caplog.at_level(logging.INFO, logger="govcheck"):
        findings = f03_rag.run_all(F03Checklist(items=items), mapping, client, cfg)

    # batch_size=2 但預算超限 → 自動降為 2 個單項批
    assert client.calls == 2, f"expected 2 single-item batches, got {client.calls}"
    # caplog 不含佐證內容（"說" * 4000）
    for rec in caplog.records:
        assert long_cjk not in rec.getMessage(), "佐證內容洩漏到 log"
    # 有 RAG_SUMMARY
    assert any(f.code == "F03.RAG_SUMMARY" for f in findings)


# ─────────────────────────────────────────────────────────────────────────────
# AC-8：TEMPLATE_REF_MODIFIED 正例
# ─────────────────────────────────────────────────────────────────────────────


def test_template_ref_modified_positive():
    """AC-8：上傳 regulation_ref_raw 與 canonical 不符 → F03.TEMPLATE_REF_MODIFIED（WARN）。"""
    class PatchedItem(F03ChecklistItem):
        regulation_ref_raw: str | None = None

    canonical_desc = "示例檢查項目描述1-1（synthetic）"
    patched = PatchedItem(
        item_id="1-1", topic="公平性",
        description=canonical_desc,  # description 一致，但 regulation_ref_raw 不符
        evidence_proposal="佐證A", evidence_golive="佐證A上線",
        regulation_ref_raw="R99/偽造條文",  # 與 canonical "R03&R07/五/(二)/2" 不符
    )
    # 只有 1-1 以避免 1-2 的 description 也造成誤觸
    checklist = F03Checklist(items=[patched])
    mapping = _make_retrieval_map(f03_ids=["1-1"])
    ok_resp = json.dumps({"results": [
        {"item_id": "1-1", "verdict": "covered", "gap_refs": [], "reason": ""},
    ]})
    client = FakeClient([ok_resp])

    findings = f03_rag.run_all(checklist, mapping, client, DEFAULT_CFG)
    codes = [f.code for f in findings]
    assert "F03.TEMPLATE_REF_MODIFIED" in codes
    modified = next(f for f in findings if f.code == "F03.TEMPLATE_REF_MODIFIED")
    assert modified.severity.value == "warn"
    assert "1-1" in (modified.location or "")


# ─────────────────────────────────────────────────────────────────────────────
# AC-9：TEMPLATE_REF_MODIFIED 反例（相符不觸發）
# ─────────────────────────────────────────────────────────────────────────────


def test_template_ref_modified_negative():
    """AC-9：上傳 regulation_ref_raw 與 canonical 相符 → 不出 TEMPLATE_REF_MODIFIED。"""
    class PatchedItem(F03ChecklistItem):
        regulation_ref_raw: str | None = None

    patched = PatchedItem(
        item_id="1-1", topic="公平性",
        description="示例檢查項目描述1-1（synthetic）",  # 與 canonical 相符
        evidence_proposal="佐證A", evidence_golive="佐證A上線",
        regulation_ref_raw="R03&R07/五/(二)/2",  # 與 canonical 相符
    )
    checklist = F03Checklist(items=[patched])
    # mapping 只含 1-1；canonical_ref_raw 預設也是 "R03&R07/五/(二)/2"
    mapping = _make_retrieval_map(f03_ids=["1-1"])
    ok_resp = json.dumps({"results": [
        {"item_id": "1-1", "verdict": "covered", "gap_refs": [], "reason": ""},
    ]})
    client = FakeClient([ok_resp])

    findings = f03_rag.run_all(checklist, mapping, client, DEFAULT_CFG)
    codes = [f.code for f in findings]
    assert "F03.TEMPLATE_REF_MODIFIED" not in codes


# ─────────────────────────────────────────────────────────────────────────────
# AC-17：隱私 log
# ─────────────────────────────────────────────────────────────────────────────


def test_privacy_batch_failure_does_not_leak(tmp_path):
    """AC-17：批失敗 log 只記型別，不記 LLMError 訊息本文（可能夾帶端點回應內容）。"""
    setup_logging()
    secret = "SUPER_SECRET_BODY_xyz"
    cfg = RagConfig(batch_size=1, max_items=30, max_sections_per_item=3, max_excerpt_chars=300)
    items = [
        F03ChecklistItem(item_id="1-0", evidence_proposal="x"),
        F03ChecklistItem(item_id="1-1", evidence_proposal="y"),
    ]
    mapping = _make_retrieval_map(f03_ids=["1-0", "1-1"])
    ok_resp = json.dumps({"results": [{"item_id": "1-1", "verdict": "covered", "gap_refs": [], "reason": ""}]})
    client = FakeClient([LLMError(f"HTTP 500 回應：{secret}"), ok_resp])

    f03_rag.run_all(F03Checklist(items=items), mapping, client, cfg)

    ops_log = (Path(load_log_config()["dir"]) / "govcheck.log").read_text(encoding="utf-8")
    assert "f03 rag batch" in ops_log
    assert "LLMError" in ops_log
    assert secret not in ops_log


# ─────────────────────────────────────────────────────────────────────────────
# AC-18：絕不 raise
# ─────────────────────────────────────────────────────────────────────────────


def test_run_all_client_none_returns_empty():
    """AC-18：client=None → []（永不 raise）。"""
    mapping = _make_retrieval_map()
    checklist = _make_checklist()
    result = f03_rag.run_all(checklist, mapping, None, DEFAULT_CFG)
    assert result == []


def test_run_all_none_checklist_returns_empty():
    """AC-18：checklist=None → []（永不 raise）。"""
    mapping = _make_retrieval_map()
    result = f03_rag.run_all(None, mapping, FakeClient([]), DEFAULT_CFG)  # type: ignore[arg-type]
    assert result == []


def test_run_all_sheet_not_present_returns_empty():
    """AC-18：sheet_present=False → []。"""
    mapping = _make_retrieval_map()
    checklist = F03Checklist(items=[], sheet_present=False)
    result = f03_rag.run_all(checklist, mapping, FakeClient([]), DEFAULT_CFG)
    assert result == []


def test_run_all_bad_client_does_not_raise():
    """AC-18：client 任何呼叫丟非 LLMError 例外 → 回 Finding（不逸出）。"""
    class BrokenClient:
        def chat(self, messages, **kw):
            raise RuntimeError("internal crash")

    mapping = _make_retrieval_map()
    checklist = _make_checklist()
    result = f03_rag.run_all(checklist, mapping, BrokenClient(), DEFAULT_CFG)  # type: ignore[arg-type]
    # 應有 Finding 而非例外逸出
    assert isinstance(result, list)
    # 應包含 RAG_SUMMARY（如果有目標項且沒被 outer except 攔截）或 RAG_ERROR
    codes = [f.code for f in result]
    assert "F03.RAG_SUMMARY" in codes or "F03.RAG_ERROR" in codes


def test_run_all_no_targets_returns_empty():
    """AC-18：無目標項（無佐證 / 不在 map）→ [] 不 raise。"""
    mapping = _make_retrieval_map()
    # 項目沒有佐證
    checklist = F03Checklist(items=[
        F03ChecklistItem(item_id="1-1", topic="t"),  # 無佐證
    ])
    result = f03_rag.run_all(checklist, mapping, FakeClient([]), DEFAULT_CFG)
    assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# gap_refs 交集邏輯驗證（裁決 5）
# ─────────────────────────────────────────────────────────────────────────────


def test_gap_refs_intersection_with_canonical():
    """裁決 5：gap_refs 非空 → 與 canonical 顯示字串做交集；交集非空 → 列交集。"""
    # canonical 有兩個 section：R03 五、(二) 2 和 R07 五、(二) 2
    # gap_refs = ["R03 五、(二) 2"] → 只列 R03 那條
    resp = json.dumps({"results": [
        {"item_id": "1-1", "verdict": "gap", "gap_refs": ["R03 五、(二) 2"], "reason": "缺口"},
    ]})
    # 只測試 1-1
    mapping = _make_retrieval_map(f03_ids=["1-1"])
    checklist = F03Checklist(items=[
        F03ChecklistItem(item_id="1-1", evidence_proposal="佐證"),
    ])
    client = FakeClient([resp])
    findings = f03_rag.run_all(checklist, mapping, client, DEFAULT_CFG)
    gap_finding = next(f for f in findings if f.code == "F03.RAG_GAP")
    # 只含 R03，不含 R07
    assert "R03 五、(二) 2" in gap_finding.message
    assert "R07 五、(二) 2" not in gap_finding.message


def test_gap_refs_empty_falls_back_to_all_canonical():
    """裁決 5：gap_refs 為空 → 列全部 canonical 引用。"""
    resp = json.dumps({"results": [
        {"item_id": "1-1", "verdict": "gap", "gap_refs": [], "reason": "全部缺口"},
    ]})
    mapping = _make_retrieval_map(f03_ids=["1-1"])
    checklist = F03Checklist(items=[
        F03ChecklistItem(item_id="1-1", evidence_proposal="佐證"),
    ])
    client = FakeClient([resp])
    findings = f03_rag.run_all(checklist, mapping, client, DEFAULT_CFG)
    gap_finding = next(f for f in findings if f.code == "F03.RAG_GAP")
    # 兩條 canonical 都列
    assert "R03 五、(二) 2" in gap_finding.message
    assert "R07 五、(二) 2" in gap_finding.message


def test_gap_refs_no_intersection_falls_back_to_all_canonical():
    """裁決 5：gap_refs 非空但無交集 → fallback 列全部 canonical。"""
    resp = json.dumps({"results": [
        {"item_id": "1-1", "verdict": "gap",
         "gap_refs": ["R99 不存在條文"],  # 完全無交集
         "reason": "缺口"},
    ]})
    mapping = _make_retrieval_map(f03_ids=["1-1"])
    checklist = F03Checklist(items=[
        F03ChecklistItem(item_id="1-1", evidence_proposal="佐證"),
    ])
    client = FakeClient([resp])
    findings = f03_rag.run_all(checklist, mapping, client, DEFAULT_CFG)
    gap_finding = next(f for f in findings if f.code == "F03.RAG_GAP")
    # fallback：兩條 canonical 都列
    assert "R03 五、(二) 2" in gap_finding.message
    assert "R07 五、(二) 2" in gap_finding.message
