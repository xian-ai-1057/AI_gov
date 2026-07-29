"""In-memory hybrid evidence retrieval with deterministic source citations."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from govcheck.models import DocumentArtifact, DocumentParseStatus
from govcheck.logging_setup import get_logger

log = get_logger("evidence_retrieval")

# 相關性絕對門檻（作用在未正規化的分數上）。寧可回「找不到佐證」，
# 也不要餵一段不相關的文字讓模型誤判為 supported。
_MIN_LEXICAL_SCORE = 1.0
_MIN_SEMANTIC_SCORE = 0.55


@dataclass(frozen=True)
class EvidenceHit:
    document_id: str
    unit_id: str
    locator_type: str
    locator_value: str
    content: str
    score: float


class EvidenceRetriever:
    def __init__(self, *, embedding_client=None, lexical_weight: float = 0.7) -> None:
        self.embedding_client = embedding_client
        self.lexical_weight = min(1.0, max(0.0, lexical_weight))
        self.embedding_calls = 0

    def retrieve(
        self,
        query: str,
        artifacts: list[DocumentArtifact],
        *,
        top_k: int = 4,
    ) -> list[EvidenceHit]:
        return self.retrieve_many([query], artifacts, top_k=top_k)[0]

    def retrieve_many(
        self,
        queries: list[str],
        artifacts: list[DocumentArtifact],
        *,
        top_k: int = 4,
    ) -> list[list[EvidenceHit]]:
        """Rank all item queries while making at most one embedding request per review run."""
        candidates = [
            (artifact, unit)
            for artifact in artifacts
            if artifact.status is DocumentParseStatus.PARSED
            for unit in artifact.source_units
            if unit.content.strip()
        ]
        if not candidates:
            return [[] for _ in queries]

        docs = [_terms(unit.content) for _, unit in candidates]
        semantic_by_query = [[0.0] * len(candidates) for _ in queries]
        if self.embedding_client is not None:
            try:
                self.embedding_calls += 1
                vectors = self.embedding_client.embed([
                    *queries, *[unit.content for _, unit in candidates]
                ])
                query_vectors = vectors[:len(queries)]
                document_vectors = vectors[len(queries):]
                semantic_by_query = [
                    [_cosine(query_vector, vector) for vector in document_vectors]
                    for query_vector in query_vectors
                ]
            except Exception as exc:  # noqa: BLE001 - semantic retrieval degrades to lexical
                log.warning("evidence embedding unavailable err=%s", type(exc).__name__)

        results = []
        for query, semantic in zip(queries, semantic_by_query):
            if not query.strip():
                results.append([])
                continue
            lexical, lexical_raw = _bm25(_terms(query), docs)
            ranked = []
            for index, (artifact, unit) in enumerate(candidates):
                if (
                    lexical_raw[index] < _MIN_LEXICAL_SCORE
                    and semantic[index] < _MIN_SEMANTIC_SCORE
                ):
                    # 絕對門檻：詞面與語意都不夠像就不算佐證。少了這道，正規化後的
                    # 最高分恆為 1.0，任一段落只要有一個偶然重疊的 CJK bigram
                    # 就會被送進 allowed_citations，讓模型「有東西可引」。
                    continue
                score = (
                    self.lexical_weight * lexical[index]
                    + (1 - self.lexical_weight) * semantic[index]
                )
                ranked.append(EvidenceHit(
                    document_id=artifact.document_id,
                    unit_id=unit.unit_id,
                    locator_type=unit.locator_type,
                    locator_value=unit.locator_value,
                    content=unit.content[:1200],
                    score=score,
                ))
            ranked.sort(key=lambda hit: (-hit.score, hit.document_id, hit.unit_id))
            results.append([hit for hit in ranked[:max(1, top_k)] if hit.score > 0])
        return results


def _terms(text: str) -> list[str]:
    normalized = "".join(text.casefold().split())
    latin = re.findall(r"[a-z0-9_]+", normalized)
    cjk = [char for char in normalized if "\u3400" <= char <= "\u9fff"]
    bigrams = ["".join(cjk[i:i + 2]) for i in range(max(0, len(cjk) - 1))]
    return latin + cjk + bigrams


def _bm25(query: list[str], docs: list[list[str]]) -> tuple[list[float], list[float]]:
    """回傳 (正規化分數, 原始 BM25 分數)。

    正規化分數用於與 cosine 融合（兩者才在同一量級），但**不可**用來判斷相關性：
    正規化後最高分恆為 1.0，任何一個偶然重疊的詞都會讓某段落看起來滿分。
    相關性門檻一律用原始分數判斷（見 _MIN_LEXICAL_SCORE）。
    """
    if not query:
        return [0.0] * len(docs), [0.0] * len(docs)
    average = sum(len(doc) for doc in docs) / max(len(docs), 1)
    frequencies = [Counter(doc) for doc in docs]
    scores = [0.0] * len(docs)
    for term in set(query):
        document_frequency = sum(1 for frequency in frequencies if frequency[term])
        inverse = math.log(1 + (len(docs) - document_frequency + 0.5) / (document_frequency + 0.5))
        for index, frequency in enumerate(frequencies):
            count = frequency[term]
            denominator = count + 1.5 * (1 - 0.75 + 0.75 * len(docs[index]) / max(average, 1))
            if count:
                scores[index] += inverse * count * 2.5 / denominator
    peak = max(scores, default=0.0)
    normalized = [score / peak if peak else 0.0 for score in scores]
    return normalized, scores


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right))
    l_norm = math.sqrt(sum(value * value for value in left))
    r_norm = math.sqrt(sum(value * value for value in right))
    return numerator / (l_norm * r_norm) if l_norm and r_norm else 0.0
