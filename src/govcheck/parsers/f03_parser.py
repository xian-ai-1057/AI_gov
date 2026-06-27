"""F03 (.xlsx) 識別解析器：只取 System Owner（B1）供跨表一致性比對。

F03「檢核表」的生命週期檢核項屬判讀性質，留待 Phase 3；本函式刻意命名為
parse_f03_identity，Phase 3 另加 parse_f03_checklist 不衝突。座標來自 review_config.yaml。
"""

from __future__ import annotations

import warnings
from pathlib import Path

import openpyxl

from govcheck.models import F03Identity
from govcheck.parsers._util import clean
from govcheck.review.config import load_review_config


def parse_f03_identity(path: str | Path, cfg: dict | None = None) -> F03Identity:
    cfg = cfg or load_review_config()
    f03cfg = cfg["f03"]
    path = Path(path)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wb = openpyxl.load_workbook(path, data_only=True)

    sheet = f03cfg["sheet"]
    if sheet not in wb.sheetnames:
        wb.close()
        return F03Identity(sheet_present=False)

    owner = clean(wb[sheet][f03cfg["owner_cell"]].value)
    wb.close()
    # 範本預設 B1="XXX"，視同未填，避免誤報跨表 owner 不一致
    placeholders = {p.casefold() for p in f03cfg.get("placeholder_values", [])}
    if owner and owner.casefold() in placeholders:
        owner = None
    return F03Identity(system_owner=owner, sheet_present=True)
