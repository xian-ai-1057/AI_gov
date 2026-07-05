"""rag/chunker + rag/models 測試（p3-01 spec §4.2–4.3；golden 直接消費 contracts/fixtures/）。"""

import json
import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from govcheck.rag.chunker import MAX_BODY_CHARS, chunk_regulation
from govcheck.rag.models import IndexMeta, RegulationChunk

ROOT = Path(__file__).resolve().parents[1]
P301_FIXTURES = ROOT / "specs" / "p3-01-regulation-index" / "contracts" / "fixtures"
DATA_ORIGINAL = ROOT / "data" / "original"


def _load_lines(name: str) -> list[str]:
    return (P301_FIXTURES / name).read_text(encoding="utf-8").splitlines()


def _load_yaml(name: str) -> dict:
    with (P301_FIXTURES / name).open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _body_of(chunk: RegulationChunk) -> str:
    """剝掉 breadcrumb 首行取 body 內文。"""
    return chunk.text.split("】\n", 1)[1]


@pytest.fixture(scope="module")
def r01_chunks() -> list[RegulationChunk]:
    return chunk_regulation(
        _load_lines("synthetic_r01_lines.txt"),
        reg_code="R01",
        reg_title="AI系統範例導入辦法（測試用）",
        style="article",
    )


@pytest.fixture(scope="module")
def r03_chunks() -> list[RegulationChunk]:
    return chunk_regulation(
        _load_lines("synthetic_r03_lines.txt"),
        reg_code="R03",
        reg_title="AI系統範例治理辦法（測試用）",
        style="chapter",
    )


def test_chunk_article_golden_r01(r01_chunks):
    """AC-2：條文式 golden——逐欄等於 expected_chunks_r01.yaml
    （4 chunk；section_path 第一條..第四條；第三條含 \\n 續行；breadcrumb 正確、條號自 body 剝除）。"""
    expected = _load_yaml("expected_chunks_r01.yaml")["chunks"]
    assert [c.model_dump() for c in r01_chunks] == expected
    assert [c.section_path for c in r01_chunks] == ["第一條", "第二條", "第三條", "第四條"]
    assert "\n本條所稱相關表單" in r01_chunks[2].text  # 第三條續行以 \n 保留


def test_chunk_chapter_golden_r03(r03_chunks):
    """AC-3：章節式 golden——逐欄等於 expected_chunks_r03.yaml（5 chunk）。
    覆蓋：L1 引言自成 chunk（一）、<30 字純標題「（二）保留」併入父層 一、
    L2 內聯 L3（一/(一)，marker 保留）、全形括號正規化（（一）→ (一)）。"""
    expected = _load_yaml("expected_chunks_r03.yaml")["chunks"]
    assert [c.model_dump() for c in r03_chunks] == expected

    # L1 引言自成 chunk，且 <30 字純標題併入其 body 末尾
    l1 = r03_chunks[0]
    assert l1.section_path == "一"
    assert l1.text.endswith("（二）保留")

    # 全形括號正規化：section_path 與 breadcrumb 皆為半形 (一)
    l2 = r03_chunks[1]
    assert l2.section_path == "一/(一)"
    assert ">(一)名詞定義】" in l2.text
    # L2 內聯：L3 marker 保留於 body
    assert "\n1. 人工智慧系統" in l2.text


def test_chunk_l3_downgrade(r03_chunks):
    """AC-4：L2 body 884 字 > 800 → 降為 3 個 L3 chunk（二/(一)/1..3），
    title 皆為父標題「評鑑程序」、各 body < 800、marker 移入 breadcrumb 並自 body 剝除、order 連續。"""
    downgraded = [c for c in r03_chunks if c.section_path.startswith("二/(一)/")]
    assert [c.section_path for c in downgraded] == ["二/(一)/1", "二/(一)/2", "二/(一)/3"]
    orders = [c.order for c in downgraded]
    assert orders == list(range(orders[0], orders[0] + 3))  # order 連續
    for i, c in enumerate(downgraded, start=1):
        assert c.title == "評鑑程序"
        body = _body_of(c)
        assert len(body) < MAX_BODY_CHARS
        assert f">{i}.】" in c.text                     # L3 marker 移入 breadcrumb
        assert not re.match(r"^\d+[.、]", body)          # 並自 body 剝除


