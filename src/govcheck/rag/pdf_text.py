"""法規 PDF → 乾淨文字行（build-time；p3-01 spec §4.1）。

pdfplumber 逐頁 extract_text → 清洗三規則（順序固定）→ 攤平為單一 list[str]：
  R1 刪頁碼行：^-?\\s*\\d+\\s*-?$ / ^第\\s*\\d+\\s*頁$ / ^\\d+\\s*/\\s*\\d+$
  R2 刪重複頁首尾：同一 strip 後文字出現於 >=60% 頁面（且總頁數>=3）→ 全數刪除
  R3 CJK 接行（同頁內、不跨頁）：上一行末字 ∉ {。：；！？} 且下一行非空、非標題 token → 直接串接（無空白）

隱私：log 只記頁數/行數等計數，絕不記內文（CLAUDE.md 硬性）。
"""

from __future__ import annotations

import re
from pathlib import Path

from govcheck.logging_setup import get_logger

log = get_logger("rag.pdf_text")

# R1：頁碼行（strip 後整行比對）
_PAGE_NUM_RE = re.compile(
    r"^-?\s*\d+\s*-?$"          # "1"、"- 3 -"、"-1-"
    r"|^第\s*\d+\s*頁$"          # "第 3 頁"
    r"|^\d+\s*/\s*\d+$"          # "3 / 10"
)

# R3：標題 token（下一行為標題則不併行）
_TITLE_TOKEN_RE = re.compile(
    r"^第[一二三四五六七八九十百]+條"
    r"|^[一二三四五六七八九十]+、"
    r"|^[（(][一二三四五六七八九十]+[）)]"
    r"|^\d+[.、]"
)

# R3：上一行以這些字元結尾則不併行
_NO_MERGE_TAIL = {"。", "：", "；", "！", "？"}

# R2：頁首尾樣板判定門檻
_HEADER_MIN_PAGES = 3
_HEADER_RATIO = 0.60


def _should_merge(prev: str, cur: str) -> bool:
    """R3 接行判斷：prev 非空、末字不在句末標點集、cur 非空且非標題 token。"""
    if not prev or not cur:
        return False
    if prev[-1] in _NO_MERGE_TAIL:
        return False
    if _TITLE_TOKEN_RE.match(cur):
        return False
    return True


def clean_pages(pages: list[list[str]]) -> list[str]:
    """吃「每頁原始行」→ 乾淨行（純函式；golden 測此）。

    順序固定：R1+R2（逐頁移除雜訊行）→ R3（各頁殘餘行內接行，不跨頁）→ 攤平。
    """
    total_pages = len(pages)

    # R1：刪頁碼行（先做，殘餘行才進 R2 統計）
    r1_pages: list[list[str]] = [
        [ln for ln in page if not _PAGE_NUM_RE.match(ln.strip())] for page in pages
    ]

    # R2：統計每個 strip 後文字「出現在幾頁」（每頁最多計一次）
    boilerplate: set[str] = set()
    if total_pages >= _HEADER_MIN_PAGES:
        page_count: dict[str, int] = {}
        for page in r1_pages:
            for stripped in {ln.strip() for ln in page if ln.strip()}:
                page_count[stripped] = page_count.get(stripped, 0) + 1
        boilerplate = {
            text
            for text, n in page_count.items()
            if n / total_pages >= _HEADER_RATIO
        }
    r2_pages: list[list[str]] = [
        [ln for ln in page if ln.strip() not in boilerplate] for page in r1_pages
    ]

    # R3：各頁殘餘行內 CJK 接行（不跨頁）
    result: list[str] = []
    for page in r2_pages:
        merged: list[str] = []
        for raw in page:
            cur = raw.strip()
            if not cur:
                continue
            if merged and _should_merge(merged[-1], cur):
                merged[-1] += cur  # CJK 無空白直接串接
            else:
                merged.append(cur)
        result.extend(merged)

    log.debug("clean_pages pages=%d lines_out=%d", total_pages, len(result))
    return result


def load_clean_lines(pdf_path: str | Path) -> list[str]:
    """真 PDF 入口：pdfplumber 逐頁 extract_text → clean_pages。"""
    import pdfplumber  # 延遲 import：僅 build-time 需要

    path = Path(pdf_path)
    pages: list[list[str]] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            pages.append(text.splitlines())
    log.debug("load_clean_lines file=%s pages=%d", path.name, len(pages))
    return clean_pages(pages)
