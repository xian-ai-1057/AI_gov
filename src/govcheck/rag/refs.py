"""L 欄「規範參考」解析與 section_path prefix 比對（p3-02 spec §4.1）。

tolerant 文法（L 欄為人工填寫，格式浮動）：
  entry := reg_codes ("/" path_token)*
  reg_codes := R\\d{2} (("&" | "、") R\\d{2})*   — 同一 entry 的多個 reg code 共用其後 path
  多 entry 以換行或 ";" 分隔。
正規化：全形→半形（NFKC）、去空白、（二）→(二)。
殘段：認得 reg code 但 path 無法解析 → 退整部規範（prefix=""）；
      完全無 reg code → 丟棄該 entry 並 DEBUG 記計數（絕不 log 原文——隱私紅線）。

prefix 雙向比對（ref_matches_chunk）以 path 分段（"/"）比對，
「五/(二)」不得誤命中「五/(二十)」。
"""

from __future__ import annotations

import re
import unicodedata

from govcheck.logging_setup import get_logger
from govcheck.rag.models import RegulationRef

log = get_logger("rag.refs")

# 展開比對（一 ref 命中多 chunk）之每 ref 上限（p3-02 spec §4.1 per-ref cap）
PER_REF_CAP = 3

_REG_CODE_RE = re.compile(r"R\d{2}")
# entry 開頭的 reg_codes 串：R\d{2}(("&"|"、")R\d{2})*
_REG_CODES_HEAD_RE = re.compile(r"^(R\d{2}(?:[&、]R\d{2})*)")
_ENTRY_SPLIT_RE = re.compile(r"[\n;]")


def normalize_section_path(raw: str) -> str:
    """正規化：全形→半形（NFKC：Ｒ→R、０→0、／→/、（）→()）、各段去空白。"""
    s = unicodedata.normalize("NFKC", raw)
    parts = [p.strip() for p in s.split("/")]
    return "/".join(p for p in parts if p)


def parse_regulation_refs(raw: str | None) -> list[RegulationRef]:
    """L 欄原始字串 → RegulationRef 清單（保序）。

    無法解析的殘段（無 reg code）丟棄並 DEBUG 記計數；絕不 log 原文。
    """
    if raw is None or not raw.strip():
        return []

    refs: list[RegulationRef] = []
    dropped = 0
    for raw_entry in _ENTRY_SPLIT_RE.split(raw):
        entry = normalize_section_path(raw_entry)  # 全形→半形 + 各段去空白
        if not entry:
            continue

        head = _REG_CODES_HEAD_RE.match(entry)
        if head is None:
            dropped += 1  # 完全無 reg code → 丟棄該 entry（只計數，不記原文）
            continue

        reg_codes = _REG_CODE_RE.findall(head.group(1))
        tail = entry[head.end():]
        if tail.startswith("/"):
            prefix = normalize_section_path(tail[1:])
        else:
            # 認得 reg code 但其後 token 非合法 path（或無 path）→ 退整部規範
            prefix = ""

        refs.extend(
            RegulationRef(reg_code=code, section_path_prefix=prefix) for code in reg_codes
        )

    if dropped:
        log.debug("refs dropped n=%d", dropped)
    return refs


def ref_matches_chunk(ref: RegulationRef, chunk_reg_code: str, chunk_section_path: str) -> bool:
    """prefix 雙向比對（reg_code 不同一律 False；分段比對防「(二)」誤命中「(二十)」）。

    1. 相等；2. chunk 在 ref 之下（ref 較粗，展開比對、受 cap）；
    3. ref 在 chunk 之下（ref 較細）；4. prefix==""（整部規範，展開比對、受 cap）。
    """
    if ref.reg_code != chunk_reg_code:
        return False
    prefix = ref.section_path_prefix
    if prefix == "":
        return True
    if chunk_section_path == prefix:
        return True
    if chunk_section_path.startswith(prefix + "/"):
        return True
    if prefix.startswith(chunk_section_path + "/"):
        return True
    return False


def filter_chunks_by_ref(
    ref: RegulationRef, chunks: list[dict], cap: int = PER_REF_CAP
) -> list[dict]:
    """對 row dict（含 reg_code / section_path / order）套 ref 比對 + per-ref cap。

    依 order 升冪取前 cap 筆（展開命中多 chunk 時避免灌爆一項）。
    供 build ②（T2 的 build_retrieval_map）重用，確保與測試同一實作。
    """
    matched = [
        c for c in chunks if ref_matches_chunk(ref, c["reg_code"], c["section_path"])
    ]
    matched.sort(key=lambda c: c["order"])
    return matched[:cap]
