"""法規乾淨文字行 → RegulationChunk 切塊（build-time；p3-01 spec §4.2）。

只吃 list[str]（pdf_text 產出），不碰 PDF（D2 解耦，golden 可離線）。
兩套規則：條文式（R01，style="article"）與章節式（R02–R07，style="chapter"）。

共通：
- order = 該法規內 chunk 的 0-based 文件順序（全域遞增）。
- chunk_seq 預設 0，僅「句號硬切」時於同一 section_path 內遞增。
- text = breadcrumb（`【{reg_code} {reg_title}|{層級路徑}】`）+ "\\n" + body。

隱私：log 只記代碼/計數，絕不記條文內容（CLAUDE.md 硬性）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from govcheck.logging_setup import get_logger
from govcheck.rag.models import RegulationChunk

log = get_logger("rag.chunker")

# body 內文長度上限（不含 breadcrumb）；超過即降層/硬切（spec §4.2 長度控制）
MAX_BODY_CHARS = 800
# 純標題節點併入父層的字數門檻
PURE_TITLE_MAX_CHARS = 30

# 條文式：條號標籤（標籤移入 breadcrumb，並自 body 剝除）
_ARTICLE_RE = re.compile(r"^(第[一二三四五六七八九十百]+條)[\s　]*(.*)$")

# 章節式三層 token
_L1_RE = re.compile(r"^([一二三四五六七八九十]+)、(.*)$")
_L2_RE = re.compile(r"^[（(]([一二三四五六七八九十]+)[）)](.*)$")
_L3_RE = re.compile(r"^(\d+)[.、][\s　]*(.*)$")


def chunk_regulation(
    lines: list[str],
    *,
    reg_code: str,
    reg_title: str,
    style: Literal["article", "chapter"],
) -> list[RegulationChunk]:
    """把乾淨文字行切為 RegulationChunk 清單（style 由呼叫端顯式傳入）。"""
    if style == "article":
        chunks = _chunk_article(lines, reg_code=reg_code, reg_title=reg_title)
    elif style == "chapter":
        chunks = _chunk_chapter(lines, reg_code=reg_code, reg_title=reg_title)
    else:  # pragma: no cover - Literal 已擋，防呆保險
        raise ValueError(f"unknown style: {style}")
    log.debug("chunked reg=%s style=%s chunks=%d", reg_code, style, len(chunks))
    return chunks


# ─────────────────────────────────────────────────────────────────────────────
# 共用小工具
# ─────────────────────────────────────────────────────────────────────────────


def _make_chunk(
    *,
    reg_code: str,
    reg_title: str,
    section_path: str,
    title: str,
    bc_path: str,
    body: str,
    chunk_seq: int,
    order: int,
) -> RegulationChunk:
    text = f"【{reg_code} {reg_title}|{bc_path}】\n{body}"
    return RegulationChunk(
        reg_code=reg_code,
        reg_title=reg_title,
        section_path=section_path,
        title=title,
        text=text,
        chunk_seq=chunk_seq,
        order=order,
    )


def _split_sentences(body: str) -> list[str]:
    """按句號切句（句號保留在句尾）；末段無句號則原樣保留（無損還原）。"""
    parts = body.split("。")
    # 前 n-1 段一律補回句號（含空片段 → "。"，代表連續句號，維持字元無損）；末段非空才保留
    sentences = [p + "。" for p in parts[:-1]]
    if parts[-1]:
        sentences.append(parts[-1])
    return sentences


def _pack_sentences(sentences: list[str], max_chars: int = MAX_BODY_CHARS) -> list[str]:
    """貪婪打包：緩衝非空且加入下一句後 > max_chars → 先 flush（spec §4.2 句號硬切）。"""
    packed: list[str] = []
    buf = ""
    for sentence in sentences:
        if buf and len(buf) + len(sentence) > max_chars:
            packed.append(buf)
            buf = sentence
        else:
            buf += sentence
    if buf:
        packed.append(buf)
    return packed


# ─────────────────────────────────────────────────────────────────────────────
# A. 條文式（R01）
# ─────────────────────────────────────────────────────────────────────────────


def _chunk_article(lines: list[str], *, reg_code: str, reg_title: str) -> list[RegulationChunk]:
    chunks: list[RegulationChunk] = []
    current_sec: str | None = None
    body_lines: list[str] = []
    preamble_dropped = 0

    def flush() -> None:
        if current_sec is None:
            return
        chunks.append(
            _make_chunk(
                reg_code=reg_code,
                reg_title=reg_title,
                section_path=current_sec,
                title=current_sec,
                bc_path=current_sec,
                body="\n".join(body_lines),
                chunk_seq=0,
                order=len(chunks),
            )
        )

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        m = _ARTICLE_RE.match(line)
        if m:
            flush()
            current_sec = m.group(1)
            body_lines = [m.group(2)] if m.group(2) else []
        elif current_sec is not None:
            body_lines.append(line)
        else:
            preamble_dropped += 1  # 第一條之前的雜行（如文件標題）不成 chunk
    flush()

    if preamble_dropped:
        log.debug("article preamble lines dropped n=%d reg=%s", preamble_dropped, reg_code)
    return chunks


# ─────────────────────────────────────────────────────────────────────────────
# B. 章節式（R02–R07）三層
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _L3Node:
    num: str                                   # 阿拉伯數字（正準）
    raw_lines: list[str]                       # 原始行（含 marker；L2 內聯模式用）
    content_lines: list[str]                   # marker 剝除後內文（降 L3 模式用）


@dataclass
class _L2Node:
    cn: str                                    # 中文數字（正準，括號另加）
    title: str
    raw_line: str                              # 原始標題行（純標題併入父層時用）
    intro_lines: list[str] = field(default_factory=list)
    l3s: list[_L3Node] = field(default_factory=list)


@dataclass
class _L1Node:
    cn: str
    title: str
    intro_lines: list[str] = field(default_factory=list)
    l2s: list[_L2Node] = field(default_factory=list)


def _parse_chapter_tree(lines: list[str]) -> tuple[list[_L1Node], int]:
    """逐行狀態機：L1/L2/L3 token 起新節點，其餘行落到最深開啟節點。"""
    l1s: list[_L1Node] = []
    cur_l1: _L1Node | None = None
    cur_l2: _L2Node | None = None
    cur_l3: _L3Node | None = None
    preamble_dropped = 0

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if m := _L1_RE.match(line):
            cur_l1 = _L1Node(cn=m.group(1), title=m.group(2).strip())
            cur_l2 = None
            cur_l3 = None
            l1s.append(cur_l1)
        elif cur_l1 is not None and (m := _L2_RE.match(line)):
            cur_l2 = _L2Node(cn=m.group(1), title=m.group(2).strip(), raw_line=line)
            cur_l3 = None
            cur_l1.l2s.append(cur_l2)
        elif cur_l2 is not None and (m := _L3_RE.match(line)):
            cur_l3 = _L3Node(num=m.group(1), raw_lines=[line], content_lines=[m.group(2)])
            cur_l2.l3s.append(cur_l3)
        elif cur_l3 is not None:
            cur_l3.raw_lines.append(line)
            cur_l3.content_lines.append(line)
        elif cur_l2 is not None:
            cur_l2.intro_lines.append(line)
        elif cur_l1 is not None:
            cur_l1.intro_lines.append(line)
        else:
            preamble_dropped += 1  # 第一個 L1 之前的雜行
    return l1s, preamble_dropped


def _is_pure_title(l2: _L2Node) -> bool:
    """< 30 字純標題（無內文、無 L3）→ 不自成 chunk，併入父層（spec §4.2）。"""
    return (
        len(l2.raw_line) < PURE_TITLE_MAX_CHARS
        and not l2.intro_lines
        and not l2.l3s
    )


def _chunk_chapter(lines: list[str], *, reg_code: str, reg_title: str) -> list[RegulationChunk]:
    l1s, preamble_dropped = _parse_chapter_tree(lines)
    chunks: list[RegulationChunk] = []
    skipped_pure_titles = 0

    def emit(section_path: str, title: str, bc_path: str, body: str, chunk_seq: int = 0) -> None:
        chunks.append(
            _make_chunk(
                reg_code=reg_code,
                reg_title=reg_title,
                section_path=section_path,
                title=title,
                bc_path=bc_path,
                body=body,
                chunk_seq=chunk_seq,
                order=len(chunks),
            )
        )

    def emit_with_split(section_path: str, title: str, bc_path: str, body: str) -> None:
        """body ≤ 800 → 單一 chunk；> 800 → 句號硬切，同 section_path、chunk_seq 遞增。"""
        if len(body) <= MAX_BODY_CHARS:
            emit(section_path, title, bc_path, body)
            return
        for seq, piece in enumerate(_pack_sentences(_split_sentences(body))):
            emit(section_path, title, bc_path, piece, chunk_seq=seq)

    for l1 in l1s:
        l1_bc = f"{l1.cn}、{l1.title}"
        l1_body_lines = list(l1.intro_lines)
        l2_specs: list[_L2Node] = []

        for l2 in l1.l2s:
            if _is_pure_title(l2):
                if l1.intro_lines:
                    # 純標題行 append 到父層（L1 引言）chunk 的 body 末尾
                    l1_body_lines.append(l2.raw_line)
                else:
                    # Lead 裁決：L1 無引言（無父 chunk 可併）→ skip + DEBUG 計數，
                    # 不自成 chunk（無義務內容，獨立成 chunk 徒增檢索噪音）。
                    skipped_pure_titles += 1
            else:
                l2_specs.append(l2)

        # L1 引言自成 chunk（無引言內文則不產生 L1 chunk）
        if l1.intro_lines:
            emit(l1.cn, l1.title, l1_bc, "\n".join(l1_body_lines))

        for l2 in l2_specs:
            l2_path = f"{l1.cn}/({l2.cn})"          # 全形括號 → 半形（正準化）
            l2_bc = f"{l1_bc}>({l2.cn}){l2.title}"
            inline_body_lines = list(l2.intro_lines)
            for l3 in l2.l3s:
                inline_body_lines.extend(l3.raw_lines)
            inline_body = "\n".join(inline_body_lines)

            if len(inline_body) <= MAX_BODY_CHARS or not l2.l3s:
                # 單一 L2 chunk（L3 內聯、marker 保留）；無 L3 的超長 L2 直接句號硬切
                emit_with_split(l2_path, l2.title, l2_bc, inline_body)
            else:
                # L2 body > 800 → 降為逐 L3 切（marker 移入 breadcrumb 並自 body 剝除）
                # 若 L2 在第一個 L3 前有 intro_lines，先為該 intro 單獨 emit 一個 L2 層級 chunk，
                # 以維持 losslessness（spec §4.2）。
                if l2.intro_lines:
                    emit_with_split(l2_path, l2.title, l2_bc, "\n".join(l2.intro_lines))
                for l3 in l2.l3s:
                    l3_path = f"{l2_path}/{l3.num}"
                    l3_bc = f"{l2_bc}>{l3.num}."
                    emit_with_split(l3_path, l2.title, l3_bc, "\n".join(l3.content_lines))

    if preamble_dropped:
        log.debug("chapter preamble lines dropped n=%d reg=%s", preamble_dropped, reg_code)
    if skipped_pure_titles:
        log.debug("pure-title nodes skipped (no parent intro) n=%d reg=%s", skipped_pure_titles, reg_code)
    return chunks
