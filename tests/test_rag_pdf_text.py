"""rag/pdf_text 清洗規則測試（p3-01 spec §4.1；golden 直接消費 contracts/fixtures/）。"""

from pathlib import Path

import pytest
import yaml

from govcheck.rag.pdf_text import clean_pages, load_clean_lines

ROOT = Path(__file__).resolve().parents[1]
P301_FIXTURES = ROOT / "specs" / "p3-01-regulation-index" / "contracts" / "fixtures"
DATA_ORIGINAL = ROOT / "data" / "original"


def _load_dirty_fixture() -> dict:
    with (P301_FIXTURES / "dirty_pdf_lines.yaml").open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_clean_pages_golden():
    """AC-1：髒輸入頁 golden——刪頁碼行、刪 100% 重複頁首尾、CJK 接行、
    不併行邊界（下一行為標題 token／上一行以「：」結尾）。"""
    fx = _load_dirty_fixture()
    assert len(fx["pages"]) == fx["num_pages"]  # fixture 自洽
    assert clean_pages(fx["pages"]) == fx["expected_lines"]


def test_r2_not_applied_below_3_pages():
    """AC-1 邊界：總頁數 < 3 時 R2（重複頁首尾刪除）不套用，即使比例 100%。"""
    pages = [
        ["重複頁首樣板文字。", "第一頁內容行。"],
        ["重複頁首樣板文字。", "第二頁內容行。"],
    ]
    assert clean_pages(pages) == [
        "重複頁首樣板文字。",
        "第一頁內容行。",
        "重複頁首樣板文字。",
        "第二頁內容行。",
    ]


@pytest.mark.parametrize("tail", ["。", "：", "；", "！", "？"])
def test_r3_no_merge_after_sentence_end_punct(tail):
    """AC-1 邊界：上一行以句末標點（。：；！？）結尾 → 不接行。"""
    pages = [[f"上一行結尾{tail}", "下一行普通內容"]]
    assert clean_pages(pages) == [f"上一行結尾{tail}", "下一行普通內容"]


def test_r3_merges_and_does_not_cross_pages():
    """AC-1 邊界：斷句併回（無空白直接串接）；接行不跨頁。"""
    pages = [
        ["此行結尾未斷句", "後半段接續文字。"],
        ["次頁開頭未斷句", "次頁後半段。"],
    ]
    # 頁內併行，但頁 1 末行（。結尾）與頁 2 首行不相接；即使頁 1 末行未以句號結尾也不得跨頁
    assert clean_pages(pages) == ["此行結尾未斷句後半段接續文字。", "次頁開頭未斷句次頁後半段。"]

    pages_cross = [["頁一末行未斷句"], ["頁二首行內容。"]]
    assert clean_pages(pages_cross) == ["頁一末行未斷句", "頁二首行內容。"]


@pytest.mark.parametrize(
    "title_line",
    ["第三條 條文標題", "二、章節標題", "（二）節標題", "(二)節標題", "2. 項目標題", "2、項目標題"],
)
def test_r3_no_merge_when_next_is_title_token(title_line):
    """AC-1 邊界：下一行為標題 token（條/L1/L2/L3 任一型）→ 不接行。"""
    pages = [["上一行結尾未斷句", title_line]]
    assert clean_pages(pages) == ["上一行結尾未斷句", title_line]


@pytest.mark.parametrize("noise", ["- 3 -", "3", "-1-", "第 12 頁", "3 / 10"])
def test_r1_page_number_variants(noise):
    """AC-1 邊界：頁碼行三種 pattern 變體皆刪除。"""
    pages = [["實際內容行。", noise]]
    assert clean_pages(pages) == ["實際內容行。"]


@pytest.mark.local_data
def test_load_clean_lines_real_pdf_smoke():
    """AC-18（部分）：真 PDF 冒煙——僅結構性斷言（行數 > 0、皆為非空字串），
    不斷言任何條文內容。data/original 缺 → skip。"""
    pdfs = sorted(DATA_ORIGINAL.glob("R01*.pdf")) if DATA_ORIGINAL.exists() else []
    if not pdfs:
        pytest.skip("data/original 未連結（真 PDF 冒煙略過）")
    lines = load_clean_lines(pdfs[0])
    assert len(lines) > 0
    assert all(isinstance(ln, str) and ln.strip() for ln in lines)
