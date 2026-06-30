"""F03 佐證 LLM 判讀檢查（分批 + 彙整表）：以假 client 注入罐裝 JSON，不打真端點。"""

from __future__ import annotations

import json

from govcheck.checks.llm import f03_evidence
from govcheck.llm.client import LLMError
from govcheck.models import F03Checklist, F03ChecklistItem


class FakeClient:
    """依序回傳預先準備的「每批一個」輸出；輸出為 Exception 時改用 raise（模擬端點/解析失敗）。"""

    def __init__(self, outputs: list):
        self._outputs = list(outputs)
        self.calls = 0

    def chat(self, messages, **kw) -> str:
        self.calls += 1
        out = self._outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _v(item_id, *, diff=False, diff_summary="", prop=False, prop_reason="", gol=False, gol_reason=""):
    """組一筆 results 項目（單一檢核項的判讀結果）。"""
    return {
        "item_id": item_id,
        "difference": {"flag": diff, "summary": diff_summary},
        "proposal": {"vague": prop, "reason": prop_reason},
        "golive": {"vague": gol, "reason": gol_reason},
    }


def _resp(*verdicts) -> str:
    """把多筆判讀包成一個批次回應 JSON（{"results":[...]}）。"""
    return json.dumps({"results": list(verdicts)}, ensure_ascii=False)


def _checklist() -> F03Checklist:
    return F03Checklist(items=[
        F03ChecklistItem(item_id="1-1", topic="公平性", description="d1",
                         evidence_proposal="提案內容A", evidence_golive="上線內容A"),
        F03ChecklistItem(item_id="1-2", topic="透明性", description="d2",
                         evidence_proposal="OK", evidence_golive=None),
        F03ChecklistItem(item_id="2-1", topic="業務範疇", description="d3",
                         evidence_proposal="完整且具體的提案說明……", evidence_golive="完整且具體的上線說明……"),
        F03ChecklistItem(item_id="3-1", topic="公平性", description="d4"),  # 無佐證 → 應跳過
    ])


def test_maps_difference_and_vagueness():
    # 3 個有佐證項在預設 batch_size 下併成一批 → 一次呼叫
    client = FakeClient([_resp(
        _v("1-1", diff=True, diff_summary="上線內容與提案不一致但未說明差異"),
        _v("1-2", prop=True, prop_reason="僅填OK過於空泛"),
        _v("2-1"),
    )])
    fs = f03_evidence.run_all(_checklist(), client, max_items=30)
    codes = [f.code for f in fs]

    assert client.calls == 1  # 批次：3 項一次送，第 4 項無佐證被排除
    assert "F03.LLM_DIFF" in codes
    assert "F03.LLM_VAGUE_PROPOSAL" in codes
    assert "F03.LLM_TABLE" in codes
    assert "F03.LLM_SUMMARY" in codes
    assert all(f.source == "llm" for f in fs)

    diff = next(f for f in fs if f.code == "F03.LLM_DIFF")
    assert "1-1" in diff.location
    summary = next(f for f in fs if f.code == "F03.LLM_SUMMARY")
    assert "審閱 3 項" in summary.message and "標示 2 項" in summary.message


def test_summary_table_present():
    client = FakeClient([_resp(
        _v("1-1", diff=True, diff_summary="上線未說明差異"),
        _v("1-2", prop=True, prop_reason="僅填OK"),
        _v("2-1"),
    )])
    fs = f03_evidence.run_all(_checklist(), client)
    table = next(f for f in fs if f.code == "F03.LLM_TABLE")
    assert "| 項次 |" in table.message            # Markdown 表頭
    assert "\n" in table.message                   # 多行 → builder 會以區塊渲染
    for iid in ("1-1", "1-2", "2-1"):
        assert iid in table.message                # 每項都在彙整表一覽


def test_batch_chunking_multiple_calls():
    items = [F03ChecklistItem(item_id=f"9-{i}", topic="t", evidence_proposal=f"內容{i}") for i in range(5)]
    client = FakeClient([
        _resp(_v("9-0"), _v("9-1")),
        _resp(_v("9-2"), _v("9-3")),
        _resp(_v("9-4")),
    ])
    fs = f03_evidence.run_all(F03Checklist(items=items), client, batch_size=2)
    assert client.calls == 3                        # 5 項 / 每批 2 → 3 批
    summary = next(f for f in fs if f.code == "F03.LLM_SUMMARY")
    assert "審閱 5 項" in summary.message


