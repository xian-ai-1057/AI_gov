from __future__ import annotations

from govcheck.evidence import EvidenceRetriever
from govcheck.evidence import retrieval as retrieval_module
from govcheck.models import DocumentArtifact, DocumentParseStatus, SourceUnit


class _Embeddings:
    def __init__(self, fail: bool = False) -> None:
        self.calls = []
        self.fail = fail

    def embed(self, texts):
        self.calls.append(texts)
        if self.fail:
            raise RuntimeError("synthetic")
        return [[float(index + 1), 1.0] for index, _ in enumerate(texts)]


class _SemanticTargetEmbeddings:
    def __init__(self) -> None:
        self.calls = []

    def embed(self, texts):
        self.calls.append(texts)
        return [
            [1.0, 0.0] if text == "語意查詢" or "SEMANTIC_TARGET" in text else [0.0, 1.0]
            for text in texts
        ]


def _artifact() -> DocumentArtifact:
    return DocumentArtifact(
        document_id="doc",
        filename_hash="hash",
        media_type="text/plain",
        parser_config_hash="cfg",
        status=DocumentParseStatus.PARSED,
        source_units=[
            SourceUnit(unit_id="u1", content="公平性測試結果", locator_value="1"),
            SourceUnit(unit_id="u2", content="資訊安全弱點掃描", locator_value="2"),
        ],
    )


def test_retrieve_many_delegates_embedding_batching_to_client():
    embeddings = _Embeddings()
    retriever = EvidenceRetriever(embedding_client=embeddings)

    results = retriever.retrieve_many(["公平性", "安全"], [_artifact()], top_k=1)

    assert len(embeddings.calls) == 1
    assert retriever.embedding_calls == 1
    assert len(embeddings.calls[0]) == 4  # two queries + two source units
    assert len(results) == 2
    assert all(len(rows) == 1 for rows in results)


def test_embedding_failure_degrades_to_lexical_results():
    retriever = EvidenceRetriever(embedding_client=_Embeddings(fail=True))

    results = retriever.retrieve_many(["公平性"], [_artifact()], top_k=1)

    assert results[0][0].unit_id == "u1"


def test_incidental_term_overlap_is_not_returned_as_evidence():
    """BM25 正規化後最高分恆為 1.0；沒有絕對門檻的話，任何偶然重疊都會變成可引用佐證。"""
    artifact = DocumentArtifact(
        document_id="doc1",
        filename_hash="h",
        media_type="text/plain",
        parser_config_hash="c",
        status=DocumentParseStatus.PARSED,
        source_units=[SourceUnit(
            unit_id="doc1:1",
            content="本文件說明午餐訂購流程與報帳方式。",
            locator_value="1",
        )],
    )

    hits = EvidenceRetriever().retrieve("公平性測試結果與群組差異記錄", [artifact], top_k=4)

    assert hits == []


def test_long_evidence_uses_bounded_verbatim_window_near_query():
    relevant = "公平性測試結果顯示各群組差異均低於門檻。"
    artifact = DocumentArtifact(
        document_id="doc-long",
        filename_hash="h",
        media_type="text/plain",
        parser_config_hash="c",
        status=DocumentParseStatus.PARSED,
        source_units=[SourceUnit(
            unit_id="doc-long:1",
            content="前段背景資料。" * 100 + relevant + "後段附件資料。" * 100,
            locator_value="1",
        )],
    )

    hits = EvidenceRetriever(max_content_chars=120).retrieve(
        "公平性測試結果與群組差異", [artifact], top_k=1
    )

    assert len(hits[0].content) <= 120
    assert "公平性測試結果" in hits[0].content
    assert hits[0].content.replace("…", "") in artifact.source_units[0].content


