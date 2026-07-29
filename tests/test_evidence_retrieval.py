from __future__ import annotations

from govcheck.evidence import EvidenceRetriever
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


def test_retrieve_many_batches_embeddings_once():
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