def test_clean_items_only_table_and_summary():
    client = FakeClient([_resp(_v("1-1"))])
    cl = F03Checklist(items=[F03ChecklistItem(item_id="1-1", evidence_proposal="具體內容")])
    fs = f03_evidence.run_all(cl, client)
    assert [f.code for f in fs] == ["F03.LLM_TABLE", "F03.LLM_SUMMARY"]


def test_single_batch_error_is_tolerated_and_continues():
    """單批失敗不應拖垮整體召回：記一筆 INFO 後續審其餘批。"""
    client = FakeClient([LLMError("batch too long"), _resp(_v("1-2")), _resp(_v("1-3"))])
    cl = F03Checklist(items=[
        F03ChecklistItem(item_id="1-1", evidence_proposal="x"),
        F03ChecklistItem(item_id="1-2", evidence_proposal="y"),
        F03ChecklistItem(item_id="1-3", evidence_proposal="z"),
    ])
    fs = f03_evidence.run_all(cl, client, batch_size=1)
    codes = [f.code for f in fs]
    assert client.calls == 3                     # 續審完所有批
    assert "F03.LLM_ITEM_ERROR" in codes         # 單批失敗記錄
    assert "F03.LLM_ERROR" not in codes          # 未達連續失敗門檻 → 不中止
    summary = next(f for f in fs if f.code == "F03.LLM_SUMMARY")
    assert "審閱 2 項" in summary.message and "1 項判讀失敗" in summary.message


def test_consecutive_errors_abort():
    """連續多批失敗（端點異常）→ 中止，避免逐批耗到 timeout。"""
    client = FakeClient([LLMError("refused")] * 5)
    cl = F03Checklist(items=[F03ChecklistItem(item_id=f"1-{i}", evidence_proposal="x") for i in range(5)])
    fs = f03_evidence.run_all(cl, client, batch_size=1)
    codes = [f.code for f in fs]
    assert client.calls == 3                      # 連續 3 批即中止
    assert "F03.LLM_ERROR" in codes
    assert "F03.LLM_SUMMARY" in codes


def test_missing_item_in_results_recorded():
    """模型漏回某 item_id → 該項記 INFO，其餘正常。"""
    client = FakeClient([_resp(_v("1-1"))])  # 缺 1-2
    cl = F03Checklist(items=[
        F03ChecklistItem(item_id="1-1", evidence_proposal="x"),
        F03ChecklistItem(item_id="1-2", evidence_proposal="y"),
    ])
    fs = f03_evidence.run_all(cl, client)
    assert client.calls == 1
    item_err = next(f for f in fs if f.code == "F03.LLM_ITEM_ERROR")
    assert "1-2" in item_err.location
    summary = next(f for f in fs if f.code == "F03.LLM_SUMMARY")
    assert "審閱 1 項" in summary.message


def test_malformed_json_tolerated_as_item_error():
    client = FakeClient(["這不是 JSON"])
    cl = F03Checklist(items=[F03ChecklistItem(item_id="1-1", evidence_proposal="x")])
    fs = f03_evidence.run_all(cl, client)
    assert "F03.LLM_ITEM_ERROR" in [f.code for f in fs]


def test_results_array_missing_tolerated_as_item_error():
    client = FakeClient(['{"foo":"bar"}'])  # 合法 JSON 但缺 results 陣列
    cl = F03Checklist(items=[F03ChecklistItem(item_id="1-1", evidence_proposal="x")])
    fs = f03_evidence.run_all(cl, client)
    assert "F03.LLM_ITEM_ERROR" in [f.code for f in fs]


def test_none_client_returns_empty():
    cl = F03Checklist(items=[F03ChecklistItem(item_id="1-1", evidence_proposal="x")])
    assert f03_evidence.run_all(cl, None) == []


def test_no_evidence_targets_returns_empty():
    """無任何佐證可審 → 回空（不輸出摘要噪音；缺漏由規則檢查負責）。"""
    cl = F03Checklist(items=[
        F03ChecklistItem(item_id="1-1", check_state="是"),   # 完成但無佐證
        F03ChecklistItem(item_id="1-2", check_state="不適用"),
    ])
    client = FakeClient([])
    fs = f03_evidence.run_all(cl, client)
    assert fs == []
    assert client.calls == 0


def test_max_items_truncates_with_notice():
    items = [F03ChecklistItem(item_id=f"9-{i}", evidence_proposal=f"內容{i}") for i in range(5)]
    client = FakeClient([_resp(_v("9-0"), _v("9-1"))])  # 截斷到 2 項 → 一批一次呼叫
    fs = f03_evidence.run_all(F03Checklist(items=items), client, max_items=2)
    assert client.calls == 1
    summary = next(f for f in fs if f.code == "F03.LLM_SUMMARY")
    assert "未送審 3 項" in summary.message