def test_chunk_id_and_breadcrumb_contract(r01_chunks, r03_chunks):
    """AC-5：對 AC-2/AC-3 產物斷言 chunk_id == {reg_code}:{section_path}:{chunk_seq}，
    且 breadcrumb 前綴格式符合 spec §4.2（【code title|…】\\n 開頭）。"""
    for c in [*r01_chunks, *r03_chunks]:
        assert c.chunk_id == f"{c.reg_code}:{c.section_path}:{c.chunk_seq}"
        assert re.match(
            rf"^【{c.reg_code} {re.escape(c.reg_title)}\|[^】]+】\n", c.text
        ), c.chunk_id


def test_sentence_split_property():
    """AC-6：單一 L3 > 800 字 → 句號硬切——同一 section_path、chunk_seq 0,1,… 連續遞增、
    每個 chunk body ≤ 800、串接所有 seq 的 body 可無損還原原 L3 內文。"""
    sentence = "測試項目之風險評鑑流程須依規定辦理並留存紀錄。"  # 23 字（synthetic）
    long_body = sentence * 60  # 1380 字 > 800
    lines = ["一、測試章節", "（一）測試節", f"1. {long_body}"]
    chunks = chunk_regulation(
        lines, reg_code="R0X", reg_title="測試辦法", style="chapter"
    )
    assert len(chunks) >= 2
    assert {c.section_path for c in chunks} == {"一/(一)/1"}    # 共用同一 section_path
    assert [c.chunk_seq for c in chunks] == list(range(len(chunks)))  # 0,1,… 連續
    bodies = [_body_of(c) for c in chunks]
    assert all(len(b) <= MAX_BODY_CHARS for b in bodies)
    assert "".join(bodies) == long_body                          # 無損還原


def test_pure_title_skipped_when_no_parent_intro():
    """Lead 裁決邊界（plan 偏離 2 核准）：L1 無引言時，<30 字純標題 L2 skip（不自成 chunk、
    不併入任何 chunk），僅 DEBUG 計數。"""
    lines = ["一、測試章節", "（一）保留", "（二）實質節", "此節有實質內文供切塊測試使用。"]
    chunks = chunk_regulation(lines, reg_code="R0X", reg_title="測試辦法", style="chapter")
    assert [c.section_path for c in chunks] == ["一/(二)"]       # 純標題 (一) 不見於任何 chunk
    assert all("保留" not in c.text for c in chunks)


def test_schema_round_trip_and_extra_forbid():
    """AC-7：RegulationChunk/IndexMeta 載入 example fixture 且 model_dump 往返一致；
    未知欄位 → ValidationError（extra="forbid"）。"""
    chunk_data = json.loads(
        (P301_FIXTURES / "regulation_chunk_example.json").read_text(encoding="utf-8")
    )
    chunk = RegulationChunk.model_validate(chunk_data)
    assert chunk.model_dump() == chunk_data
    assert RegulationChunk.model_validate(chunk.model_dump()) == chunk

    meta_data = json.loads(
        (P301_FIXTURES / "index_meta_example.json").read_text(encoding="utf-8")
    )
    meta = IndexMeta.model_validate(meta_data)
    assert meta.model_dump() == meta_data

    with pytest.raises(ValidationError):
        RegulationChunk.model_validate({**chunk_data, "unknown_field": 1})
    with pytest.raises(ValidationError):
        IndexMeta.model_validate({**meta_data, "unknown_field": 1})


def test_chunk_id_deterministic_pk():
    """AC-8：chunk_id 決定性——固定欄位 → 穩定 id；不同 chunk_seq → 不同 id。"""
    base = dict(
        reg_code="R03", reg_title="測試辦法", section_path="一/(一)",
        title="名詞定義", text="【R03 測試辦法|一、總則>(一)名詞定義】\n內文", order=1,
    )
    c0 = RegulationChunk(**base, chunk_seq=0)
    assert c0.chunk_id == "R03:一/(一):0"
    assert RegulationChunk(**base, chunk_seq=0).chunk_id == c0.chunk_id  # 穩定
    assert RegulationChunk(**base, chunk_seq=1).chunk_id == "R03:一/(一):1"
    assert RegulationChunk(**base, chunk_seq=1).chunk_id != c0.chunk_id


_CHAPTER_PATH_RE = re.compile(
    r"^[一二三四五六七八九十]+(/\([一二三四五六七八九十]+\)(/\d+)?)?$"
)


