"""In-memory hybrid evidence retrieval with deterministic source citations."""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from govcheck.models import DocumentArtifact, DocumentParseStatus, SourceUnit
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
    quote: str = ""


def excerpt_id(hit: EvidenceHit) -> str:
    """Return a compact stable identity for one exact source window.

    模型只能引用 allow-list 內的 excerpt_id；同一 source unit 的不同原文視窗因此可以區分，
    F03 判讀與協填共用同一套識別，報告裡的 citation 才能互相對照。
    """
    identity = json.dumps(
        [hit.document_id, hit.unit_id, hit.content], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(identity.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class _EvidenceChunk:
    """A contiguous source window used for both semantic ranking and model evidence."""

    content: str
    start: int


@dataclass(frozen=True)
class _Candidate:
    index: int
    artifact: DocumentArtifact
    unit: SourceUnit
    chunk: _EvidenceChunk


class EvidenceRetriever:
    def __init__(
        self,
        *,
        embedding_client=None,
        lexical_weight: float = 0.7,
        max_content_chars: int = 600,
        max_embedding_chunks: int = 512,
    ) -> None:
        self.embedding_client = embedding_client
        self.lexical_weight = min(1.0, max(0.0, lexical_weight))
        self.max_content_chars = max(100, max_content_chars)
        self.max_embedding_chunks = max(1, max_embedding_chunks)
        self.embedding_calls = 0
        # Query indexes whose configured semantic retrieval could not examine the full corpus.
        # Callers use this to distinguish a trustworthy empty result from a bounded/failing
        # semantic search; lexical retrieval still scans every chunk in either case.
        self.last_incomplete_query_indices: frozenset[int] = frozenset()

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
        """Rank all item queries; the embedding client may split bounded endpoint batches."""
        self.last_incomplete_query_indices = frozenset()
        if not queries:
            return []
        source_units = [
            (artifact, unit)
            for artifact in artifacts
            if artifact.status is DocumentParseStatus.PARSED
            for unit in artifact.source_units
            if unit.content.strip()
        ]
        if not source_units:
            return [[] for _ in queries]

        counts = [_chunk_count(unit.content, self.max_content_chars) for _, unit in source_units]
        total_chunks = sum(counts)
        semantic_by_query: list[dict[int, float]] = [{} for _ in queries]
        semantic_available = False
        active_queries = [index for index, query in enumerate(queries) if query.strip()]
        sampled_indices = (
            _candidate_indices(counts, min(total_chunks, self.max_embedding_chunks))
            if self.embedding_client is not None and active_queries
            else []
        )
        sampled_candidates = _candidates_at_indices(
            source_units,
            counts,
            sampled_indices,
            max_chars=self.max_content_chars,
        )
        semantic_corpus_truncated = len(sampled_candidates) < total_chunks
        if self.embedding_client is not None and active_queries:
            if semantic_corpus_truncated:
                log.info(
                    "evidence embedding chunk limit total=%d selected=%d",
                    total_chunks,
                    len(sampled_candidates),
                )
            try:
                vectors = self.embedding_client.embed([
                    *[queries[index] for index in active_queries],
                    *[candidate.chunk.content for candidate in sampled_candidates],
                ])
                expected = len(active_queries) + len(sampled_candidates)
                if len(vectors) != expected:
                    raise ValueError("embedding vector count mismatch")
                query_vectors = vectors[:len(active_queries)]
                chunk_vectors = vectors[len(active_queries):]
                for query_index, query_vector in zip(active_queries, query_vectors):
                    semantic_by_query[query_index] = {
                        candidate.index: _cosine(query_vector, vector)
                        for candidate, vector in zip(sampled_candidates, chunk_vectors)
                    }
                semantic_available = True
            except Exception as exc:  # noqa: BLE001 - semantic retrieval degrades to lexical
                log.warning("evidence embedding unavailable err=%s", type(exc).__name__)
            finally:
                self.embedding_calls += max(
                    1, int(getattr(self.embedding_client, "last_request_count", 1))
                )
            if semantic_corpus_truncated or not semantic_available:
                self.last_incomplete_query_indices = frozenset(active_queries)

        lexical_pools, lexical_peaks, sampled_lexical = _stream_lexical_candidates(
            queries,
            source_units,
            max_chars=self.max_content_chars,
            sampled_indices=set(sampled_indices),
            pool_size=max(32, max(1, top_k) * 8),
        )
        lexical_weight = self.lexical_weight if semantic_available else 1.0
        sampled_by_index = {candidate.index: candidate for candidate in sampled_candidates}
        results = []
        for query_index, query in enumerate(queries):
            if not query.strip():
                results.append([])
                continue
            candidate_scores = dict(lexical_pools[query_index])
            candidate_scores.update({
                index: (sampled_by_index[index], raw_score)
                for index, raw_score in sampled_lexical[query_index].items()
            })
            ranked = []
            for index, (candidate, lexical_raw) in candidate_scores.items():
                semantic = semantic_by_query[query_index].get(index, 0.0)
                if (
                    lexical_raw < _MIN_LEXICAL_SCORE
                    and semantic < _MIN_SEMANTIC_SCORE
                ):
                    # 絕對門檻：詞面與語意都不夠像就不算佐證。少了這道，正規化後的
                    # 最高分恆為 1.0，任一段落只要有一個偶然重疊的 CJK bigram
                    # 就會被送進 allowed_citations，讓模型「有東西可引」。
                    continue
                score = (
                    lexical_weight
                    * (lexical_raw / lexical_peaks[query_index] if lexical_peaks[query_index] else 0)
                    + (1 - lexical_weight) * semantic
                )
                ranked.append((
                    score,
                    index,
                    candidate.artifact,
                    candidate.unit,
                    candidate.chunk,
                ))
            ranked.sort(
                key=lambda row: (
                    -row[0], row[2].document_id, row[3].unit_id, row[4].start
                )
            )
            selected = []
            for row in ranked:
                if row[0] <= 0:
                    continue
                if any(_same_overlapping_window(row, prior) for prior in selected):
                    continue
                selected.append(row)
                if len(selected) >= max(1, top_k):
                    break
            hits = []
            for score, _, artifact, unit, chunk in selected:
                hits.append(EvidenceHit(
                    document_id=artifact.document_id,
                    unit_id=unit.unit_id,
                    locator_type=unit.locator_type,
                    locator_value=unit.locator_value,
                    # The exact same contiguous chunk is ranked and sent to L3. This avoids a
                    # second sampling pass selecting unrelated text from the source unit.
                    content=chunk.content,
                    score=score,
                    quote=" ".join(
                        _relevant_excerpt(chunk.content, query, max_chars=180).split()
                    ),
                ))
            results.append(hits)
        return results


def _relevant_excerpt(content: str, query: str, *, max_chars: int) -> str:
    """Return a bounded, verbatim window near a query match, with omission markers."""
    if len(content) <= max_chars:
        return content

    compact, compact_offsets = _normalized_with_offsets(content, cjk_only=False)
    cjk_content, cjk_offsets = _normalized_with_offsets(content, cjk_only=True)
    normalized_query = unicodedata.normalize("NFKC", query).casefold()
    latin_terms = re.findall(r"[a-z0-9_]{2,}", normalized_query)
    cjk = [char for char in normalized_query if "\u3400" <= char <= "\u9fff"]
    cjk_terms = ["".join(cjk[index:index + 2]) for index in range(len(cjk) - 1)]
    terms = list(dict.fromkeys([*latin_terms, *cjk_terms]))[:256]
    marker = "…"
    content_budget = max(1, max_chars - 2 * len(marker))
    matches: list[tuple[int, int]] = []
    for term in terms:
        searchable, offsets = (
            (cjk_content, cjk_offsets)
            if any("\u3400" <= char <= "\u9fff" for char in term)
            else (compact, compact_offsets)
        )
        offset = 0
        while True:
            position = searchable.find(term, offset)
            if position < 0:
                break
            original_start = offsets[position]
            original_end = offsets[min(position + len(term) - 1, len(offsets) - 1)] + 1
            matches.append((original_start, max(1, original_end - original_start)))
            offset = position + max(1, len(term))

    if not matches:
        # Semantic-only matches have no lexical anchor. Sample the beginning, middle and end rather
        # than silently assuming the first paragraph contains the relevant evidence.
        part = max(1, (max_chars - 2 * len(marker)) // 3)
        middle = max(0, len(content) // 2 - part // 2)
        return marker.join((content[:part], content[middle:middle + part], content[-part:]))[
            :max_chars
        ]

    candidate_starts = sorted({
        max(0, min(len(content) - content_budget, position - content_budget // 3))
        for position, _ in matches
    })
    if len(candidate_starts) > 64:
        last = len(candidate_starts) - 1
        candidate_starts = [
            candidate_starts[round(index * last / 63)] for index in range(64)
        ]

    def score(start: int) -> tuple[int, int]:
        end = start + content_budget
        density = sum(weight for position, weight in matches if start <= position < end)
        return density, -start

    start = max(candidate_starts, key=score)
    end = min(len(content), start + content_budget)
    start = max(0, end - content_budget)
    excerpt = content[start:end]
    if start:
        excerpt = marker + excerpt
    if end < len(content):
        excerpt += marker
    return excerpt


def _contiguous_chunks(content: str, max_chars: int) -> list[_EvidenceChunk]:
    """Split a unit into overlapping, bounded windows without rewriting source text."""
    return [
        _chunk_at(content, max_chars, index)
        for index in range(_chunk_count(content, max_chars))
    ]


def _candidates_at_indices(
    source_units: list[tuple[DocumentArtifact, SourceUnit]],
    counts: list[int],
    selected: list[int],
    *,
    max_chars: int,
) -> list[_Candidate]:
    """Materialize only selected global chunk indices for bounded semantic retrieval."""
    candidates: list[_Candidate] = []
    source_index = 0
    source_start = 0
    for global_index in selected:
        while global_index >= source_start + counts[source_index]:
            source_start += counts[source_index]
            source_index += 1
        artifact, unit = source_units[source_index]
        local_index = global_index - source_start
        candidates.append(_Candidate(
            index=global_index,
            artifact=artifact,
            unit=unit,
            chunk=_chunk_at(unit.content, max_chars, local_index),
        ))
    return candidates


def _iter_candidates(
    source_units: list[tuple[DocumentArtifact, SourceUnit]],
    max_chars: int,
):
    """Yield every source window without retaining the corpus-wide chunk list."""
    global_index = 0
    for artifact, unit in source_units:
        for local_index in range(_chunk_count(unit.content, max_chars)):
            yield _Candidate(
                index=global_index,
                artifact=artifact,
                unit=unit,
                chunk=_chunk_at(unit.content, max_chars, local_index),
            )
            global_index += 1


def _stream_lexical_candidates(
    queries: list[str],
    source_units: list[tuple[DocumentArtifact, SourceUnit]],
    *,
    max_chars: int,
    sampled_indices: set[int],
    pool_size: int,
) -> tuple[
    list[dict[int, tuple[_Candidate, float]]],
    list[float],
    list[dict[int, float]],
]:
    """Run exact BM25 statistics in two streaming passes and retain only bounded top pools."""
    query_terms = [set(_terms(query)) if query.strip() else set() for query in queries]
    all_query_terms = set().union(*query_terms) if query_terms else set()
    document_frequency: Counter[str] = Counter()
    total_length = 0
    total_documents = 0
    for candidate in _iter_candidates(source_units, max_chars):
        frequency, length = _term_counter(candidate.chunk.content)
        total_length += length
        total_documents += 1
        document_frequency.update(frequency.keys() & all_query_terms)

    average = total_length / max(total_documents, 1)
    inverse = {
        term: math.log(
            1
            + (total_documents - document_frequency[term] + 0.5)
            / (document_frequency[term] + 0.5)
        )
        for term in all_query_terms
    }
    query_indices_by_term: dict[str, list[int]] = {}
    for query_index, terms in enumerate(query_terms):
        for term in terms:
            query_indices_by_term.setdefault(term, []).append(query_index)

    heaps: list[list[tuple[float, int, _Candidate]]] = [[] for _ in queries]
    peaks = [0.0] * len(queries)
    sampled_scores: list[dict[int, float]] = [{} for _ in queries]
    bounded_pool_size = max(1, pool_size)
    for candidate in _iter_candidates(source_units, max_chars):
        frequency, length = _term_counter(candidate.chunk.content)
        scores = [0.0] * len(queries)
        for term in frequency.keys() & all_query_terms:
            count = frequency[term]
            denominator = count + 1.5 * (
                1 - 0.75 + 0.75 * length / max(average, 1)
            )
            contribution = inverse[term] * count * 2.5 / denominator
            for query_index in query_indices_by_term[term]:
                scores[query_index] += contribution
        for query_index, score in enumerate(scores):
            peaks[query_index] = max(peaks[query_index], score)
            if candidate.index in sampled_indices:
                sampled_scores[query_index][candidate.index] = score
            if score <= 0:
                continue
            row = (score, -candidate.index, candidate)
            heap = heaps[query_index]
            if len(heap) < bounded_pool_size:
                heapq.heappush(heap, row)
            elif row[:2] > heap[0][:2]:
                heapq.heapreplace(heap, row)

    pools = [
        {candidate.index: (candidate, score) for score, _, candidate in heap}
        for heap in heaps
    ]
    return pools, peaks, sampled_scores


def _term_counter(text: str) -> tuple[Counter[str], int]:
    """Build one chunk's BM25 frequencies without retaining duplicate token lists."""
    normalized = "".join(unicodedata.normalize("NFKC", text).casefold().split())
    latin = re.findall(r"[a-z0-9_]+", normalized)
    cjk = [char for char in normalized if "\u3400" <= char <= "\u9fff"]
    frequency: Counter[str] = Counter(latin)
    frequency.update(cjk)
    frequency.update("".join(cjk[index:index + 2]) for index in range(len(cjk) - 1))
    return frequency, len(latin) + len(cjk) + max(0, len(cjk) - 1)


def _candidate_indices(counts: list[int], limit: int) -> list[int]:
    """Sample the whole corpus while retaining one window per source unit when possible."""
    total = sum(counts)
    if total <= limit:
        return list(range(total))
    selected: set[int] = set()
    if len(counts) <= limit:
        source_start = 0
        for count in counts:
            selected.add(source_start + count // 2)
            source_start += count
    if len(selected) < limit:
        for index in _uniform_indices(total, limit):
            selected.add(index)
            if len(selected) >= limit:
                break
    return sorted(selected)


def _chunk_count(content: str, max_chars: int) -> int:
    if len(content) <= max_chars:
        return 1
    step = _chunk_step(max_chars)
    return 1 + (len(content) - max_chars + step - 1) // step


def _chunk_at(content: str, max_chars: int, index: int) -> _EvidenceChunk:
    start = index * _chunk_step(max_chars)
    return _EvidenceChunk(content=content[start:start + max_chars], start=start)


def _chunk_step(max_chars: int) -> int:
    overlap = min(max(32, max_chars // 6), max_chars - 1)
    return max(1, max_chars - overlap)


def _uniform_indices(total: int, limit: int) -> list[int]:
    """Select a deterministic corpus-wide sample while bounding embedding endpoint work."""
    if total <= 0 or limit <= 0:
        return []
    if total <= limit:
        return list(range(total))
    if limit == 1:
        return [total // 2]
    return [round(index * (total - 1) / (limit - 1)) for index in range(limit)]


def _same_overlapping_window(left: tuple, right: tuple) -> bool:
    """Drop redundant overlapping chunks while retaining distinct windows in one source unit."""
    if left[2].document_id != right[2].document_id or left[3].unit_id != right[3].unit_id:
        return False
    left_chunk = left[4]
    right_chunk = right[4]
    return (
        left_chunk.start < right_chunk.start + len(right_chunk.content)
        and right_chunk.start < left_chunk.start + len(left_chunk.content)
    )


def _normalized_with_offsets(text: str, *, cjk_only: bool) -> tuple[str, list[int]]:
    """Normalize searchable text while retaining offsets into the original OCR content."""
    normalized = []
    offsets = []
    for original_index, char in enumerate(text):
        for folded in unicodedata.normalize("NFKC", char).casefold():
            if cjk_only:
                if not "\u3400" <= folded <= "\u9fff":
                    continue
            elif folded.isspace():
                continue
            normalized.append(folded)
            offsets.append(original_index)
    return "".join(normalized), offsets


def _terms(text: str) -> list[str]:
    normalized = "".join(unicodedata.normalize("NFKC", text).casefold().split())
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
