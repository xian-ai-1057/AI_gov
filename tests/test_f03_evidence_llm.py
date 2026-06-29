"""F03 佐證 LLM 判讀檢查：以假 client 注入罐裝 JSON，不打真端點。"""

from __future__ import annotations

from govcheck.checks.llm import f03_evidence
from govcheck.llm.client import LLMError
from govcheck.models import F03Checklist, F03ChecklistItem


class FakeClient:
    """依序回傳預先準備的輸出；輸出為 Exception 時改用 raise（模擬端點/解析失敗）。"""

    def __init__(self, outputs: list):
        self._outputs = list(outputs)
        self.calls = 0

    def chat(self, messages, **kw) -> str:
        self.calls += 1
        out = self._outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


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
    client = FakeClient([
        '{"difference":{"flag":true,"summary":"上線內容與提案不一致但未說明差異"},'
        '"proposal":{"vague":false,"reason":""},"golive":{"vague":false,"reason":""}}',
        '{"difference":{"flag":false,"summary":""},'
        '"proposal":{"vague":true,"reason":"僅填OK過於空泛"},"golive":{"vague":false,"reason":""}}',
        '{"difference":{"flag":false},"proposal":{"vague":false},"golive":{"vague":false}}',
    ])
    fs = f03_evidence.run_all(_checklist(), client, max_items=30)
    codes = [f.code for f in fs]

    assert client.calls == 3  # 第 4 項無佐證被跳過
    assert "F03.LLM_DIFF" in codes
    assert "F03.LLM_VAGUE_PROPOSAL" in codes
    assert "F03.LLM_SUMMARY" in codes
    assert all(f.source == "llm" for f in fs)

    diff = next(f for f in fs if f.code == "F03.LLM_DIFF")
    assert "1-1" in diff.location
    summary = next(f for f in fs if f.code == "F03.LLM_SUMMARY")
    assert "審閱 3 項" in summary.message and "標示 2 項" in summary.message


def test_clean_items_only_summary():
    client = FakeClient(['{"difference":{"flag":false},"proposal":{"vague":false},"golive":{"vague":false}}'])
    cl = F03Checklist(items=[F03ChecklistItem(item_id="1-1", evidence_proposal="具體內容")])
    fs = f03_evidence.run_all(cl, client)
    assert [f.code for f in fs] == ["F03.LLM_SUMMARY"]


def test_single_item_error_is_tolerated_and_continues():
    """單項失敗不應拖垮整體召回：記一筆 INFO 後續審其餘項。"""
    good = '{"difference":{"flag":false},"proposal":{"vague":false},"golive":{"vague":false}}'
    client = FakeClient([LLMError("item too long"), good, good])
    cl = F03Checklist(items=[
        F03ChecklistItem(item_id="1-1", evidence_proposal="x"),
        F03ChecklistItem(item_id="1-2", evidence_proposal="y"),
        F03ChecklistItem(item_id="1-3", evidence_proposal="z"),
    ])
    fs = f03_evidence.run_all(cl, client)
    codes = [f.code for f in fs]
    assert client.calls == 3                     # 續審完所有項
    assert "F03.LLM_ITEM_ERROR" in codes         # 單項失敗記錄
    assert "F03.LLM_ERROR" not in codes          # 未達連續失敗門檻 → 不中止
    summary = next(f for f in fs if f.code == "F03.LLM_SUMMARY")
    assert "審閱 2 項" in summary.message and "1 項判讀失敗" in summary.message


def test_consecutive_errors_abort():
    """連續多次失敗（端點異常）→ 中止，避免逐項耗到 timeout。"""
    client = FakeClient([LLMError("refused")] * 5)
    cl = F03Checklist(items=[F03ChecklistItem(item_id=f"1-{i}", evidence_proposal="x") for i in range(5)])
    fs = f03_evidence.run_all(cl, client)
    codes = [f.code for f in fs]
    assert client.calls == 3                      # 連續 3 次即中止
    assert "F03.LLM_ERROR" in codes
    assert "F03.LLM_SUMMARY" in codes


def test_malformed_json_tolerated_as_item_error():
    client = FakeClient(["這不是 JSON"])
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
    client = FakeClient(['{"difference":{"flag":false},"proposal":{"vague":false},"golive":{"vague":false}}'] * 2)
    fs = f03_evidence.run_all(F03Checklist(items=items), client, max_items=2)
    assert client.calls == 2
    summary = next(f for f in fs if f.code == "F03.LLM_SUMMARY")
    assert "未送審 3 項" in summary.message