def test_excerpt_prefers_dense_query_match_over_early_generic_term():
    relevant = "模型公平性測試結果包含群組差異與量化門檻。"
    artifact = DocumentArtifact(
        document_id="doc-density",
        filename_hash="h",
        media_type="text/plain",
        parser_config_hash="c",
        status=DocumentParseStatus.PARSED,
        source_units=[SourceUnit(
            unit_id="doc-density:1",
            content="模型概述。" + "一般背景。" * 100 + relevant,
            locator_value="1",
        )],
    )

    hits = EvidenceRetriever(max_content_chars=100).retrieve(
        "模型公平性測試結果與群組差異", [artifact], top_k=1
    )

    assert relevant in hits[0].content


def test_semantic_hit_reuses_the_exact_embedded_source_window():
    content = "甲" * 250 + "SEMANTIC_TARGET" + "乙" * 750
    artifact = DocumentArtifact(
        document_id="doc-semantic",
        filename_hash="h",
        media_type="text/plain",
        parser_config_hash="c",
        status=DocumentParseStatus.PARSED,
        source_units=[SourceUnit(
            unit_id="doc-semantic:1",
            content=content,
            locator_value="1",
        )],
    )
    embeddings = _SemanticTargetEmbeddings()

    hits = EvidenceRetriever(
        embedding_client=embeddings,
        lexical_weight=0.7,
        max_content_chars=120,
    ).retrieve("語意查詢", [artifact], top_k=1)

    assert len(hits) == 1
    assert "SEMANTIC_TARGET" in hits[0].content
    assert hits[0].content in content
    embedded_chunks = embeddings.calls[0][1:]
    assert hits[0].content in embedded_chunks
    assert all(len(chunk) <= 120 for chunk in embedded_chunks)


def test_excerpt_maps_spaced_ocr_cjk_match_back_to_original_text():
    spaced = "公 平 性 測 試 結 果"
    artifact = DocumentArtifact(
        document_id="doc-ocr",
        filename_hash="h",
        media_type="text/plain",
        parser_config_hash="c",
        status=DocumentParseStatus.PARSED,
        source_units=[SourceUnit(
            unit_id="doc-ocr:1",
            content="前段。" * 100 + spaced + "後段。" * 100,
            locator_value="1",
        )],
    )

    hits = EvidenceRetriever(max_content_chars=100).retrieve(
        "公平性測試結果", [artifact], top_k=1
    )

    assert spaced in hits[0].content


def test_embedding_corpus_has_a_total_chunk_limit_but_lexical_keeps_full_coverage():
    target = "最後的公平性測試結果"
    artifact = DocumentArtifact(
        document_id="doc-limit",
        filename_hash="h",
        media_type="text/plain",
        parser_config_hash="c",
        status=DocumentParseStatus.PARSED,
        source_units=[SourceUnit(
            unit_id="doc-limit:1",
            content="背景" * 3000 + target,
            locator_value="1",
        )],
    )
    embeddings = _Embeddings()

    hits = EvidenceRetriever(
        embedding_client=embeddings,
        max_content_chars=100,
        max_embedding_chunks=5,
    ).retrieve("公平性測試結果", [artifact], top_k=1)

    assert len(embeddings.calls[0]) == 6  # one query + five uniformly sampled chunks
    assert target in hits[0].content  # lexical still evaluates every chunk, including the tail


def test_streaming_lexical_finds_relevant_chunk_between_embedding_samples():
    target = "中段安全性掃描結果"
    artifact = DocumentArtifact(
        document_id="doc-local-limit",
        filename_hash="h",
        media_type="text/plain",
        parser_config_hash="c",
        status=DocumentParseStatus.PARSED,
        source_units=[SourceUnit(
            unit_id="doc-local-limit:1",
            content="背景" * 225 + target + "後段" * 700,
            locator_value="1",
        )],
    )
    embeddings = _Embeddings()
    hits = EvidenceRetriever(
        embedding_client=embeddings,
        max_content_chars=100,
        max_embedding_chunks=7,
    ).retrieve("安全性掃描結果", [artifact], top_k=1)

    assert all(target not in chunk for chunk in embeddings.calls[0][1:])
    assert target in hits[0].content


