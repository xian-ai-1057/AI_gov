"""從官方 F02 範本程式化生成測試 fixture（正例/反例）。

做法：複製官方 .xlsm，寫入指定的 C 欄答案、加成因子、以及「Excel 已存的」快取分數
（E46:H46 / I2 / I4），再選擇性地在續填表寫入資料列。寫入快取格時用字面值覆蓋公式，
parser 以 data_only 讀回即為我們設定的值——藉此完全掌控每條規則的正/反路徑。

絕不寫回 data/original：輸出一律寫到呼叫端指定的暫存/fixtures 路徑。
"""

from __future__ import annotations

import warnings
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL = ROOT / "data" / "original" / "附件二：AI-R02-F02 AI風險評鑑.xlsm"

ASSESS_SHEET = "AI系統固有風險分級評估表"
RESIDUAL_SHEET = "AI系統剩餘風險評鑑表"
TREATMENT_SHEET = "AI系統風險處理計畫表"

PCT_CELLS = {"finance": "E46", "operation": "F46", "reputation": "G46", "compliance": "H46"}


def build_f02_fixture(
    out_path: str | Path,
    answers: dict[str, str] | None = None,
    uplift: dict[str, str] | None = None,
    cached_pct: dict[str, float] | None = None,
    cached_overall: float | None = None,
    cached_grade: str | None = None,
    residual_rows: list[list] | None = None,
    treatment_rows: list[list] | None = None,
) -> Path:
    out_path = Path(out_path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wb = openpyxl.load_workbook(OFFICIAL, keep_vba=True)
    ws = wb[ASSESS_SHEET]

    qid_row = _qid_to_row(ws)
    uplift_row = {"factor1": 42, "factor2": 43, "factor3": 44}

    for qid, val in (answers or {}).items():
        if qid not in qid_row:
            raise KeyError(f"未知題號 {qid}")
        ws[f"C{qid_row[qid]}"] = val
    for name, val in (uplift or {}).items():
        ws[f"C{uplift_row[name]}"] = val

    # 覆蓋快取分數（字面值取代公式）
    for d, val in (cached_pct or {}).items():
        ws[PCT_CELLS[d]] = val
    if cached_overall is not None:
        ws["I2"] = cached_overall
    if cached_grade is not None:
        ws["I4"] = cached_grade

    _write_rows(wb[RESIDUAL_SHEET], residual_rows)
    _write_rows(wb[TREATMENT_SHEET], treatment_rows)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path


def _qid_to_row(ws) -> dict[str, int]:
    mapping = {}
    for row in ws.iter_rows():
        v = row[0].value
        if isinstance(v, str) and v.strip():
            mapping[v.strip()] = row[0].row
    return mapping


def _write_rows(ws, rows: list[list] | None) -> None:
    if not rows:
        return
    for r, values in enumerate(rows, start=2):  # 表頭在第 1 列
        for c, val in enumerate(values, start=1):
            ws.cell(row=r, column=c, value=val)