@pytest.mark.local_data
def test_real_pdf_chunk_smoke():
    """AC-18：真 PDF 冒煙——僅結構事實：R01（條文式）產出 第一條~第四條；
    章節式 R03 的 section_path 全數符合 L1[/(L2)[/L3]] 正規式且含 L2 層級。
    不斷言任何條文內容。data/original 缺 → skip。"""
    if not DATA_ORIGINAL.exists():
        pytest.skip("data/original 未連結（真 PDF 冒煙略過）")
    from govcheck.rag.pdf_text import load_clean_lines

    r01_pdf = sorted(DATA_ORIGINAL.glob("R01*.pdf"))
    r03_pdf = sorted(DATA_ORIGINAL.glob("R03*.pdf"))
    if not r01_pdf or not r03_pdf:
        pytest.skip("data/original 缺 R01/R03 PDF（真 PDF 冒煙略過）")

    r01 = chunk_regulation(
        load_clean_lines(r01_pdf[0]), reg_code="R01", reg_title="R01", style="article"
    )
    paths_r01 = {c.section_path for c in r01}
    assert {"第一條", "第二條", "第三條", "第四條"} <= paths_r01
    assert 1 <= len(r01) <= 200  # 單份合理範圍（全 7 份估 200–400）

    r03 = chunk_regulation(
        load_clean_lines(r03_pdf[0]), reg_code="R03", reg_title="R03", style="chapter"
    )
    assert 1 <= len(r03) <= 200
    assert all(_CHAPTER_PATH_RE.match(c.section_path) for c in r03)
    assert any("/(" in c.section_path for c in r03)  # 至少有 L2 層級路徑


def test_l2_intro_not_lost_when_downgraded_to_l3():
    """Bug fix：L2 同時有 intro_lines 與多個 L3、且 inline_body > 800 時，
    降為逐 L3 切的 else 分支過去只 emit L3、intro_lines 永久遺失。
    本測試確認修正後 intro 不遺失（spec §4.2 losslessness 意圖）。

    對應 bug：chunker.py else 分支未 emit l2.intro_lines。
    對應 spec §4.2 長度控制：L2 body > 800 → 降為逐 L3 切；L2 intro 須先另 emit。
    """
    # 每個 L3 約 450 字的 synthetic 內文（句子 × 次數），確保各自 < 800。
    l3_sentence = "本項作業應依規定程序辦理並留存完整紀錄以供查核。"  # 24 字
    l3_body = l3_sentence * 19  # 456 字，< 800

    # L2 intro 散文（明確的識別文字），讓 inline_body 被它推過 800 門檻。
    intro_text = "本節說明評鑑作業之整體流程與各關係人之責任分工，各單位應確實遵循。"  # 36 字

    # inline_body = intro(36) + L3-1-header(8) + l3_body(456) + L3-2-header(8) + l3_body(456) ≈ 964 > 800
    lines = [
        "三、評鑑作業",
        "（一）整體程序",
        intro_text,
        f"1. {l3_body}",
        f"2. {l3_body}",
    ]

    chunks = chunk_regulation(
        lines, reg_code="R0T", reg_title="測試辦法", style="chapter"
    )

    # 篩出本 L2 相關 chunk（section_path 以 三/(一) 開頭）
    l2_path = "三/(一)"
    related = [c for c in chunks if c.section_path.startswith(l2_path)]

    # (a) 必須有一個 section_path == l2_path 且 body 含 intro 文字的 chunk
    intro_chunks = [c for c in related if c.section_path == l2_path]
    assert intro_chunks, "應有一個 section_path=三/(一) 的 intro chunk"
    intro_body = _body_of(intro_chunks[0])
    assert intro_text in intro_body, "intro_lines 應出現在 intro chunk 的 body 中"

    # (b) losslessness：L2 相關所有 chunk 的 body 串接後，intro 文字與各 L3 文字均存在
    all_bodies = "".join(_body_of(c) for c in related)
    assert intro_text in all_bodies, "intro 文字不應遺失"
    assert l3_body in all_bodies, "L3 內文不應遺失"

    # (c) 各 chunk body ≤ 800
    for c in related:
        body = _body_of(c)
        assert len(body) <= MAX_BODY_CHARS, (
            f"chunk {c.section_path} body={len(body)} 超過 {MAX_BODY_CHARS}"
        )