def test_unsampled_semantic_only_empty_result_is_marked_incomplete():
    content = "甲" * 252 + "SEMANTIC_TARGET" + "乙" * 800
    artifact = DocumentArtifact(
        document_id="doc-semantic-limit",
        filename_hash="h",
        media_type="text/plain",
        parser_config_hash="c",
        status=DocumentParseStatus.PARSED,
        source_units=[SourceUnit(
            unit_id="doc-semantic-limit:1",
            content=content,
            locator_value="1",
        )],
    )
    embeddings = _SemanticTargetEmbeddings()
    retriever = EvidenceRetriever(
        embedding_client=embeddings,
        max_content_chars=100,
        max_embedding_chunks=3,
    )

    hits = retriever.retrieve("語意查詢", [artifact], top_k=1)

    assert hits == []
    assert all("SEMANTIC_TARGET" not in chunk for chunk in embeddings.calls[0][1:])
    assert retriever.last_incomplete_query_indices == frozenset({0})


def test_embedding_sampling_retains_each_source_unit_when_budget_allows():
    artifact = _artifact()
    large = SourceUnit(unit_id="large", content="甲" * 100_000, locator_value="1")
    small = SourceUnit(unit_id="small", content="乙" * 20, locator_value="2")
    source_units = [(artifact, large), (artifact, small)]
    counts = [retrieval_module._chunk_count(unit.content, 100) for _, unit in source_units]
    selected = retrieval_module._candidate_indices(counts, 2)
    candidates = retrieval_module._candidates_at_indices(
        source_units,
        counts,
        selected,
        max_chars=100,
    )

    assert sum(counts) > 2
    assert {candidate.unit.unit_id for candidate in candidates} == {"large", "small"}


def test_streaming_lexical_retains_only_bounded_top_pool():
    artifact = _artifact()
    unit = SourceUnit(
        unit_id="large",
        content=("一般背景。" * 10_000) + "安全性掃描結果",
        locator_value="1",
    )

    pools, _, _ = retrieval_module._stream_lexical_candidates(
        ["安全性掃描結果"],
        [(artifact, unit)],
        max_chars=100,
        sampled_indices=set(),
        pool_size=7,
    )

    assert len(pools[0]) <= 7


def test_fullwidth_ocr_latin_text_matches_nfkc_query():
    artifact = DocumentArtifact(
        document_id="doc-fullwidth",
        filename_hash="h",
        media_type="text/plain",
        parser_config_hash="c",
        status=DocumentParseStatus.PARSED,
        source_units=[SourceUnit(
            unit_id="doc-fullwidth:1",
            content="已建立ＫＰＩ偏移監測與告警門檻。",
            locator_value="1",
        )],
    )

    hits = EvidenceRetriever().retrieve("KPI 偏移監測", [artifact], top_k=1)

    assert hits[0].unit_id == "doc-fullwidth:1"


def test_retrieval_can_keep_multiple_relevant_windows_from_one_source_unit():
    artifact = DocumentArtifact(
        document_id="doc-conflict",
        filename_hash="h",
        media_type="text/plain",
        parser_config_hash="c",
        status=DocumentParseStatus.PARSED,
        source_units=[SourceUnit(
            unit_id="doc-conflict:1",
            content=(
                "公平性測試結果支持群組差異符合門檻。"
                + "其他說明。" * 100
                + "公平性測試結果反駁前述結論，群組差異超過門檻。"
            ),
            locator_value="1",
        )],
    )

    hits = EvidenceRetriever(max_content_chars=120).retrieve(
        "公平性測試結果 群組差異 門檻", [artifact], top_k=2
    )

    assert len(hits) == 2
    assert {hit.unit_id for hit in hits} == {"doc-conflict:1"}
    assert any("支持" in hit.content for hit in hits)
    assert any("反駁" in hit.content for hit in hits)
    assert all("公平性測試結果" in hit.quote for hit in hits)
