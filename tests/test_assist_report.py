"""填寫建議書 Markdown：標題／免責、各表表格、未涵蓋標示、出處格式、管線逸出、執行資訊。"""

from __future__ import annotations

from govcheck.assist.models import AssistResult, Suggestion
from govcheck.models import (
    Citation,
    DocumentArtifact,
    DocumentParseStatus,
    Finding,
    PromptRunRef,
    Severity,
    UsageSummary,
)
from govcheck.report import builder
from govcheck.report.assist_builder import DISCLAIMER, UNCOVERED_CELL, to_markdown


def _citation(quote: str = "本案由甲方單位提出") -> Citation:
    return Citation(
        document_id="doc1",
        unit_id="doc1:1",
        excerpt_id="abcd",
        locator_type="page",
        locator_value="2",
        quote=quote,
    )


def _result(**overrides) -> AssistResult:
    base = dict(
        assist_id="assist-123",
        forms=["f01", "f02", "f03"],
        suggestions=[
            Suggestion(
                form="f01", field_id="test_a", label="測試欄位A", cell="A4", kind="text",
                covered=True, suggestion="甲方單位 | 資訊處", confidence=0.87,
                citations=[_citation()], rationale="片段直接寫明",
            ),
            Suggestion(
                form="f02", field_id="Q-01", label="測試題目一", cell="C3", kind="yes_no",
                covered=True, suggestion="Y", confidence=0.6, citations=[_citation("已完成評估")],
            ),
            Suggestion(
                form="f02", field_id="Q-02", label="測試題目二", cell="C4", kind="yes_no",
                covered=False, citations=[_citation("可能相關")],
            ),
            Suggestion(
                form="f03", field_id="1-1", label="測試檢核項1", cell="I5", kind="text",
                covered=False,
            ),
        ],
        document_statuses=[
            DocumentArtifact(
                document_id="doc1", filename_hash="h", media_type="text/plain",
                parser_config_hash="c", status=DocumentParseStatus.PARSED,
            ),
            DocumentArtifact(
                document_id="doc2", filename_hash="h", media_type="text/plain",
                parser_config_hash="c", status=DocumentParseStatus.UNREADABLE,
            ),
        ],
        notices=[Finding(
            severity=Severity.INFO, code="ASSIST.LLM_FAILED", title="部分欄位失敗",
            message="有 1 個欄位未產生建議", source="llm",
        )],
        usage=UsageSummary(review_llm_primary_calls=2, input_tokens=120, output_tokens=40,
                           evidence_embedding_calls=1, latency_ms=35),
        prompt_refs=[PromptRunRef(
            prompt_id="govcheck.assist.prefill", prompt_version="1",
            content_hash="0123456789abcdef0123", schema_version="assist-prefill-v1",
        )],
        mode="llm",
    )
    base.update(overrides)
    return AssistResult(**base)


def test_markdown_has_title_disclaimer_and_summary():
    text = to_markdown(_result())

    assert text.startswith("# 填寫建議書（AI 協填草稿）\n")
    assert f"> {DISCLAIMER}" in text
    assert "不代填、不匯出表單" in text
    assert "**協填編號**：assist-123" in text
    assert "已建議 2 / 未涵蓋 2" in text
    assert "1/2 份可讀" in text


def test_each_form_has_table_with_expected_columns_and_rows():
    text = to_markdown(_result())

    header = "| 欄位（儲存格） | 建議內容 | 依據出處 | 信心 |"
    assert text.count(header) == 3
    assert "## F01 AI系統資訊表" in text
    assert "## F02 AI風險評鑑" in text
    assert "## F03 上線檢核表" in text
    # 覆蓋列：建議、出處格式 doc · locator:value · 「quote」、信心兩位小數
    assert "| 測試欄位A（A4） | 甲方單位 \\| 資訊處 | doc1 · page:2 · 「本案由甲方單位提出」 | 0.87 |" in text
    assert "| 測試題目一（C3） | Y | doc1 · page:2 · 「已完成評估」 | 0.60 |" in text


def test_uncovered_rows_are_marked_and_listed():
    text = to_markdown(_result())

    assert f"| 測試題目二（C4） | {UNCOVERED_CELL} | 候選：doc1 · page:2 · 「可能相關」 | — |" in text
    assert f"| 測試檢核項1（I5） | {UNCOVERED_CELL} | — | — |" in text
    section = text.split("## 未涵蓋欄位")[1].split("## 執行資訊")[0]
    assert "- F02 AI風險評鑑：測試題目二（C4）" in section
    assert "- F03 上線檢核表：測試檢核項1（I5）" in section
    assert "測試欄位A" not in section


def test_run_info_lists_prompt_refs_usage_and_notices():
    text = to_markdown(_result())

    info = text.split("## 執行資訊")[1]
    assert "`govcheck.assist.prefill` 版本 1" in info
    assert "schema assist-prefill-v1" in info
    assert "hash 0123456789ab" in info
    assert "模型呼叫 2 次" in info
    assert "輸入 120 tokens" in info
    assert "embedding 呼叫 1 次" in info
    assert "parsed 1" in info and "unreadable 1" in info
    assert "## 提示" in text
    assert "`ASSIST.LLM_FAILED` 部分欄位失敗：有 1 個欄位未產生建議" in text


def test_retrieval_only_and_empty_forms_render_gracefully():
    text = to_markdown(_result(
        mode="retrieval_only", prompt_refs=[], notices=[], forms=["f01"],
        suggestions=[], document_statuses=[], usage=UsageSummary(),
    ))

    assert "僅候選出處" in text
    assert "（本表沒有可處理的欄位）" in text
    assert "## 未涵蓋欄位\n\n（無）" in text
    assert "- prompt：未使用模型" in text
    assert "## 提示" not in text
    assert "佐證文件狀態：（無）" in text
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_table_cell_is_shared_with_review_report():
    assert builder.table_cell("a|b\nc") == "a\\|b c"
    assert builder._table_cell is builder.table_cell  # 舊名保留給既有呼叫端
